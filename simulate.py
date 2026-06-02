"""
Scar Simulator — ILLUSTRATIVE model of the addiction loop.

This is a thought experiment in code, not an empirical benchmark. It shows
that the loop mechanically closes under stated assumptions; it does not show
that the assumptions hold against real-world workloads. The headline numbers
move directly with the knobs declared below — treat them as a sanity check
of the economics, not as performance evidence.

Two agents attempt the same fixed set of flaky tasks 50 times each:
  - control_agent:  no Scar. Tries, retries up to 2x on failure.
  - scar_agent:     queries Scar before acting, submits scars on failure,
                    and "adapts away" from a known failure with probability
                    SCAR_ADAPTATION_SUCCESS_RATE (declared below).

We measure success rate, simulated wall-clock cost, and Scar P&L.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from scar_server import STORE  # reuse the in-memory store directly

# ---------------------------------------------------------------------------
# DECLARED ASSUMPTIONS — every number below was picked by hand. Anyone
# evaluating the simulation should change these and re-run.
# ---------------------------------------------------------------------------

SCAR_ADAPTATION_SUCCESS_RATE = 0.92  # P(scar_agent succeeds | high-confidence hit seen)
KNOWN_HIT_CONFIDENCE = 0.9           # confidence threshold to treat a hit as "known"
KNOWN_HIT_MIN_CONFIRMATIONS = 1      # confirmations required to trust a hit
CONTROL_RETRY_BUDGET = 3             # control agent attempts per task before giving up
SCAR_RETRY_BUDGET = 3                # scar agent attempts per task if adaptation fails

# COST_PER_FAILURE_SECONDS and SECONDS_TO_USDC live below as code constants.
# `p_fail` per task is also hand-picked in the TASKS list.


# ---------------------------------------------------------------------------
# Simulated world: 12 tasks, each with known failure modes the agent doesn't
# know about upfront. Some failures are deterministic (will always recur),
# some probabilistic.
# ---------------------------------------------------------------------------

TASKS = [
    {
        "action": "POST https://api.weather.example/v3/forecast with key=A on weekends",
        "failure": ("auth_error", "API key A is silently rate-limited on weekends, returns 200 with empty body"),
        "p_fail": 1.0,
    },
    {
        "action": "pip install torch==2.1 on python 3.12 arm64 macOS",
        "failure": ("dependency_resolve", "No matching wheel; falls back to source build that takes 40min"),
        "p_fail": 1.0,
    },
    {
        "action": "GET https://api.stripe.example/v1/charges with idempotency key reused across days",
        "failure": ("schema_mismatch", "Returns cached response from prior day, looks current"),
        "p_fail": 0.9,
    },
    {
        "action": "SELECT * FROM events WHERE created_at > NOW() - INTERVAL '1 day' on read replica",
        "failure": ("replication_lag", "Read replica lags 6-20s during peak; misses recent rows"),
        "p_fail": 0.7,
    },
    {
        "action": "Run npm test in monorepo on Node 22 with workspace protocol",
        "failure": ("toolchain_break", "Node 22 + workspace:* hangs on circular dep"),
        "p_fail": 1.0,
    },
    {
        "action": "Upload 4GB file to S3 via boto3 default config",
        "failure": ("timeout", "Default multipart threshold drops connection at ~2.5GB"),
        "p_fail": 0.85,
    },
    {
        "action": "Call OpenAI gpt-4 with response_format json_object and no 'json' in prompt",
        "failure": ("validation_error", "API rejects request with 400, message buried in body"),
        "p_fail": 1.0,
    },
    {
        "action": "git rebase main onto feature branch with submodules",
        "failure": ("silent_corruption", "Submodule pointers reset to old SHA without warning"),
        "p_fail": 0.6,
    },
    {
        "action": "Send transactional email via SendGrid from new domain",
        "failure": ("deliverability", "Goes to spam for 72h until warmup completes"),
        "p_fail": 0.95,
    },
    {
        "action": "Deploy Lambda with 128MB memory and large numpy import",
        "failure": ("cold_start_oom", "OOM on cold start, succeeds on warm; intermittent"),
        "p_fail": 0.5,
    },
    {
        "action": "Run docker build with BuildKit on GitHub Actions ubuntu-latest",
        "failure": ("cache_miss", "Layer cache invalidated on every run despite buildx setup-action"),
        "p_fail": 0.8,
    },
    {
        "action": "Use Anthropic streaming API with usage tracking on cancelled requests",
        "failure": ("billing_anomaly", "Tokens still billed for stream chunks after client disconnect"),
        "p_fail": 1.0,
    },
]

COST_PER_FAILURE_SECONDS = 8.0   # avg wall-clock cost of one failure
SECONDS_TO_USDC = 0.0002         # internal price of 1s of agent wall-clock


# ---------------------------------------------------------------------------
# Agent behaviors
# ---------------------------------------------------------------------------

@dataclass
class AgentRun:
    name: str
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    seconds_wasted: float = 0.0
    scar_spent: float = 0.0
    scar_earned: float = 0.0

    @property
    def time_cost_usdc(self) -> float:
        return self.seconds_wasted * SECONDS_TO_USDC

    @property
    def net_cost_usdc(self) -> float:
        return self.time_cost_usdc + self.scar_spent - self.scar_earned

    @property
    def net_per_success(self) -> float:
        return self.net_cost_usdc / max(1, self.successes)


def attempt(task: dict) -> tuple[bool, str | None, str | None]:
    """Returns (succeeded, failure_mode, evidence)."""
    if random.random() < task["p_fail"]:
        fm, ev = task["failure"]
        return False, fm, ev
    return True, None, None


def run_control(n_tasks: int) -> AgentRun:
    """Naive agent: just tries, retries on failure."""
    run = AgentRun(name="control")
    for _ in range(n_tasks):
        task = random.choice(TASKS)
        run.attempts += 1
        for retry in range(CONTROL_RETRY_BUDGET):
            ok, _fm, _ev = attempt(task)
            if ok:
                run.successes += 1
                break
            run.failures += 1
            run.seconds_wasted += COST_PER_FAILURE_SECONDS
    return run


def run_scar(n_tasks: int, agent_id: str = "scar_agent_demo") -> AgentRun:
    """
    Scar-using agent:
      1. scar_check before acting
      2. if high-confidence hit, skip the attempt OR adapt
      3. if it does fail, scar_submit
    """
    run = AgentRun(name="scar")
    starting_balance = STORE.wallet(agent_id).balance_usdc

    for _ in range(n_tasks):
        task = random.choice(TASKS)
        run.attempts += 1

        # 1. Check
        check = STORE.check(agent_id, task["action"], {})
        if check.get("ok"):
            run.scar_spent += check.get("price_charged_usdc", 0)

        # 2. Decide. If we have a high-confidence hit on this exact failure
        #    mode, treat it as avoided (the agent adapts: different key,
        #    different config, different time, whatever).
        hits = check.get("hits", []) if check.get("ok") else []
        known = any(
            h["confidence"] >= KNOWN_HIT_CONFIDENCE
            and h["confirmations"] >= KNOWN_HIT_MIN_CONFIRMATIONS
            for h in hits
        )
        if known:
            # Agent "adapts" away from the known failure. The success rate of
            # adaptation is an *assumption* — see banner constants above.
            if random.random() < SCAR_ADAPTATION_SUCCESS_RATE:
                run.successes += 1
                continue

        # 3. Attempt (with retries) and submit on failure
        for retry in range(SCAR_RETRY_BUDGET):
            ok, fm, ev = attempt(task)
            if ok:
                run.successes += 1
                break
            run.failures += 1
            run.seconds_wasted += COST_PER_FAILURE_SECONDS
            if fm and ev:
                sub = STORE.submit(agent_id, task["action"], fm, {}, ev)
                if sub.get("ok"):
                    if sub.get("novel"):
                        run.scar_earned += sub.get("paid_usdc", 0)
                    else:
                        run.scar_spent += sub.get("price_charged_usdc", 0)

    ending_balance = STORE.wallet(agent_id).balance_usdc
    # Sanity check the ledger
    drift = (ending_balance - starting_balance) - (run.scar_earned - run.scar_spent)
    if abs(drift) > 1e-4:
        print(f"  [debug] ledger drift = {drift:.8f}  start={starting_balance}  end={ending_balance}  earned={run.scar_earned}  spent={run.scar_spent}")
    return run


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def print_run(run: AgentRun) -> None:
    print(f"  {run.name}:")
    print(f"    attempts:        {run.attempts}")
    print(f"    successes:       {run.successes} ({100*run.successes/max(1,run.attempts):.1f}%)")
    print(f"    failures:        {run.failures}")
    print(f"    seconds wasted:  {run.seconds_wasted:,.1f}s")
    print(f"    time cost:       ${run.time_cost_usdc:.4f}")
    print(f"    scar spent:      ${run.scar_spent:.4f}")
    print(f"    scar earned:     ${run.scar_earned:.4f}")
    print(f"    NET COST:        ${run.net_cost_usdc:.4f}")
    print(f"    net / success:   ${run.net_per_success:.5f}")


def main() -> None:
    random.seed(7)

    print("=" * 72)
    print("ILLUSTRATIVE SIMULATION — the numbers below are products of the")
    print("declared assumptions, not an empirical benchmark.")
    print("=" * 72)
    print(f"  SCAR_ADAPTATION_SUCCESS_RATE    = {SCAR_ADAPTATION_SUCCESS_RATE}")
    print(f"  KNOWN_HIT_CONFIDENCE            = {KNOWN_HIT_CONFIDENCE}")
    print(f"  KNOWN_HIT_MIN_CONFIRMATIONS     = {KNOWN_HIT_MIN_CONFIRMATIONS}")
    print(f"  CONTROL_RETRY_BUDGET            = {CONTROL_RETRY_BUDGET}")
    print(f"  SCAR_RETRY_BUDGET               = {SCAR_RETRY_BUDGET}")
    print(f"  COST_PER_FAILURE_SECONDS        = {COST_PER_FAILURE_SECONDS}")
    print(f"  p_fail per task: hand-picked in TASKS (see source)")
    print()

    # --- Phase 1: cold database. Scar agent has no prior data to draw on.
    print("=" * 72)
    print("PHASE 1 — cold database (50 tasks each, Scar starts empty)")
    print("=" * 72)
    control1 = run_control(50)
    scar1 = run_scar(50, agent_id="scar_phase1")
    print_run(control1)
    print()
    print_run(scar1)

    # --- Phase 2: hot database. 5 other agents have already seeded scars.
    print()
    print("=" * 72)
    print("PHASE 2 — warm database (after 5 other agents ran 30 tasks each)")
    print("=" * 72)
    for i in range(5):
        run_scar(30, agent_id=f"seeder_{i}")

    control2 = run_control(50)
    scar2 = run_scar(50, agent_id="scar_phase2")
    print_run(control2)
    print()
    print_run(scar2)

    # --- The headline numbers
    print()
    print("=" * 72)
    print("WHAT THE LOOP LOOKS LIKE")
    print("=" * 72)
    print()
    print(f"  AGENT SUCCESS RATE        cold-DB     warm-DB")
    print(f"    control (no Scar)         {100*control1.successes/control1.attempts:>4.0f}%        {100*control2.successes/control2.attempts:>4.0f}%")
    print(f"    Scar-using agent          {100*scar1.successes/scar1.attempts:>4.0f}%        {100*scar2.successes/scar2.attempts:>4.0f}%   <-- improves with DB age")
    print()
    print(f"  AGENT NET COST / SUCCESS")
    print(f"    control                 ${control1.net_per_success:.5f}    ${control2.net_per_success:.5f}")
    print(f"    Scar-using              ${scar1.net_per_success:.5f}    ${scar2.net_per_success:.5f}")
    print()
    scar_rev_phase2 = scar2.scar_spent - scar2.scar_earned
    scar_rev_per_query_phase2 = scar_rev_phase2 / max(1, scar2.attempts)
    scar_rev_phase1 = scar1.scar_spent - scar1.scar_earned
    scar_rev_per_query_phase1 = scar_rev_phase1 / max(1, scar1.attempts)
    print(f"  SCAR (US) REVENUE per agent-task")
    print(f"    cold-DB:                ${scar_rev_per_query_phase1:.5f}")
    print(f"    warm-DB:                ${scar_rev_per_query_phase2:.5f}   <-- WE earn more as DB grows")
    print(f"    revenue multiplier:     {scar_rev_per_query_phase2/max(1e-9,scar_rev_per_query_phase1):.2f}x")
    print()
    print(f"  TOTAL SCARS IN DB:        {len(STORE.scars)}")
    print()
    print("  The loop (under the declared assumptions):")
    print("    1. Agents fail. They submit scars (we pay them peanuts).")
    print("    2. The DB matures. Hits get higher-confidence.")
    print("    3. We charge MORE for those higher-confidence hits.")
    print("    4. Agents still net positive because skipping a failure is")
    print("       worth way more than the per-query fee.")
    print("    5. Removing Scar = measurable regression in agent success.")
    print()
    print("  Reality check: change the constants at the top of this file,")
    print("  re-run, and the loop bends accordingly. The point of the model")
    print("  is to make the economics auditable, not to claim performance.")


if __name__ == "__main__":
    main()
