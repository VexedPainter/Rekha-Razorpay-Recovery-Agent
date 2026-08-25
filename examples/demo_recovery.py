"""The closed loop, end to end: intent -> authorization -> execution -> outcome -> measurement.

Runs the whole thing offline in about a minute:

    1. The AI diagnoses 200 failed payments and proposes recoveries
    2. The control plane authorizes some, parks some for a human, refuses some
    3. Razorpay (the sandbox) creates the approved payment links
    4. Customers pay some of them -- and Razorpay says so via signed webhooks
    5. Webhooks are verified, deduplicated, and recorded
    6. Money recovered is MEASURED by folding two independent records together

Step 6 is the one that matters. Every number before it is something we did; the
recovered figure is what a customer actually paid, taken from a webhook rather
than from our own optimism. A created payment link is not recovered money, and
this demo keeps those two quantities visibly separate.

    python examples/demo_recovery.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB = "demo-recovery.db"
WEBHOOKS = "demo-webhooks.json"


def _run(*args: str, quiet: bool = False) -> str:
    """Run a real command and echo it, so nothing here is a printed claim."""
    printable = " ".join(args[1:]) if args[0] == sys.executable else " ".join(args)
    if not quiet:
        print(f"\n$ {printable}")
    completed = subprocess.run(
        args, capture_output=True, text=True, cwd=REPO_ROOT, timeout=600
    )
    if completed.returncode != 0 and not completed.stdout.strip():
        print(completed.stderr[-1500:])
        raise SystemExit(f"command failed: {printable}")
    if not quiet:
        print(completed.stdout.rstrip())
    return completed.stdout


def _rekha(*args: str, quiet: bool = False) -> str:
    return _run(sys.executable, "-m", "rekha.cli.main", *args, quiet=quiet)


def main() -> int:
    for stale in (DB, WEBHOOKS):
        (REPO_ROOT / stale).unlink(missing_ok=True)

    print("=" * 78)
    print("AI REVENUE RECOVERY -- the closed loop")
    print("=" * 78)
    print()
    print("  intent -> authorization -> execution -> actual outcome -> verification")

    # ---------------------------------------------------------------- 1 + 2 + 3
    print()
    print("-" * 78)
    print("STEP 1  The agent diagnoses failed payments and requests recoveries")
    print("-" * 78)
    print("  The AI proposes. It cannot execute, approve, or record -- every action")
    print("  goes out through the governed proxy, and the mandate decides.")
    recover_out = _rekha("recover", "--db", DB, "--provider", "replay")

    # ---------------------------------------------------------------------- 4
    print()
    print("-" * 78)
    print("STEP 2  Customers pay some of the links -- Razorpay signs the webhooks")
    print("-" * 78)
    print("  This script stands in for the customer AND for Razorpay's signer.")
    print("  The verifier below neither knows nor cares who signed: it checks an")
    print("  HMAC it did not produce against a body it did not write.")
    _run(
        sys.executable,
        "scripts/simulate_payments.py",
        "--db",
        DB,
        "--out",
        WEBHOOKS,
        "--rate",
        "0.6",
    )

    # ---------------------------------------------------------------------- 5
    print()
    print("-" * 78)
    print("STEP 3  Webhooks are verified, deduplicated, recorded")
    print("-" * 78)
    print("  Note the duplicate: Razorpay retries deliveries, and a retry must not")
    print("  be able to make one recovery look like two.")
    _rekha("webhooks", "replay", WEBHOOKS, "--db", DB)

    # ---------------------------------------------------------------------- 6
    print()
    print("-" * 78)
    print("STEP 4  MEASURED money recovered -- two independent records, folded")
    print("-" * 78)
    print("  `authorized` is what we asked for. `paid` is what a customer actually")
    print("  paid, according to a signed webhook. Only the second is recovery.")
    recoveries_out = _rekha("webhooks", "recoveries", "--db", DB)

    # ---------------------------------------------------------------- evidence
    print()
    print("-" * 78)
    print("STEP 5  The evidence chain")
    print("-" * 78)
    print("  Every decision above -- diagnosis, mandate check, policy verdict,")
    print("  approval, execution, webhook -- is one hash-chained event.")
    verify_out = _rekha("verify", DB)

    # ------------------------------------------------------------------ summary
    at_risk = _grep(recover_out, "revenue at risk")
    requested = _grep(recoveries_out, "amount requested")
    recovered = _grep(recoveries_out, "amount RECOVERED")
    count = _grep(recoveries_out, "recovered count")

    print()
    print("=" * 78)
    print("THE BATCH, MEASURED")
    print("=" * 78)
    for label, line in (
        ("revenue at risk", at_risk),
        ("requested", requested),
        ("RECOVERED", recovered),
        ("links recovered", count),
    ):
        if line:
            print(f"  {label:<18} {line.split(':', 1)[1].strip()}")

    print()
    print("  A created payment link is NOT recovered money. The recovered figure")
    print("  above comes from webhooks -- facts about what customers did, not")
    print("  claims about what we attempted.")
    print()
    for line in verify_out.splitlines():
        if line.startswith(("chain:", "coherence:", "events:")):
            print(f"  {line}")
    print()
    print(f"  Inspect it yourself:  rekha verify {DB}")
    print(f"                        rekha webhooks recoveries --db {DB}")
    return 0


def _grep(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            return line.strip()
    return ""


if __name__ == "__main__":
    sys.exit(main())
