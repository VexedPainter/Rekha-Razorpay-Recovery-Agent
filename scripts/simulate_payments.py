"""Simulate customers paying recovery links, and emit signed webhook envelopes.

This script plays two roles that are NOT part of the system under test, and says
so explicitly:

1. **The customer.** Calls the sandbox's `sandbox_simulate_payment`, which is a
   sandbox-only tool with no contract -- so the governed proxy would refuse it.
   Driven here, out-of-band, as the outside world.
2. **Razorpay's webhook signer.** HMAC-signs each resulting event with the
   configured secret.

Point 2 is worth being precise about. Signing our own webhooks does not make the
verification meaningless, because the verifier neither knows nor cares who signed:
it checks an HMAC it did not produce against a body it did not write. What this
does mean is that the *authenticity* of the underlying event is asserted by this
script rather than by Razorpay. In test mode, with a sandbox upstream, that is the
only option available -- and the README says so plainly rather than implying a
live webhook delivery that never happened.

What is genuinely proven offline: the signature check rejects tampering, the
deduplication survives a replay, the amounts reconcile, and the evidence chain
verifies. What is not: that Razorpay's real delivery format matches ours field for
field. The shapes are taken from Razorpay's documented payloads, and a live run
would confirm them.

    python scripts/simulate_payments.py --rate 0.6
"""

from __future__ import annotations

# Fail with an actionable message on Python < 3.12 rather than a SyntaxError raised
# from a valid line -- a bare python on PATH is commonly older than the venv's.
# See examples/_bootstrap.py.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / 'examples'))
import argparse
import json
import random
import sys
from pathlib import Path
from typing import Any

import _bootstrap  # noqa: F401  (imported for its side effect)
import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from recovery.providers import load_env  # noqa: E402
from rekha.ledger.store import LedgerStore  # noqa: E402
from rekha.razorpay.webhooks import sign_payload  # noqa: E402

SANDBOX = REPO_ROOT / "examples" / "razorpay-sandbox" / "server.py"


def _unwrap(raw: object) -> dict[str, Any]:
    structured = getattr(raw, "structuredContent", None)
    if isinstance(structured, dict):
        nested = structured.get("result", structured)
        return dict(nested) if isinstance(nested, dict) else {}
    return {}


