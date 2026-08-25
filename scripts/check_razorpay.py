"""Preflight: what does this Razorpay TEST MODE account actually expose?

Run before building against the live API, because two answers change design
decisions and both are cheaper to learn now than at Phase 8:

1. **Do the credentials work at all?** A 401 here is a typo in `.env`, not a
   bug in anything we wrote.
2. **Does test mode return settlement data?** `rekha/settlement/` reconciles
   three legs -- what we authorized, what Razorpay reported, and what actually
   settled. If test mode never produces settlements, the third leg has to be
   fixture-driven, and the README must say so plainly rather than implying a
   live reconciliation that never ran.

Read-only: every call is a GET. Nothing is created, nothing is charged.

Credentials come from `.env` (gitignored) or the real environment. Secrets are
never printed -- only the key id, and only its non-secret prefix.

    python scripts/check_razorpay.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.razorpay.com/v1"
REPO_ROOT = Path(__file__).resolve().parents[1]

#: (label, path, what a result tells us). Read-only endpoints only.
PROBES: list[tuple[str, str, str]] = [
    ("credentials", "/payments?count=1", "authentication works"),
    ("failed payments", "/payments?count=100", "the recovery cohort"),
    ("payment links", "/payment_links", "recovery action surface"),
    ("orders", "/orders?count=1", "order context for diagnosis"),
    ("settlements", "/settlements?count=10", "SETTLEMENT VERIFICATION leg 3"),
    ("settlement recon", "/settlements/recon/combined?year=2026&month=8", "recon detail"),
]


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal `.env` reader. No dependency, no interpolation, no surprises."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def get(path: str, key_id: str, key_secret: str) -> tuple[int, dict | None, str]:
    """GET `path`, returning `(status, parsed_body, message)`. Never raises."""
    token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    request = urllib.request.Request(
        f"{API}{path}",
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode()), "ok"
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            parsed = json.loads(body)
            message = parsed.get("error", {}).get("description", body[:200])
        except json.JSONDecodeError:
            message = body[:200]
        return exc.code, None, message
    except Exception as exc:  # network, DNS, TLS
        return 0, None, f"{type(exc).__name__}: {exc}"


def main() -> int:
    env = {**load_dotenv(REPO_ROOT / ".env"), **os.environ}
    key_id = env.get("RAZORPAY_KEY_ID", "")
    key_secret = env.get("RAZORPAY_KEY_SECRET", "")

    if not key_id or key_id.startswith("rzp_test_xxx") or "xxx" in key_secret:
        print("No Razorpay credentials found.\n")
        print("  1. copy .env.example to .env")
        print("  2. fill in RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET from")
        print("     dashboard.razorpay.com -> Test Mode -> Settings -> API Keys")
        print("  3. run this again\n")
        return 2

    if not key_id.startswith("rzp_test_"):
        print(f"REFUSING TO RUN: key id {key_id[:12]}... is not a test key.")
        print("This project is test-mode only. Never use live keys here.")
        return 3

    print(f"Razorpay TEST MODE preflight  (key {key_id[:16]}...)\n")

    results: dict[str, tuple[int, int | None]] = {}
    for label, path, why in PROBES:
        status, body, message = get(path, key_id, key_secret)
        count = None
        if body is not None:
            items = body.get("items")
            count = body.get("count") if isinstance(body.get("count"), int) else None
            if count is None and isinstance(items, list):
                count = len(items)
        results[label] = (status, count)

        if status == 200:
            detail = f"{count} record(s)" if count is not None else "ok"
            print(f"  [ok]    {label:<20} {detail:<18} ({why})")
        elif status in (401, 403):
            print(f"  [AUTH]  {label:<20} {status} -- {message}")
        else:
            print(f"  [--]    {label:<20} {status} -- {message[:80]}")

    print()

    auth_status = results["credentials"][0]
    if auth_status in (401, 403):
        print("VERDICT: credentials rejected. Check for a typo, or regenerate the")
        print("test key (the secret is only shown once at creation).")
        return 1
    if auth_status != 200:
        print("VERDICT: could not reach the Razorpay API. Check the network.")
        return 1

    failed_count = results["failed payments"][1]
    settlement_status, settlement_count = results["settlements"]

    print("VERDICT")
    print("  credentials            work")
    print(f"  payments visible       {failed_count if failed_count is not None else 'unknown'}")

    if settlement_status == 200 and (settlement_count or 0) > 0:
        print(f"  settlement data        YES ({settlement_count}) -- leg 3 can run LIVE")
    elif settlement_status == 200:
        print("  settlement data        endpoint works but returned 0 records")
        print("                         -> leg 3 runs FIXTURE-BACKED; say so in the README")
    else:
        print(f"  settlement data        NOT AVAILABLE ({settlement_status})")
        print("                         -> leg 3 runs FIXTURE-BACKED; say so in the README")

    if not failed_count:
        print()
        print("  NOTE: no payments in this account yet. That is fine -- the seeded")
        print("  sandbox (examples/razorpay-sandbox) provides the failed-payment")
        print("  cohort for the demo. Live mode is only used to prove one real")
        print("  payment link can be created under policy control.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
