"""Quick end-to-end smoke test of the MCP server over stdio."""
import json
import subprocess
import sys

def send(proc, req):
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    return json.loads(proc.stdout.readline())

proc = subprocess.Popen(
    [sys.executable, "scar_server.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, bufsize=1,
)

# 1. initialize
r = send(proc, {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}})
print("init:", r["result"]["serverInfo"])

# 2. list tools
r = send(proc, {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
print(f"tools: {[t['name'] for t in r['result']['tools']]}")

# 3. agent A submits a novel scar
r = send(proc, {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{
    "name":"scar_submit",
    "arguments":{
        "agent_id":"agent_alice",
        "action_signature":"deploy lambda with 128MB memory and numpy import",
        "failure_mode":"cold_start_oom",
        "context":{"runtime":"python3.11"},
        "evidence":"Runtime exited with error: signal: killed. OOMKilled at cold start."
    }
}})
print("submit:", json.loads(r["result"]["content"][0]["text"]))

# 4. agent B asks before doing the same thing
r = send(proc, {"jsonrpc":"2.0","id":4,"method":"tools/call","params":{
    "name":"scar_check",
    "arguments":{
        "agent_id":"agent_bob",
        "action_signature":"deploy lambda 128MB with numpy",
    }
}})
result = json.loads(r["result"]["content"][0]["text"])
print(f"check: {len(result['hits'])} hit(s), charged ${result['price_charged_usdc']}")
if result['hits']:
    print(f"  -> {result['hits'][0]['failure_mode']} (conf {result['hits'][0]['confidence']})")

# 5. wallet status for both
for who in ["agent_alice", "agent_bob"]:
    r = send(proc, {"jsonrpc":"2.0","id":5,"method":"tools/call","params":{
        "name":"wallet_status","arguments":{"agent_id":who}
    }})
    ws = json.loads(r["result"]["content"][0]["text"])
    print(f"{who} balance: ${ws['balance_usdc']:.5f}")

proc.terminate()
print("\nOK — MCP server end-to-end works.")