def _links_from_ledger(db: Path) -> list[str]:
    """Payment links this system actually created, read from its own evidence."""
    ledger = LedgerStore(f"sqlite:///{db}")
    links: list[str] = []
    for event in ledger.read_by_types(["result_recorded"]):
        result = event.payload.get("result")
        if not isinstance(result, dict):
            continue
        link_id = result.get("id")
        if isinstance(link_id, str) and link_id.startswith("plink_") and link_id not in links:
            links.append(link_id)
    return links


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="recovery.db", help="Ledger with the created links.")
    parser.add_argument(
        "--out", default="webhooks.json", help="Where to write the signed envelopes."
    )
    parser.add_argument(
        "--rate",
        type=float,
        default=0.6,
        help="Fraction of links the customer pays. Not 1.0 by default: a recovery "
        "rate of 100%% is not a measurement, it is a demo.",
    )
    parser.add_argument("--seed", type=int, default=20260905, help="Deterministic selection.")
    parser.add_argument(
        "--recon-out",
        default="recon.json",
        help="Where to write the SETTLED leg (settlement reconciliation entries).",
    )
    parser.add_argument(
        "--inject",
        choices=["none", "unauthorized", "amount", "duplicate"],
        default="none",
        help="Corrupt the settlement data, to prove the verifier detects it: "
        "`unauthorized` settles money against a reference nobody approved, "
        "`amount` settles a different figure than was authorized, "
        "`duplicate` settles one authorization twice.",
    )
    args = parser.parse_args()

    secret = load_env().get("RAZORPAY_WEBHOOK_SECRET", "")
    if not secret:
        print("no RAZORPAY_WEBHOOK_SECRET in .env -- cannot sign webhooks")
        return 2

    links = _links_from_ledger(REPO_ROOT / args.db)
    if not links:
        print(f"no payment links found in {args.db} -- run `rekha recover` first")
        return 2

    rng = random.Random(args.seed)
    paying = [link for link in links if rng.random() < args.rate]

    print(f"links created by the agent : {len(links)}")
    print(f"customers who pay          : {len(paying)}  (rate {args.rate})")
    print()

    envelopes: list[dict[str, str]] = []
    params = StdioServerParameters(command=sys.executable, args=[str(SANDBOX)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        # The sandbox is a fresh process, so the links the agent created are not in
        # it. Recreate each one by its reference id -- creation is idempotent on
        # reference_id, so this reproduces the same link ids deterministically.
        ledger = LedgerStore(f"sqlite:///{REPO_ROOT / args.db}")
        for event in ledger.read_by_types(["tool_called"]):
            tool = event.payload.get("tool")
            call_args = event.payload.get("args")
            if not isinstance(tool, str) or not tool.startswith("create_payment_link"):
                continue
            if isinstance(call_args, dict):
                await session.call_tool(tool, call_args)

        paid_payment_ids: list[str] = []
        for link_id in paying:
            result = _unwrap(
                await session.call_tool(
                    "sandbox_simulate_payment", {"payment_link_id": link_id}
                )
            )
            webhook = result.get("webhook")
            if not isinstance(webhook, dict):
                print(f"  {link_id}: no webhook produced (already paid?)")
                continue

            # The exact raw string that gets signed. Stored verbatim, because HMAC
            # is over bytes and re-serializing would change whitespace and
            # invalidate the signature.
            body = json.dumps(webhook, separators=(",", ":"), sort_keys=True)
            event_id = f"evt_{link_id[-12:]}"
            envelopes.append(
                {
                    "event_id": event_id,
                    "signature": sign_payload(body, secret),
                    "body": body,
                }
            )
            payment = result.get("payment")
            if isinstance(payment, dict) and isinstance(payment.get("id"), str):
                paid_payment_ids.append(payment["id"])
            amount = webhook["payload"]["payment_link"]["entity"]["amount_paid"]
            print(f"  {link_id} paid INR {amount / 100:,.2f}  -> {event_id}")

        # ------------------------------------------------------ the SETTLED leg
        #
        # Settle the payments that were made, producing itemised reconciliation
        # entries with realistic fee and GST deductions. This is the third,
        # independent leg the verifier needs.
        print()
        recon: list[dict[str, Any]] = []
        if paid_payment_ids:
            settled = _unwrap(
                await session.call_tool("sandbox_settle", {"payment_ids": paid_payment_ids})
            )
            print(f"  settled {settled.get('settled', 0)} payment(s)")
            recon = _unwrap(
                await session.call_tool("fetch_settlement_recon_details", {})
            ).get("items", [])

        if args.inject == "unauthorized":
            # Money settled against a recovery reference nobody approved. Only
            # detectable from a source we do not author -- the case three-way
            # verification exists for.
            injected = _unwrap(
                await session.call_tool(
                    "sandbox_inject_unauthorized_settlement", {"amount": 5000000}
                )
            ).get("injected", {})
            injected["reference_id"] = "recover-pay_NEVER_AUTHORIZED"
            recon.append(injected)
            print("  INJECTED: INR 50,000.00 settled against an unapproved reference")
        elif args.inject == "amount" and recon:
            original = recon[0]["amount"]
            recon[0]["amount"] = original + 150000
            print(
                f"  INJECTED: entry 0 settled INR {recon[0]['amount'] / 100:,.2f} "
                f"instead of INR {original / 100:,.2f}"
            )
        elif args.inject == "duplicate" and recon:
            recon.append(dict(recon[0]))
            print("  INJECTED: one authorization settled twice")

        recon_path = REPO_ROOT / args.recon_out
        recon_path.write_text(json.dumps(recon, indent=2), encoding="utf-8")
        print(f"  wrote {len(recon)} recon entry(ies) to {args.recon_out}")

    # One duplicate, on purpose: Razorpay retries deliveries, so the replay path
    # must be exercised against a genuine duplicate rather than only a clean run.
    if envelopes:
        envelopes.append(dict(envelopes[0]))
        print(f"\n  + 1 duplicate delivery of {envelopes[0]['event_id']} (Razorpay retries)")

    out = REPO_ROOT / args.out
    out.write_text(json.dumps(envelopes, indent=2), encoding="utf-8")
    print(f"\nwrote {len(envelopes)} envelope(s) to {args.out}")
    print(f"ingest with: rekha webhooks replay {args.out} --db {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
