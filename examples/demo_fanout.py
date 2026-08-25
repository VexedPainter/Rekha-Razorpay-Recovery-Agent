"""The fan-out attack, demonstrated: per-action limits do not bound an agent.

Forty payment links of Rs 4,000 each. Every single one passes the merchant's
Rs 5,000 per-action ceiling. Together they would spend Rs 1,60,000 against a
Rs 50,000 daily budget.

This is the scenario that shows why an aggregate limit is not a nice-to-have. A
control plane with only per-transaction caps would have allowed all forty, and
every individual decision would have looked correct.

Runs entirely offline against `examples/razorpay-sandbox` -- no credentials, no
network. Verified at the end against the sandbox's own state, so the count is
what actually exists rather than what we believe we did.

    python examples/demo_fanout.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from rekha.clock import SystemClock
from rekha.contracts.loader import load_contract_set
from rekha.errors import RekhaError
from rekha.finance.mandate import load_mandate
from rekha.finance.money import Money
from rekha.ledger.store import LedgerStore
from rekha.ledger.verify import verify_chain, verify_coherence
from rekha.policy.cumulative import CumulativeTracker
from rekha.policy.model import PolicyDoc, ToolRule
from rekha.proxy.lifecycle import Lifecycle

REPO_ROOT = Path(__file__).resolve().parents[1]
SANDBOX = REPO_ROOT / "examples" / "razorpay-sandbox" / "server.py"
PACK = REPO_ROOT / "packs" / "razorpay" / "contracts.yaml"
MANDATE = REPO_ROOT / "examples" / "mandates" / "merchant.yaml"

LINK_RUPEES = "2000.00"
ATTEMPTS = 40


def _describe(tool: str, args: dict[str, Any]) -> tuple[Money | None, str | None]:
    raw = args.get("amount")
    amount = (
        Money(minor_units=raw, currency=args.get("currency", "INR"))
        if isinstance(raw, int) and not isinstance(raw, bool)
        else None
    )
    return amount, args.get("method")


def _unwrap(raw: object) -> dict[str, Any]:
    structured = getattr(raw, "structuredContent", None)
    if isinstance(structured, dict):
        nested = structured.get("result", structured)
        return dict(nested) if isinstance(nested, dict) else {}
    return {}


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=[str(SANDBOX)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        async def upstream(tool: str, args: dict[str, Any]) -> Any:
            return await session.call_tool(tool, args)

        ledger = LedgerStore()
        mandate = load_mandate(MANDATE)
        each = Money.from_major(LINK_RUPEES, "INR")

        # The operator's policy is deliberately permissive here, so the ONLY
        # thing being demonstrated is the merchant's aggregate ceiling. With the
        # real packs/razorpay/policy.yaml these would also pause for approval,
        # which is correct but would obscure the point.
        lifecycle = Lifecycle(
            contract_set=load_contract_set([PACK]),
            unsafe_passthrough_tools=frozenset(),
            ledger=ledger,
            session_id="s_fanout_demo",
            policy=PolicyDoc(tools=[ToolRule(match="*", verdict="allow")]),
            mandate=mandate,
            action_describer=_describe,
        )
        lifecycle.start_session("recovery-agent", "acme_retail finance team")

        print("THE FAN-OUT ATTACK")
        print()
        print(f"  merchant mandate : {mandate.max_per_action} per action")
        print(f"                     {mandate.max_cumulative} per {mandate.window}")
        print(f"                     human approval above {mandate.approval_threshold}")
        print(f"  agent attempts   : {ATTEMPTS} payment links of {each} each")
        print(f"  total intended   : {each * ATTEMPTS}")
        print()
        print(f"  Each link is under the {mandate.max_per_action} per-action ceiling AND")
        print(f"  under the {mandate.approval_threshold} approval threshold -- so every")
        print("  single one is individually authorized with NO human review.")
        print(f"  Together they would spend {each * ATTEMPTS} against a")
        print(f"  {mandate.max_cumulative} budget.")
        print()

        created = 0
        refusal: RekhaError | None = None
        for index in range(ATTEMPTS):
            try:
                outcome = await lifecycle.govern_and_execute(
                    "create_payment_link",
                    {
                        "amount": each.minor_units,
                        "currency": "INR",
                        "description": f"Recovery attempt {index + 1}",
                        "reference_id": f"fanout-{index}",
                        "method": "upi",
                    },
                    read_only_hint=False,
                    executor=upstream,
                )
            except RekhaError as exc:
                refusal = exc
                print(f"  link {index + 1:>2}  REFUSED -- {exc.code}")
                break

            # Three distinct outcomes, and conflating them is a real bug class:
            # a `pending_approval` action has NOT executed and NOT spent anything.
            # Counting it as success is how a client ends up believing money moved
            # when it did not -- so the distinction is explicit here.
            if isinstance(outcome, dict) and outcome.get("status") == "pending_approval":
                print(f"  link {index + 1:>2}  PAUSED for human approval -- nothing spent")
                continue
            created += 1
            if index < 3 or index % 5 == 4:
                print(f"  link {index + 1:>2}  executed")

        assert refusal is not None, "the aggregate ceiling did not fire"
        print()
        print("  WHY:")
        print(f"    {refusal.detail['reason']}")
        print()
        print(f"    already spent : {refusal.detail['already_spent']}")
        print(f"    this action   : {refusal.detail['this_action']}")
        print(f"    ceiling       : {refusal.detail['limit']}")
        print(f"    window        : {refusal.detail['window']}")
        print(f"    mandate hash  : {refusal.detail['mandate_hash'][:24]}...")
        print()

        spent = CumulativeTracker(ledger).spent_by_merchant(
            mandate.merchant_id,
            currency=mandate.currency,
            now=SystemClock().now(),
            window=mandate.window_delta,
        )
        blocked = ATTEMPTS - created

        print("  OUTCOME")
        print(f"    links created   : {created}")
        print(f"    links blocked   : {blocked}")
        print(f"    actually spent  : {spent}")
        print(f"    would have spent: {each * ATTEMPTS}")
        print(f"    prevented       : {each * ATTEMPTS - spent}")
        print()

        # Verified against the sandbox itself, not against our own bookkeeping.
        links = _unwrap(await session.call_tool("fetch_all_payment_links", {}))
        on_server = [
            item
            for item in links.get("items", [])
            if str(item.get("reference_id", "")).startswith("fanout-")
        ]
        print(f"  VERIFIED on the Razorpay sandbox: {len(on_server)} links exist")
        assert len(on_server) == created, (
            f"ledger says {created}, server says {len(on_server)} -- these must agree"
        )

        events = ledger.read("s_fanout_demo")
        chain = verify_chain(events)
        coherence = verify_coherence(events)
        print(f"  ledger events: {len(events)}")
        print(f"  chain: {'OK' if chain.ok else 'BROKEN'}")
        print(f"  coherence: {'OK' if coherence.ok else 'BROKEN'}")
        print()
        print("  Per-action limits are not a budget. This is.")

    return 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
