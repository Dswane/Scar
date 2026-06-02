"""
Scar MCP Server — negative-result database for autonomous agents.

Reference implementation. Single-file. Runs on stdio (standard MCP transport).
Production version would swap in pgvector + a real wallet ledger + x402/Stripe.

Tools exposed to agents:
    scar_check     -- "is this action known to fail?"  (you pay us)
    scar_submit    -- "I just failed, here's the scar"  (we pay you, if novel)
    scar_bounty_post -- "pay me if you've seen X fail"   (reverse auction)
    wallet_status  -- balance + ledger
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Any

# ---------------------------------------------------------------------------
# Pricing — every number here is the actual product
# ---------------------------------------------------------------------------

PRICE_CHECK_BASE_USDC = 0.0001          # only charged when we actually return hits
PRICE_CHECK_MAX_USDC = 0.01             # ceiling — high-confidence rare hit
PRICE_FETCH_USDC = 0.0003               # full-evidence reveal; 50% royalty to submitter
PRICE_SUBMIT_BOUNTY_USDC = 0.0005       # paid to writer for genuinely novel scar
PRICE_SUBMIT_DUPE_PENALTY_USDC = 0.0001 # charged for redundant submissions
READ_ROYALTY_FRACTION = 0.20            # share of a paid scar_check paid back to submitters
FETCH_ROYALTY_FRACTION = 0.50           # share of a scar_fetch paid back to the submitter
SIM_DEDUP_THRESHOLD = 0.85              # Jaccard threshold above which a submit is a dupe
NOVEL_RATE_LIMIT_24H = 20               # max novel payouts per agent per 24h
BOUNTY_MATCH_THRESHOLD = 0.5            # Jaccard threshold for a bounty -> scar match
BOUNTY_DEFAULT_TTL_S = 30
DEFAULT_MATCH_THRESHOLD = 0.5
MATCH_THRESHOLD_FLOOR = 0.3

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Scar:
    """One recorded failure."""
    scar_id: str
    action_signature: str        # the hashable "what was being attempted"
    failure_mode: str            # short label: "timeout", "auth_error", "schema_mismatch"
    context: dict                # arbitrary structured detail
    evidence: str                # stack trace, response body, whatever
    submitted_by: str            # agent_id of writer
    submitted_at: float
    confirmations: int = 1       # how many other agents have re-hit this
    last_confirmed_at: float = 0.0


@dataclass
class WalletEntry:
    ts: float
    delta_usdc: float
    reason: str
    ref_id: str


@dataclass
class Wallet:
    agent_id: str
    balance_usdc: float = 0.0
    ledger: list[WalletEntry] = field(default_factory=list)

    def credit(self, amount: float, reason: str, ref_id: str) -> None:
        self.balance_usdc += amount
        self.ledger.append(WalletEntry(time.time(), +amount, reason, ref_id))

    def debit(self, amount: float, reason: str, ref_id: str) -> bool:
        if self.balance_usdc < amount:
            return False
        self.balance_usdc -= amount
        self.ledger.append(WalletEntry(time.time(), -amount, reason, ref_id))
        return True


# ---------------------------------------------------------------------------
# Store — in-memory, but the interface is the API
# ---------------------------------------------------------------------------

class ScarStore:
    def __init__(self) -> None:
        self.scars: dict[str, Scar] = {}
        self.by_signature: dict[str, list[str]] = defaultdict(list)
        self.wallets: dict[str, Wallet] = {}
        self.bounties: dict[str, dict] = {}
        self.reads: list[dict] = []  # scar_check audit trail for read-driven confirmations

    # --- wallet helpers ----------------------------------------------------

    def wallet(self, agent_id: str) -> Wallet:
        if agent_id not in self.wallets:
            # Bootstrap with a tiny float so demos work; production: require funding.
            w = Wallet(agent_id=agent_id, balance_usdc=2.00)
            w.ledger.append(WalletEntry(time.time(), +2.00, "signup_bonus", "init"))
            self.wallets[agent_id] = w
        return self.wallets[agent_id]

    # --- core operations ---------------------------------------------------

    @staticmethod
    def _signature_hash(action_signature: str) -> str:
        return hashlib.sha256(action_signature.lower().strip().encode()).hexdigest()[:16]

    @staticmethod
    def _similarity(a: str, b: str) -> float:
        """Cheap-and-dirty signature similarity. Production: real embeddings."""
        ta, tb = set(a.lower().split()), set(b.lower().split())
        if not ta or not tb:
            return 0.0
        return len(ta & tb) / len(ta | tb)

    def check(
        self,
        agent_id: str,
        action_signature: str,
        context: dict,
        match_threshold: float | None = None,
    ) -> dict:
        """
        Look up known failures for this action. $0 on miss (we charge for value
        delivered, not for the work of looking). On a hit, 20% of the charge is
        paid out to the top hits' original submitters as a royalty.
        """
        threshold = max(MATCH_THRESHOLD_FLOOR, match_threshold if match_threshold is not None else DEFAULT_MATCH_THRESHOLD)
        sig_h = self._signature_hash(action_signature)
        candidates: list[tuple[Scar, float]] = []
        for scar in self.scars.values():
            if self._signature_hash(scar.action_signature) == sig_h:
                candidates.append((scar, 1.0))
            else:
                s = self._similarity(scar.action_signature, action_signature)
                if s >= threshold:
                    candidates.append((scar, s))

        candidates.sort(
            key=lambda cs: cs[1] * math.log(cs[0].confirmations + 1) + cs[1],
            reverse=True,
        )
        candidates = candidates[:5]

        wallet = self.wallet(agent_id)

        # $0 on miss — agents can call this freely when there's nothing to learn.
        if not candidates:
            return {
                "ok": True,
                "price_charged_usdc": 0.0,
                "balance_usdc": round(wallet.balance_usdc, 6),
                "hits": [],
            }

        top_conf = candidates[0][1] * math.log(candidates[0][0].confirmations + 1.7)
        price = min(PRICE_CHECK_MAX_USDC, PRICE_CHECK_BASE_USDC + 0.0015 * top_conf)
        price = round(price, 6)

        ref_id = f"check_{uuid.uuid4().hex[:8]}"
        if not wallet.debit(price, "scar_check", ref_id):
            return {
                "ok": False,
                "error": "insufficient_funds",
                "required_usdc": price,
                "balance_usdc": wallet.balance_usdc,
            }

        # Log the read so a later matching submit can be treated as confirmation.
        top_scar = candidates[0][0]
        top_sim = candidates[0][1]
        self.reads.append({
            "agent_id": agent_id,
            "scar_id": top_scar.scar_id,
            "top_confidence": round(top_sim, 4),
            "ts": time.time(),
        })

        # Royalty pool: 20% of the read price, distributed across the top-3
        # unique submitters by similarity weight (self-payouts skipped).
        royalty_pool = round(price * READ_ROYALTY_FRACTION, 6)
        weights: dict[str, float] = {}
        for scar, sim in candidates[:3]:
            if scar.submitted_by and scar.submitted_by != agent_id:
                weights[scar.submitted_by] = weights.get(scar.submitted_by, 0.0) + sim
        total_w = sum(weights.values())
        if total_w > 0 and royalty_pool > 0:
            for payee, w in weights.items():
                share = round(royalty_pool * (w / total_w), 6)
                if share > 0:
                    self.wallet(payee).credit(share, "scar_read_royalty", ref_id)

        return {
            "ok": True,
            "price_charged_usdc": price,
            "royalty_pool_usdc": royalty_pool,
            "balance_usdc": round(wallet.balance_usdc, 6),
            "hits": [
                {
                    "scar_id": scar.scar_id,
                    "failure_mode": scar.failure_mode,
                    "confidence": round(sim, 3),
                    "confirmations": scar.confirmations,
                    "context": scar.context,
                    "evidence_preview": scar.evidence[:240],
                    "age_seconds": round(time.time() - scar.submitted_at),
                }
                for scar, sim in candidates
            ],
        }

    def submit(
        self,
        agent_id: str,
        action_signature: str,
        failure_mode: str,
        context: dict,
        evidence: str,
    ) -> dict:
        """
        Agent reports a failure. Novel scars get paid; near-duplicates (same
        failure_mode and similar signature) are treated as confirmations and
        charged the sliver dupe fee. Novel payouts are rate-limited per 24h to
        cap sybil farming. Matching open bounties pay out to the submitter.
        """
        sig_h = self._signature_hash(action_signature)
        existing: Scar | None = None

        # Exact-signature dedup.
        for sid in self.by_signature.get(sig_h, []):
            cand = self.scars[sid]
            if cand.failure_mode.lower() == failure_mode.lower():
                existing = cand
                break

        # Vector-similarity dedup: catches paraphrased resubmissions of the
        # same failure. Same failure_mode only — different failure modes are
        # different scars even when the action looks alike.
        if existing is None:
            for scar in self.scars.values():
                if scar.failure_mode.lower() != failure_mode.lower():
                    continue
                if self._similarity(scar.action_signature, action_signature) >= SIM_DEDUP_THRESHOLD:
                    existing = scar
                    break

        wallet = self.wallet(agent_id)
        if existing:
            existing.confirmations += 1
            existing.last_confirmed_at = time.time()
            ref_id = f"submit_dupe_{uuid.uuid4().hex[:8]}"
            wallet.debit(PRICE_SUBMIT_DUPE_PENALTY_USDC, "scar_submit_dupe", ref_id)
            return {
                "ok": True,
                "novel": False,
                "scar_id": existing.scar_id,
                "confirmations": existing.confirmations,
                "price_charged_usdc": PRICE_SUBMIT_DUPE_PENALTY_USDC,
                "balance_usdc": round(wallet.balance_usdc, 6),
            }

        # Rate limit novel payouts by counting recent ledger entries.
        cutoff = time.time() - 24 * 3600
        recent_novel = sum(
            1 for e in wallet.ledger
            if e.reason == "scar_submit_novel" and e.ts > cutoff
        )
        rate_limited = recent_novel >= NOVEL_RATE_LIMIT_24H

        scar = Scar(
            scar_id=f"scar_{uuid.uuid4().hex[:10]}",
            action_signature=action_signature,
            failure_mode=failure_mode,
            context=context,
            evidence=evidence,
            submitted_by=agent_id,
            submitted_at=time.time(),
        )
        self.scars[scar.scar_id] = scar
        self.by_signature[sig_h].append(scar.scar_id)

        ref_id = f"submit_novel_{uuid.uuid4().hex[:8]}"
        paid = 0.0
        if not rate_limited:
            wallet.credit(PRICE_SUBMIT_BOUNTY_USDC, "scar_submit_novel", ref_id)
            paid = PRICE_SUBMIT_BOUNTY_USDC
        else:
            wallet.ledger.append(WalletEntry(time.time(), 0.0, "scar_submit_rate_limited", ref_id))

        # Bounty match: pay the submitter from the best open bounty whose
        # action signature is similar enough. Skip self-posted bounties.
        bounty_paid = 0.0
        matched_bounty_id: str | None = None
        candidates_b = []
        now = time.time()
        for bid, b in self.bounties.items():
            if b["expires_at"] <= now or b["agent_id"] == agent_id:
                continue
            if self._similarity(b["action_signature"], action_signature) >= BOUNTY_MATCH_THRESHOLD:
                candidates_b.append((bid, b))
        candidates_b.sort(key=lambda kv: kv[1]["max_pay_usdc"], reverse=True)
        for bid, b in candidates_b:
            poster_wallet = self.wallet(b["agent_id"])
            pay = float(b["max_pay_usdc"])
            if poster_wallet.debit(pay, "bounty_paid_out", bid):
                wallet.credit(pay, "bounty_collected", bid)
                bounty_paid = pay
                matched_bounty_id = bid
                break
        if matched_bounty_id is not None:
            self.bounties.pop(matched_bounty_id, None)

        return {
            "ok": True,
            "novel": True,
            "scar_id": scar.scar_id,
            "paid_usdc": paid,
            "rate_limited": rate_limited,
            "bounty_paid_usdc": bounty_paid,
            "balance_usdc": round(wallet.balance_usdc, 6),
        }

    def fetch(self, agent_id: str, scar_id: str) -> dict:
        """
        Paid reveal of the full evidence body. 50% goes to the original
        submitter as a long-tail royalty.
        """
        scar = self.scars.get(scar_id)
        if scar is None:
            return {"ok": False, "error": "not_found"}
        wallet = self.wallet(agent_id)
        ref_id = f"fetch_{uuid.uuid4().hex[:8]}"
        if not wallet.debit(PRICE_FETCH_USDC, "scar_fetch", ref_id):
            return {
                "ok": False,
                "error": "insufficient_funds",
                "required_usdc": PRICE_FETCH_USDC,
                "balance_usdc": wallet.balance_usdc,
            }
        royalty = 0.0
        if scar.submitted_by and scar.submitted_by != agent_id:
            royalty = round(PRICE_FETCH_USDC * FETCH_ROYALTY_FRACTION, 6)
            self.wallet(scar.submitted_by).credit(royalty, "scar_fetch_royalty", ref_id)
        return {
            "ok": True,
            "scar_id": scar.scar_id,
            "action_signature": scar.action_signature,
            "failure_mode": scar.failure_mode,
            "context": scar.context,
            "evidence": scar.evidence,
            "confirmations": scar.confirmations,
            "age_seconds": round(time.time() - scar.submitted_at),
            "price_charged_usdc": PRICE_FETCH_USDC,
            "royalty_paid_usdc": royalty,
            "balance_usdc": round(wallet.balance_usdc, 6),
        }

    def bounty_post(
        self, agent_id: str, action_signature: str, max_pay_usdc: float,
        ttl_s: int = BOUNTY_DEFAULT_TTL_S,
    ) -> dict:
        """
        Reverse auction: agent says "I'll pay up to $X to anyone who can tell me
        about failures for this action in the next N seconds." Other agents'
        scar_submit calls matching this signature get matched + paid.
        """
        bounty_id = f"bnty_{uuid.uuid4().hex[:10]}"
        self.bounties[bounty_id] = {
            "agent_id": agent_id,
            "action_signature": action_signature,
            "max_pay_usdc": max_pay_usdc,
            "expires_at": time.time() + ttl_s,
        }
        return {"ok": True, "bounty_id": bounty_id, "expires_in_s": ttl_s}

    def wallet_status(self, agent_id: str) -> dict:
        w = self.wallet(agent_id)
        return {
            "agent_id": agent_id,
            "balance_usdc": round(w.balance_usdc, 6),
            "recent_ledger": [asdict(e) for e in w.ledger[-10:]],
        }


# ---------------------------------------------------------------------------
# MCP server (stdio JSON-RPC). Minimal handcrafted impl so this file is
# zero-dependency. Drop-in compatible with the MCP spec for tools/list and
# tools/call requests.
# ---------------------------------------------------------------------------

import sys

STORE = ScarStore()

TOOLS = [
    {
        "name": "scar_check",
        "description": "Query the negative-result database BEFORE attempting an action. Returns known failure modes for similar actions, ranked by confidence. Charged in USDC scaled by hit quality — **no hits is free**; high-confidence rare hits go up to $0.01. 20% of every paid read is paid as a royalty to the original submitters, so good scars keep earning. Call this whenever you are about to do something potentially flaky: an API call, a code path, a tool invocation, anything where 'has this failed before' is a useful signal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "action_signature": {
                    "type": "string",
                    "description": "Free-text description of what you are about to do, e.g. 'POST https://api.foo.com/v1/widgets with bearer token at peak hours' or 'pip install torch==2.1 on python 3.12 arm64'.",
                },
                "context": {"type": "object", "description": "Optional structured context."},
                "match_threshold": {
                    "type": "number",
                    "description": "Optional similarity threshold (0.3–1.0). Lower values cast a wider net at the cost of confidence; default 0.5.",
                },
            },
            "required": ["agent_id", "action_signature"],
        },
    },
    {
        "name": "scar_submit",
        "description": "Report a failure you just experienced. You GET PAID $0.0005 if it's a novel scar (capped at 20 novel payouts per agent per 24h to discourage farming). You pay $0.0001 if it's a duplicate — duplicates are detected by exact signature AND fuzzy similarity, so paraphrased resubmissions are caught. If an open bounty matches your scar, you also collect the bounty payout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "action_signature": {"type": "string"},
                "failure_mode": {"type": "string", "description": "Short label: 'timeout', 'auth_error', 'schema_mismatch', 'rate_limit', 'silent_corruption', etc."},
                "context": {"type": "object"},
                "evidence": {"type": "string", "description": "Stack trace, error response body, log excerpt — whatever helps the next agent recognize this failure."},
            },
            "required": ["agent_id", "action_signature", "failure_mode", "evidence"],
        },
    },
    {
        "name": "scar_fetch",
        "description": "Reveal the FULL evidence body for a scar_id seen in a scar_check preview. Charged at ~3x the check base price; 50% is paid to the original submitter as a royalty. Use this when the 240-char preview isn't enough to diagnose or adapt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "scar_id": {"type": "string"},
            },
            "required": ["agent_id", "scar_id"],
        },
    },
    {
        "name": "scar_bounty_post",
        "description": "Post a reverse auction: 'I'll pay up to $max_pay_usdc to any agent who can tell me about failures for this action in the next N seconds.' If another agent submits a matching scar before expiry, the payout transfers from your wallet to theirs automatically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "action_signature": {"type": "string"},
                "max_pay_usdc": {"type": "number"},
                "ttl_s": {"type": "integer"},
            },
            "required": ["agent_id", "action_signature", "max_pay_usdc"],
        },
    },
    {
        "name": "wallet_status",
        "description": "Get your current balance and recent ledger entries.",
        "inputSchema": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
]


def handle_request(req: dict) -> dict | None:
    method = req.get("method")
    req_id = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "scar", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "scar_check":
                result = STORE.check(
                    args["agent_id"], args["action_signature"],
                    args.get("context") or {}, args.get("match_threshold"),
                )
            elif name == "scar_submit":
                result = STORE.submit(
                    args["agent_id"], args["action_signature"],
                    args["failure_mode"], args.get("context") or {}, args["evidence"],
                )
            elif name == "scar_fetch":
                result = STORE.fetch(args["agent_id"], args["scar_id"])
            elif name == "scar_bounty_post":
                result = STORE.bounty_post(
                    args["agent_id"], args["action_signature"],
                    args["max_pay_usdc"], args.get("ttl_s", BOUNTY_DEFAULT_TTL_S),
                )
            elif name == "wallet_status":
                result = STORE.wallet_status(args["agent_id"])
            else:
                return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"unknown tool {name}"}}
        except KeyError as e:
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": f"missing arg {e}"}}

        return {
            "jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": json.dumps(result, indent=2)}]},
        }

    if method == "notifications/initialized":
        return None

    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"method {method} not found"}}


def serve_stdio() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve_stdio()
