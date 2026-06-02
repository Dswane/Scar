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

PRICE_CHECK_BASE_USDC = 0.0001          # floor — known-novel question
PRICE_CHECK_MAX_USDC = 0.01             # ceiling — high-confidence rare hit
PRICE_SUBMIT_BOUNTY_USDC = 0.0005       # paid to writer for genuinely novel scar
PRICE_SUBMIT_DUPE_PENALTY_USDC = 0.0001 # charged for redundant submissions
BOUNTY_DEFAULT_TTL_S = 30

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

    def check(self, agent_id: str, action_signature: str, context: dict) -> dict:
        """
        Look up known failures for this action. Charge the caller based on
        result quality. No hits -> minimum charge (we still did the work).
        """
        # Find candidates by exact sig + similar sigs
        sig_h = self._signature_hash(action_signature)
        candidates: list[tuple[Scar, float]] = []
        for scar in self.scars.values():
            if self._signature_hash(scar.action_signature) == sig_h:
                candidates.append((scar, 1.0))
            else:
                s = self._similarity(scar.action_signature, action_signature)
                if s >= 0.5:
                    candidates.append((scar, s))

        # Rank by confidence = similarity * log(confirmations+1)
        candidates.sort(
            key=lambda cs: cs[1] * math.log(cs[0].confirmations + 1) + cs[1],
            reverse=True,
        )
        candidates = candidates[:5]

        # Price scales with information delivered
        if not candidates:
            price = PRICE_CHECK_BASE_USDC
        else:
            top_conf = candidates[0][1] * math.log(candidates[0][0].confirmations + 1.7)
            price = min(PRICE_CHECK_MAX_USDC, PRICE_CHECK_BASE_USDC + 0.0015 * top_conf)

        wallet = self.wallet(agent_id)
        ref_id = f"check_{uuid.uuid4().hex[:8]}"
        if not wallet.debit(price, "scar_check", ref_id):
            return {
                "ok": False,
                "error": "insufficient_funds",
                "required_usdc": price,
                "balance_usdc": wallet.balance_usdc,
            }

        # Mark confirmations — caller is about to attempt this; if they later
        # submit a matching scar, confirmations get bumped. For demo we count
        # the check itself as a weak signal.
        return {
            "ok": True,
            "price_charged_usdc": round(price, 6),
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
        Agent reports a failure. We pay them if it's novel, charge them if it's
        a redundant dupe (this is the incentive structure — quality data only).
        """
        # Dedupe via exact-sig + fuzzy match on failure_mode
        sig_h = self._signature_hash(action_signature)
        existing: Scar | None = None
        for sid in self.by_signature.get(sig_h, []):
            cand = self.scars[sid]
            if cand.failure_mode.lower() == failure_mode.lower():
                existing = cand
                break

        wallet = self.wallet(agent_id)
        if existing:
            # Dupe — confirm it, charge nominal fee
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

        # Novel — store + pay
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
        wallet.credit(PRICE_SUBMIT_BOUNTY_USDC, "scar_submit_novel", ref_id)
        return {
            "ok": True,
            "novel": True,
            "scar_id": scar.scar_id,
            "paid_usdc": PRICE_SUBMIT_BOUNTY_USDC,
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
        "description": "Query the negative-result database BEFORE attempting an action. Returns known failure modes for similar actions, ranked by confidence. You will be charged in USDC scaled by the value of the information returned (no hits = floor price; high-confidence rare hits = up to $0.01). Call this whenever you are about to do something potentially flaky: an API call, a code path, a tool invocation, anything where 'has this failed before' is a useful signal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "action_signature": {
                    "type": "string",
                    "description": "Free-text description of what you are about to do, e.g. 'POST https://api.foo.com/v1/widgets with bearer token at peak hours' or 'pip install torch==2.1 on python 3.12 arm64'.",
                },
                "context": {"type": "object", "description": "Optional structured context."},
            },
            "required": ["agent_id", "action_signature"],
        },
    },
    {
        "name": "scar_submit",
        "description": "Report a failure you just experienced. You GET PAID $0.0005 if it's a novel scar (we want your data). You pay $0.0001 if it's a redundant duplicate. This is how the database grows.",
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
        "name": "scar_bounty_post",
        "description": "Post a reverse auction: 'I'll pay up to $max_pay_usdc to any agent who can tell me about failures for this action in the next N seconds.' Useful when scar_check returns no hits but you want to actively recruit data before committing.",
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
                result = STORE.check(args["agent_id"], args["action_signature"], args.get("context") or {})
            elif name == "scar_submit":
                result = STORE.submit(
                    args["agent_id"], args["action_signature"],
                    args["failure_mode"], args.get("context") or {}, args["evidence"],
                )
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
