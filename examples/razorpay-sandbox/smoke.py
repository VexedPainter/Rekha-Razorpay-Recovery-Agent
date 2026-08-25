"""Smoke-check the Razorpay sandbox over a real stdio MCP session.

Not a test -- a development aid, run directly, that answers "does the sandbox
actually speak MCP and do its tools behave?" without the pytest harness in the
way. Deleted-safe: nothing imports it.

    python examples/razorpay-sandbox/smoke.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

SERVER = Path(__file__).parent / "server.py"


def _result(raw: object) -> dict:
    structured = getattr(raw, "structuredContent", None)
    if isinstance(structured, dict):
        return structured.get("result", structured)
    return {}


async def main() -> int:
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        tools = await session.list_tools()
        names = sorted(t.name for t in tools.tools)
        print(f"tools advertised ({len(names)}):")
        for name in names:
            annotations = next(t.annotations for t in tools.tools if t.name == name)
            read_only = bool(annotations and annotations.readOnlyHint)
            print(f"  {'[read]' if read_only else '[WRITE]'} {name}")

        print("\n-- fetch_all_payments(status='failed') --")
        payments = _result(
            await session.call_tool("fetch_all_payments", {"status": "failed", "count": 200})
        )
        items = payments["items"]
        at_risk = sum(int(p["amount"]) for p in items)
        print(f"   {payments['count']} failed, INR {at_risk / 100:,.2f} at risk")

        target = items[0]
        print(f"\n-- fetch_payment({target['id']}) --")
        detail = _result(await session.call_tool("fetch_payment", {"payment_id": target["id"]}))
        print(f"   {detail['error_reason']} / {detail['error_step']}")
        print(f"   {detail['error_description']}")

        print("\n-- create_payment_link (idempotent on reference_id) --")
        first = _result(
            await session.call_tool(
                "create_payment_link",
                {
                    "amount": 240000,
                    "currency": "INR",
                    "description": "Recovery for order #34001",
                    "reference_id": "recover-pay_test-1",
                },
            )
        )
        second = _result(
            await session.call_tool(
                "create_payment_link",
                {
                    "amount": 240000,
                    "currency": "INR",
                    "description": "Recovery for order #34001",
                    "reference_id": "recover-pay_test-1",
                },
            )
        )
        print(f"   link      {first['id']}  status={first['status']}")
        print(f"   short_url {first['short_url']}")
        print(f"   replayed  {second['id']}  same id: {first['id'] == second['id']}")

        print("\n-- cancel (the undo) --")
        cancelled = _result(
            await session.call_tool("cancel_payment_link", {"payment_link_id": first["id"]})
        )
        print(f"   status={cancelled['status']}")

        print("\n-- pay a link, then try to cancel it (conditional undo) --")
        payable = _result(
            await session.call_tool(
                "create_payment_link",
                {"amount": 180000, "currency": "INR", "reference_id": "recover-pay_test-2"},
            )
        )
        paid = _result(
            await session.call_tool(
                "sandbox_simulate_payment", {"payment_link_id": payable["id"]}
            )
        )
        print(f"   link status={paid['payment_link']['status']}")
        print(f"   webhook event={paid['webhook']['event']}")
        refused = _result(
            await session.call_tool("cancel_payment_link", {"payment_link_id": payable["id"]})
        )
        print(f"   cancel refused: {json.dumps(refused.get('error', {}))[:90]}")

        print("\n-- settle, then read recon (leg 3) --")
        settled = _result(
            await session.call_tool(
                "sandbox_settle", {"payment_ids": [paid["payment"]["id"]]}
            )
        )
        print(f"   settled {settled['settled']} txn, net {settled['settlement']['amount']}")
        recon = _result(await session.call_tool("fetch_settlement_recon_details", {}))
        entry = recon["items"][0]
        print(
            f"   recon: amount={entry['amount']} fee={entry['fee']} "
            f"tax={entry['tax']} credit={entry['credit']}"
        )

        print("\n-- inject an unauthorized settlement --")
        injected = _result(
            await session.call_tool("sandbox_inject_unauthorized_settlement", {"amount": 5000000})
        )
        print(f"   {injected['injected']['entity_id']} credit={injected['injected']['credit']}")

        print("\nsandbox OK")
    return 0


if __name__ == "__main__":
    sys.exit(anyio.run(main))
