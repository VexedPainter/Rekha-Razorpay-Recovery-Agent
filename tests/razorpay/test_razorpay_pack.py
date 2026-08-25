"""`packs/razorpay/` against the real sandbox MCP server.

Two kinds of test here, and the distinction matters:

- **Drift tests** compare the pack against what the server actually advertises.
  A contract pack that has silently diverged from its upstream is worse than no
  pack, because `resolve()` would refuse real tools and permit renamed ones.
- **Acceptance tests** drive a real stdio MCP session through the full governed
  lifecycle, the way `tests/executor/test_crm_mock_acceptance.py` does for the
  CRM mock. Marked `slow`: they spawn a subprocess.

The test that earns its place most is
`test_a_paid_link_registers_as_irreversible`. It is the reason link creation is
declared `conditional` rather than `reversible`: the undo exists, and it stops
working the moment the customer pays. A system that reported "fully rewound"
after failing to cancel a paid link would be lying about money.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from rekha.contracts.loader import load_contract_set
from rekha.contracts.model import ContractSet
from rekha.errors import RekhaError
from rekha.finance.mandate import load_mandate
from rekha.finance.money import Money
from rekha.ledger.store import LedgerStore
from rekha.ledger.verify import verify_chain, verify_coherence
from rekha.policy.model import load_policy
from rekha.proxy.lifecycle import Lifecycle

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK = REPO_ROOT / "packs" / "razorpay" / "contracts.yaml"
POLICY = REPO_ROOT / "packs" / "razorpay" / "policy.yaml"
MANDATE = REPO_ROOT / "examples" / "mandates" / "merchant.yaml"
SANDBOX = REPO_ROOT / "examples" / "razorpay-sandbox" / "server.py"

#: Tools the sandbox exposes for test orchestration only. Deliberately absent
#: from the pack, so the governed proxy refuses them -- the demo drives them
#: out-of-band, as the outside world, never as the agent.
SANDBOX_ONLY_PREFIX = "sandbox_"


def _contracts() -> ContractSet:
    return load_contract_set([PACK])


def _describe(tool: str, args: dict[str, Any]) -> tuple[Money | None, str | None]:
    """The Razorpay action describer: amount in minor units, method by name."""
    amount = None
    raw = args.get("amount")
    if isinstance(raw, int) and not isinstance(raw, bool):
        amount = Money(minor_units=raw, currency=args.get("currency", "INR"))
    return amount, args.get("method") or None


def _result(raw: object) -> dict[str, Any]:
    structured = getattr(raw, "structuredContent", None)
    if isinstance(structured, dict):
        nested = structured.get("result", structured)
        return dict(nested) if isinstance(nested, dict) else {}
    return {}


# --------------------------------------------------------------- static checks


def test_the_pack_loads_and_pins_a_set_hash() -> None:
    contracts = _contracts()
    assert len(contracts.contracts) == 13
    assert contracts.set_hash.startswith("sha256:")


def test_the_set_hash_is_stable_across_loads() -> None:
    """A session pins `set_hash`; it must not change between processes."""
    assert _contracts().set_hash == _contracts().set_hash


def test_payment_link_creation_is_conditional_not_reversible() -> None:
    """Financially exact: the undo only works while the link is unpaid."""
    for tool in ("create_payment_link", "create_payment_link_upi"):
        contract = _contracts().resolve(tool)
        assert contract is not None
        assert contract.reversibility == "conditional"
        assert contract.undo is not None
        assert contract.undo.tool == "cancel_payment_link"
        assert contract.conditions == ["$result.status != 'paid'"]


def test_a_refund_is_irreversible_and_declares_no_undo() -> None:
    contract = _contracts().resolve("create_refund")
    assert contract is not None
    assert contract.reversibility == "irreversible"
    assert contract.undo is None


def test_every_read_tool_declares_only_read_effects() -> None:
    """A `fetch_*` that declared a write effect would be a contract bug, and
    `policy.yaml` allows `fetch_*` outright -- so this is load-bearing."""
    for name, contract in _contracts().contracts.items():
        if not name.startswith("fetch_"):
            continue
        assert {e.type for e in contract.effects} == {"read"}, name


def test_every_money_moving_action_derives_its_amount_from_arguments() -> None:
    """A `spend` effect with no amount is invisible to every cap meant to bound it."""
    for name in ("create_payment_link", "create_payment_link_upi", "create_refund"):
        contract = _contracts().resolve(name)
        assert contract is not None
        spend = [e for e in contract.effects if e.type == "spend"]
        assert spend, name
        assert spend[0].amount_from is not None, name
        assert spend[0].amount_from.minor_units == "$args.amount"


def test_every_mutating_action_declares_an_idempotency_key() -> None:
    """So a retried recovery cannot create a second link or a second refund."""
    for name, contract in _contracts().contracts.items():
        if name.startswith("fetch_") or name == "cancel_payment_link":
            continue
        assert contract.idempotency_key is not None, name


def test_customer_contact_details_are_redacted_from_evidence() -> None:
    for name in ("create_payment_link", "create_payment_link_upi"):
        contract = _contracts().resolve(name)
        assert contract is not None
        assert contract.redact is not None
        assert "customer.contact" in contract.redact
        assert "customer.email" in contract.redact


def test_no_contract_claims_to_be_verified() -> None:
    """These are tested against the sandbox, not against a real settled payment.
    Claiming `verified: true` would be a claim we have not earned."""
    for name, contract in _contracts().contracts.items():
        assert contract.provenance is not None, name
        assert contract.provenance.verified is False, name


def test_the_capture_tool_of_every_contract_is_read_only() -> None:
    """`SagaExecutor` enforces this at runtime; catching it here is cheaper."""
    contracts = _contracts()
    for name, contract in contracts.contracts.items():
        if contract.capture is None:
            continue
        capture_contract = contracts.resolve(contract.capture.tool)
        assert capture_contract is not None, f"{name} captures an undeclared tool"
        assert {e.type for e in capture_contract.effects} == {"read"}, name


def test_the_mandate_permits_only_contracted_actions() -> None:
    """A mandate authorizing an action with no contract would be incoherent: the
    merchant would have granted something the system can never do."""
    contracts = _contracts()
    mandate = load_mandate(MANDATE)
    for action in mandate.allowed_actions:
        assert contracts.resolve(action) is not None, action
    for action in mandate.forbidden_actions:
        assert contracts.resolve(action) is not None, (
            f"{action} is forbidden but has no contract -- the prompt-injection "
            f"scenario requires the tool to genuinely exist and work"
        )


def test_the_policy_document_loads_and_denies_refunds() -> None:
    """Two locks on the same door: the mandate forbids refunds AND policy denies
    any refund spend, independently."""
    policy = load_policy(POLICY)
    refund_caps = [
        cap
        for cap in policy.caps
        if cap.match.resource == "razorpay.refund" and cap.over == "deny"
    ]
    assert refund_caps, "policy must deny refunds independently of the mandate"


def test_sandbox_only_tools_are_absent_from_the_pack() -> None:
    """They must be `contract_missing` for the agent, and driven out-of-band."""
    for name in _contracts().contracts:
        assert not name.startswith(SANDBOX_ONLY_PREFIX), name


# ------------------------------------------------------------------ drift check


@pytest.mark.slow
@pytest.mark.anyio
async def test_the_pack_matches_what_the_sandbox_actually_advertises() -> None:
    """Drift: every real tool has a contract, every contract names a real tool."""
    params = StdioServerParameters(command=sys.executable, args=[str(SANDBOX)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        advertised = {tool.name for tool in listed.tools}
        read_only = {
            tool.name
            for tool in listed.tools
            if tool.annotations and tool.annotations.readOnlyHint
        }

    contracted = set(_contracts().contracts)
    real = {name for name in advertised if not name.startswith(SANDBOX_ONLY_PREFIX)}

    assert contracted == real, (
        f"pack/server drift -- only in pack: {sorted(contracted - real)}; "
        f"only on server: {sorted(real - contracted)}"
    )

    # A tool the server calls read-only must not be contracted as a mutation,
    # and vice versa: `resolve()` trusts `readOnlyHint` for uncontracted tools,
    # so a disagreement here is a real authorization inconsistency.
    for name, contract in _contracts().contracts.items():
        declared_read_only = {e.type for e in contract.effects} == {"read"}
        assert declared_read_only == (name in read_only), name


# ------------------------------------------------------------ acceptance: saga


class _Upstream:
    """A real MCP session, exposed as the executor callable the lifecycle wants."""

    def __init__(self, session: ClientSession) -> None:
        self._session = session
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, tool: str, args: dict[str, Any]) -> Any:
        self.calls.append((tool, args))
        return await self._session.call_tool(tool, args)


def _lifecycle(ledger: LedgerStore, session_id: str, *, permissive: bool) -> Lifecycle:
    """`permissive` swaps the real policy for one that allows, so a test can
    isolate contract/saga behaviour from the approval flow that the real policy
    (correctly) imposes on every irreversible money-moving action."""
    from rekha.policy.model import PolicyDoc, ToolRule

    policy = (
        PolicyDoc(tools=[ToolRule(match="*", verdict="allow")])
        if permissive
        else load_policy(POLICY)
    )
    return Lifecycle(
        contract_set=_contracts(),
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id=session_id,
        policy=policy,
        mandate=load_mandate(MANDATE),
        action_describer=_describe,
    )


@pytest.mark.slow
@pytest.mark.anyio
async def test_an_unpaid_link_is_created_then_compensated() -> None:
    """The happy reversible path: create a real link, then undo it."""
    params = StdioServerParameters(command=sys.executable, args=[str(SANDBOX)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        upstream = _Upstream(session)
        ledger = LedgerStore()
        lifecycle = _lifecycle(ledger, "s_pack_undo", permissive=True)
        lifecycle.start_session("recovery-agent")

        await lifecycle.govern_and_execute(
            "create_payment_link",
            {
                "amount": 240000,
                "currency": "INR",
                "description": "Recovery for order #34001",
                "reference_id": "acc-undo-1",
                "method": "upi",
            },
            read_only_hint=False,
            executor=upstream,
        )

        events = ledger.read("s_pack_undo")
        compensation = next(e for e in events if e.type == "compensation_registered")
        assert compensation.payload["reversible"] is True
        assert compensation.payload["tool"] == "cancel_payment_link"
        assert compensation.payload["args"]["payment_link_id"].startswith("plink_")

        # Run the compensation for real and confirm the link is actually cancelled.
        link_id = compensation.payload["args"]["payment_link_id"]
        await session.call_tool("cancel_payment_link", {"payment_link_id": link_id})
        after = _result(await session.call_tool("fetch_payment_link", {"payment_link_id": link_id}))
        assert after["status"] == "cancelled"

        assert verify_chain(events).ok
        assert verify_coherence(events).ok


@pytest.mark.slow
@pytest.mark.anyio
async def test_a_paid_link_registers_as_irreversible() -> None:
    """The reason link creation is `conditional`.

    The undo exists and stops working the moment the customer pays. Reporting a
    paid link as reversible would mean claiming an undo that fails -- so the
    condition is evaluated at commit time and the step honestly registers as not
    reversible instead.
    """
    params = StdioServerParameters(command=sys.executable, args=[str(SANDBOX)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        ledger = LedgerStore()
        lifecycle = _lifecycle(ledger, "s_pack_paid", permissive=True)
        lifecycle.start_session("recovery-agent")

        # Pre-create and pay a link, then have the governed call return it (the
        # sandbox is idempotent on reference_id, mirroring the real API).
        created = _result(
            await session.call_tool(
                "create_payment_link",
                {"amount": 180000, "currency": "INR", "reference_id": "acc-paid-1"},
            )
        )
        await session.call_tool(
            "sandbox_simulate_payment", {"payment_link_id": created["id"]}
        )

        await lifecycle.govern_and_execute(
            "create_payment_link",
            {
                "amount": 180000,
                "currency": "INR",
                "reference_id": "acc-paid-1",
                "method": "upi",
            },
            read_only_hint=False,
            executor=_Upstream(session),
        )

        compensation = next(
            e for e in ledger.read("s_pack_paid") if e.type == "compensation_registered"
        )
        assert compensation.payload["reversible"] is False
        assert compensation.payload["reason"] == "conditional_unmet"


@pytest.mark.slow
@pytest.mark.anyio
async def test_a_replayed_recovery_calls_the_upstream_once() -> None:
    """Idempotency, observable end to end: one link, not two.

    The contract's `idempotency_key` is `$args.reference_id`, so the second
    governed call is served from the idempotency store and the upstream is never
    reached a second time.
    """
    params = StdioServerParameters(command=sys.executable, args=[str(SANDBOX)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        upstream = _Upstream(session)
        ledger = LedgerStore()
        lifecycle = _lifecycle(ledger, "s_pack_idem", permissive=True)
        lifecycle.start_session("recovery-agent")

        args = {
            "amount": 150000,
            "currency": "INR",
            "reference_id": "acc-idem-1",
            "method": "upi",
        }
        await lifecycle.govern_and_execute(
            "create_payment_link", dict(args), read_only_hint=False, executor=upstream
        )
        await lifecycle.govern_and_execute(
            "create_payment_link", dict(args), read_only_hint=False, executor=upstream
        )

        creations = [c for c in upstream.calls if c[0] == "create_payment_link"]
        assert len(creations) == 1, "the upstream was called twice for one recovery"

        links = _result(await session.call_tool("fetch_all_payment_links", {}))
        matching = [
            item for item in links["items"] if item.get("reference_id") == "acc-idem-1"
        ]
        assert len(matching) == 1


# ------------------------------------------------- acceptance: default-deny


@pytest.mark.slow
@pytest.mark.anyio
async def test_a_sandbox_only_tool_is_refused_as_contract_missing() -> None:
    """Default-deny (spec §4.6) proven against the real server: the tool exists
    and works, and the agent still cannot reach it."""
    params = StdioServerParameters(command=sys.executable, args=[str(SANDBOX)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        upstream = _Upstream(session)
        ledger = LedgerStore()
        lifecycle = _lifecycle(ledger, "s_pack_deny", permissive=True)
        lifecycle.start_session("recovery-agent")

        with pytest.raises(RekhaError) as excinfo:
            await lifecycle.govern_and_execute(
                "sandbox_inject_unauthorized_settlement",
                {"amount": 5000000},
                read_only_hint=False,
                executor=upstream,
            )
        assert excinfo.value.code in ("mandate_violation", "contract_missing")
        assert upstream.calls == []


@pytest.mark.slow
@pytest.mark.anyio
async def test_below_the_mandate_threshold_recovery_is_fully_autonomous() -> None:
    """Rs 2,400 is under the mandate's Rs 2,500 threshold and under every policy
    cap, so it executes with no human in the loop.

    This path matters as much as the refusals: a control plane that queues every
    action for approval has not automated anything, it has just added a step.
    """
    params = StdioServerParameters(command=sys.executable, args=[str(SANDBOX)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        upstream = _Upstream(session)
        ledger = LedgerStore()
        lifecycle = _lifecycle(ledger, "s_pack_auto", permissive=False)
        lifecycle.start_session("recovery-agent")

        raw = await lifecycle.govern_and_execute(
            "create_payment_link",
            {
                "amount": 240000,
                "currency": "INR",
                "reference_id": "acc-auto-1",
                "method": "upi",
            },
            read_only_hint=False,
            executor=upstream,
        )

        link = _result(raw)
        assert link["status"] == "created"
        assert link["short_url"].startswith("https://rzp.io/i/")
        assert len(upstream.calls) == 1

        events = ledger.read("s_pack_auto")
        verdict = next(e for e in events if e.type == "policy_evaluated")
        assert verdict.payload["verdict"] == "allow"
        assert any(e.type == "step_committed" for e in events)
        assert verify_chain(events).ok
        assert verify_coherence(events).ok


@pytest.mark.slow
@pytest.mark.anyio
async def test_above_the_mandate_threshold_a_human_must_approve() -> None:
    """Rs 3,000 is over the mandate's Rs 2,500 approval threshold.

    Policy alone would have allowed it -- it is under the operator's Rs 10,000
    cap and the action is `conditional`, not `irreversible`. The escalation comes
    from the MERCHANT's mandate, and it must be able to escalate `allow` to
    `pause` independently of the operator's policy.
    """
    params = StdioServerParameters(command=sys.executable, args=[str(SANDBOX)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        upstream = _Upstream(session)
        ledger = LedgerStore()
        lifecycle = _lifecycle(ledger, "s_pack_pause", permissive=False)
        lifecycle.start_session("recovery-agent")

        outcome = await lifecycle.govern_and_execute(
            "create_payment_link",
            {
                "amount": 300000,
                "currency": "INR",
                "reference_id": "acc-pause-1",
                "method": "upi",
            },
            read_only_hint=False,
            executor=upstream,
        )

        assert isinstance(outcome, dict)
        assert outcome["status"] == "pending_approval"
        assert outcome["approval_id"].startswith("ap_")
        assert upstream.calls == [], "a paused action must not reach Razorpay"

        verdict = next(
            e for e in ledger.read("s_pack_pause") if e.type == "policy_evaluated"
        )
        assert verdict.payload["verdict"] == "pause"
        assert any("mandate.approval_threshold" in r for r in verdict.payload["reasons"])


@pytest.mark.slow
@pytest.mark.anyio
async def test_over_the_operator_policy_cap_is_denied_outright() -> None:
    """Rs 12,000 exceeds the operator's Rs 10,000 `deny` cap.

    The mandate would already have refused it at Rs 5,000; this proves the
    operator's policy is an independent backstop that holds even if a mandate
    were written too permissively. Two locks, different keyholders.
    """
    from rekha.finance.mandate import MerchantMandate

    params = StdioServerParameters(command=sys.executable, args=[str(SANDBOX)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        upstream = _Upstream(session)
        ledger = LedgerStore()

        # A deliberately over-permissive mandate, to isolate the policy cap.
        loose = MerchantMandate.model_validate(
            {
                "merchant_id": "acme_retail",
                "currency": "INR",
                "allowed_actions": ["create_payment_link"],
                "max_per_action": {"major": "100000.00", "currency": "INR"},
                "max_cumulative": {"major": "100000.00", "currency": "INR"},
            }
        )
        lifecycle = Lifecycle(
            contract_set=_contracts(),
            unsafe_passthrough_tools=frozenset(),
            ledger=ledger,
            session_id="s_pack_deny_cap",
            policy=load_policy(POLICY),
            mandate=loose,
            action_describer=_describe,
        )
        lifecycle.start_session("recovery-agent")

        with pytest.raises(RekhaError) as excinfo:
            await lifecycle.govern_and_execute(
                "create_payment_link",
                {
                    "amount": 1200000,
                    "currency": "INR",
                    "reference_id": "acc-deny-1",
                    "method": "upi",
                },
                read_only_hint=False,
                executor=upstream,
            )

        assert excinfo.value.code == "policy_denied"
        assert upstream.calls == []


@pytest.mark.slow
@pytest.mark.anyio
async def test_reads_are_allowed_outright_so_diagnosis_is_not_gated() -> None:
    """200 approval requests to analyse a cohort would make the system unusable.
    `policy.yaml` allows `fetch_*`, and that relaxation is recorded in evidence."""
    params = StdioServerParameters(command=sys.executable, args=[str(SANDBOX)])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        upstream = _Upstream(session)
        ledger = LedgerStore()
        lifecycle = _lifecycle(ledger, "s_pack_read", permissive=False)
        lifecycle.start_session("recovery-agent")

        raw = await lifecycle.govern_and_execute(
            "fetch_all_payments",
            {"status": "failed", "count": 200},
            read_only_hint=True,
            executor=upstream,
        )
        payments = _result(raw)
        assert payments["count"] == 200
        assert len(upstream.calls) == 1

        events = ledger.read("s_pack_read")
        assert any(e.type == "step_committed" for e in events)
        assert verify_chain(events).ok


# ------------------------------------------------------------------- pack meta


def test_pack_metadata_declares_its_upstream_and_trust_state() -> None:
    meta = yaml.safe_load((PACK.parent / "pack.yaml").read_text(encoding="utf-8"))
    assert meta["name"] == "razorpay"
    assert meta["trust_state"] == "unverified"
    assert "razorpay-mcp-server" in meta["upstream"]["repository"]
