"""End-to-end smoke test of the MCP server over stdio.

Covers the v1 path (init, list, submit, check, wallet) plus the hardening
additions: $0 on miss, scar_fetch + submitter royalty, vector-sim dedup,
and bounty match payout.
"""
import json
import subprocess
import sys


def send(proc, req):
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())


def call(proc, req_id, name, arguments):
    r = send(proc, {
        "jsonrpc": "2.0", "id": req_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    return json.loads(r["result"]["content"][0]["text"])


def balance(proc, req_id, agent_id):
    return call(proc, req_id, "wallet_status", {"agent_id": agent_id})["balance_usdc"]


proc = subprocess.Popen(
    [sys.executable, "scar_server.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)

failures = []

def check_assert(label, cond, detail=""):
    status = "ok" if cond else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        failures.append(label)


# 1. initialize + list
r = send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
print("init:", r["result"]["serverInfo"])
r = send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
tool_names = [t["name"] for t in r["result"]["tools"]]
print(f"tools: {tool_names}")
check_assert("scar_fetch exposed", "scar_fetch" in tool_names)

# 2. alice submits a novel scar
sub_alice = call(proc, 3, "scar_submit", {
    "agent_id": "agent_alice",
    "action_signature": "deploy lambda with 128MB memory and numpy import",
    "failure_mode": "cold_start_oom",
    "context": {"runtime": "python3.11"},
    "evidence": "Runtime exited with error: signal: killed. OOMKilled at cold start.",
})
print("submit alice:", sub_alice)
check_assert("alice submit is novel", sub_alice["novel"])
check_assert("alice paid the novel bounty", sub_alice["paid_usdc"] == 0.0005)
alice_scar_id = sub_alice["scar_id"]

# 3. $0 on miss: bob queries something completely unrelated, expects free
miss = call(proc, 4, "scar_check", {
    "agent_id": "agent_bob",
    "action_signature": "totally unrelated query about quantum cryptography backflips",
})
print("miss check:", miss)
check_assert("miss is $0", miss["price_charged_usdc"] == 0.0)
check_assert("miss returns empty hits", miss["hits"] == [])

# 4. bob queries the lambda thing — gets a hit, pays, alice gets royalty
alice_before = balance(proc, 5, "agent_alice")
hit = call(proc, 6, "scar_check", {
    "agent_id": "agent_bob",
    "action_signature": "deploy lambda 128MB with numpy",
})
print(f"hit check: {len(hit['hits'])} hit(s), charged ${hit['price_charged_usdc']}")
check_assert("paid check has hits", len(hit["hits"]) > 0)
check_assert("paid check costs more than zero", hit["price_charged_usdc"] > 0)
alice_after = balance(proc, 7, "agent_alice")
royalty = round(alice_after - alice_before, 8)
expected_pool = round(hit["price_charged_usdc"] * 0.20, 6)
check_assert(
    "alice received the read royalty",
    abs(royalty - expected_pool) < 1e-6,
    f"got {royalty}, pool {expected_pool}",
)

# 5. scar_fetch reveals full evidence and pays alice 50%
alice_before = balance(proc, 8, "agent_alice")
fetched = call(proc, 9, "scar_fetch", {"agent_id": "agent_bob", "scar_id": alice_scar_id})
print(f"fetch: charged ${fetched['price_charged_usdc']}, royalty ${fetched['royalty_paid_usdc']}")
check_assert("fetch returned full evidence", "OOMKilled" in fetched["evidence"])
check_assert("fetch royalty is 50%", fetched["royalty_paid_usdc"] == round(fetched["price_charged_usdc"] * 0.5, 6))
alice_after = balance(proc, 10, "agent_alice")
check_assert(
    "alice received the fetch royalty",
    abs((alice_after - alice_before) - fetched["royalty_paid_usdc"]) < 1e-6,
)

# 6. fuzzy dedup: a reordered resubmission with same failure_mode is a dupe.
# The stdio similarity is bag-of-words Jaccard (caught by ~0.85+ overlap).
# The hosted server uses gte-small cosine at 0.95, which catches much richer
# semantic paraphrases — this test only exercises that the CODE PATH fires
# on a near-duplicate, not that bag-of-words can do semantic paraphrase.
dupe = call(proc, 11, "scar_submit", {
    "agent_id": "agent_eve",
    "action_signature": "deploy lambda with 128MB memory and numpy",  # drops "import"
    "failure_mode": "cold_start_oom",
    "evidence": "different evidence string but same underlying failure",
})
print("near-duplicate submit:", dupe)
check_assert("near-duplicate caught as dupe", not dupe["novel"])
check_assert("near-duplicate matched alice's scar", dupe["scar_id"] == alice_scar_id)

# 7. bounty match: carol posts a bounty, dave submits a matching scar, dave gets paid
bounty = call(proc, 12, "scar_bounty_post", {
    "agent_id": "agent_carol",
    "action_signature": "fetch market data from coingecko api with rate-limited key",
    "max_pay_usdc": 0.005,
    "ttl_s": 30,
})
print("bounty:", bounty)
carol_before = balance(proc, 13, "agent_carol")
dave_before = balance(proc, 14, "agent_dave")
sub_dave = call(proc, 15, "scar_submit", {
    "agent_id": "agent_dave",
    "action_signature": "GET coingecko api market data with a rate-limited api key",
    "failure_mode": "rate_limit",
    "evidence": "429 Too Many Requests, retry-after: 60",
})
print("dave submit:", sub_dave)
check_assert("dave's submit collected the bounty", sub_dave["bounty_paid_usdc"] == 0.005)
carol_after = balance(proc, 16, "agent_carol")
dave_after = balance(proc, 17, "agent_dave")
check_assert(
    "carol's balance dropped by the bounty",
    abs((carol_before - carol_after) - 0.005) < 1e-6,
    f"before {carol_before} after {carol_after}",
)
check_assert(
    "dave's balance went up by novel + bounty",
    abs((dave_after - dave_before) - (0.0005 + 0.005)) < 1e-6,
    f"before {dave_before} after {dave_after}",
)

# 8. final wallet snapshot
for who in ["agent_alice", "agent_bob", "agent_carol", "agent_dave", "agent_eve"]:
    print(f"  {who} balance: ${balance(proc, 18, who):.6f}")

proc.terminate()

if failures:
    print(f"\nFAILED ({len(failures)}): " + ", ".join(failures))
    sys.exit(1)
print("\nOK — MCP server end-to-end works, hardening checks pass.")
