"""Mandate enforcement inside the governed lifecycle (ADR 0028).

`tests/finance/test_mandate.py` covers `check_mandate` as a pure function.
This file covers the thing that actually protects a merchant: that the
lifecycle consults the mandate *before* contracts, planning, or policy, refuses
the action, records why in the ledger, and never reaches the upstream.

The last clause is the one worth having a test for. A refusal that still calls
Razorpay is not a refusal.
"""

from __future__ import annotations

from typing import Any

import pytest
from belay.contracts.model import Contract, ContractSet
from belay.errors import BelayError
from belay.finance.mandate import MerchantMandate
from belay.finance.money import Money
from belay.ledger.store import LedgerStore
from belay.policy.model import PolicyDoc, ToolRule
from belay.proxy.lifecycle import Lifecycle


def _contract(tool: str, effect_type: str = "spend") -> Contract:
    return Contract.model_validate(
        {
            "belay_contract": "0.1",
            "tool": tool,
            "reversibility": "irreversible",
            "effects": [{"type": effect_type, "resource": "razorpay.payment", "count": "1"}],
        }
    )


def _contract_set(*tools: str) -> ContractSet:
    return ContractSet(
        contracts={t: _contract(t) for t in tools},
        set_hash="sha256:test",
    )


def _mandate(**overrides: Any) -> MerchantMandate:
    base: dict[str, Any] = {
        "merchant_id": "acme_retail",
        "currency": "INR",
        "allowed_actions": ["create_payment_link"],
        "forbidden_actions": ["create_refund"],
        "allowed_methods": ["upi"],
        "max_per_action": Money.from_major("5000.00", "INR"),
        "max_cumulative": Money.from_major("50000.00", "INR"),
    }
    base.update(overrides)
    return MerchantMandate.model_validate(base)


def _describe(tool: str, args: dict[str, Any]) -> tuple[Money | None, str | None]:
    """Stand-in for the Razorpay describer: amount in paise, method by name."""
    amount = None
    if "amount" in args:
        amount = Money(minor_units=int(args["amount"]), currency=args.get("currency", "INR"))
    return amount, args.get("method")


class _RecordingUpstream:
    """Records every upstream call, so 'never reached the upstream' is assertable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, args))
        return {"id": "plink_test", "status": "created"}


def _lifecycle(
    ledger: LedgerStore, mandate: MerchantMandate | None, *, describe: bool = True
) -> Lifecycle:
    """A lifecycle whose *only* variable is the mandate.

    The policy is deliberately permissive here. These contracts are
    `irreversible`, so the real default policy (spec §6.4) would pause every
    one of them for approval -- correct behaviour, and tested elsewhere, but it
    would mean a test asserting "the upstream was reached" could pass or fail
    for reasons having nothing to do with the mandate. Policy interaction is
    covered in `tests/policy/`; this file isolates the mandate boundary.
    """
    return Lifecycle(
        contract_set=_contract_set("create_payment_link", "create_refund", "capture_payment"),
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id="s_mandate",
        policy=PolicyDoc(tools=[ToolRule(match="*", verdict="allow")]),
        mandate=mandate,
        action_describer=_describe if describe else None,
    )


# ------------------------------------------------------------------- happy path


@pytest.mark.anyio
async def test_a_permitted_action_within_limits_reaches_the_upstream() -> None:
    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    lifecycle = _lifecycle(ledger, _mandate())
    lifecycle.start_session("recovery-agent")

    await lifecycle.govern_and_execute(
        "create_payment_link",
        {"amount": 240000, "currency": "INR", "method": "upi"},
        read_only_hint=False,
        executor=upstream,
    )
    assert len(upstream.calls) == 1


# --------------------------------------------------------------------- refusals


@pytest.mark.anyio
async def test_a_forbidden_action_never_reaches_the_upstream() -> None:
    """The prompt-injection defence, end to end. A refusal that still calls
    Razorpay is not a refusal."""
    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    lifecycle = _lifecycle(ledger, _mandate())
    lifecycle.start_session("recovery-agent")

    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "create_refund",
            {"amount": 5000000, "currency": "INR"},
            read_only_hint=False,
            executor=upstream,
        )

    assert excinfo.value.code == "mandate_violation"
    assert excinfo.value.detail["field"] == "forbidden_actions"
    assert upstream.calls == []


@pytest.mark.anyio
async def test_over_the_per_action_ceiling_never_reaches_the_upstream() -> None:
    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    lifecycle = _lifecycle(ledger, _mandate())
    lifecycle.start_session("recovery-agent")

    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "create_payment_link",
            {"amount": 500001, "currency": "INR", "method": "upi"},
            read_only_hint=False,
            executor=upstream,
        )

    assert excinfo.value.detail["field"] == "max_per_action"
    assert upstream.calls == []


@pytest.mark.anyio
async def test_an_action_absent_from_the_allow_list_is_refused() -> None:
    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    lifecycle = _lifecycle(ledger, _mandate())
    lifecycle.start_session("recovery-agent")

    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "capture_payment", {"amount": 100}, read_only_hint=False, executor=upstream
        )
    assert excinfo.value.detail["field"] == "allowed_actions"
    assert upstream.calls == []


@pytest.mark.anyio
async def test_a_disallowed_method_is_refused() -> None:
    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    lifecycle = _lifecycle(ledger, _mandate())
    lifecycle.start_session("recovery-agent")

    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "create_payment_link",
            {"amount": 100000, "currency": "INR", "method": "paylater"},
            read_only_hint=False,
            executor=upstream,
        )
    assert excinfo.value.detail["field"] == "allowed_methods"
    assert upstream.calls == []


@pytest.mark.anyio
async def test_a_foreign_currency_is_refused() -> None:
    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    lifecycle = _lifecycle(ledger, _mandate())
    lifecycle.start_session("recovery-agent")

    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "create_payment_link",
            {"amount": 10000, "currency": "USD", "method": "upi"},
            read_only_hint=False,
            executor=upstream,
        )
    assert excinfo.value.detail["field"] == "currency"
    assert upstream.calls == []


# ------------------------------------------------------------------- ordering


@pytest.mark.anyio
async def test_the_mandate_is_checked_before_contract_resolution() -> None:
    """A forbidden action with NO contract must report the mandate refusal.

    If contract resolution ran first this would be `contract_missing`, which
    would be a worse answer: it would tell the agent to go and write a contract
    for something the merchant never authorized. Ordering is the assertion.
    """
    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    lifecycle = Lifecycle(
        contract_set=ContractSet(contracts={}, set_hash="sha256:empty"),
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id="s_order",
        mandate=_mandate(),
        action_describer=_describe,
    )
    lifecycle.start_session("recovery-agent")

    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "create_refund", {"amount": 100}, read_only_hint=False, executor=upstream
        )
    assert excinfo.value.code == "mandate_violation"
    assert upstream.calls == []


# ------------------------------------------------------------------- evidence


@pytest.mark.anyio
async def test_the_mandate_hash_is_pinned_into_session_started() -> None:
    """So which authority governed a session is a signed fact, not an assertion."""
    ledger = LedgerStore()
    mandate = _mandate()
    lifecycle = _lifecycle(ledger, mandate)
    lifecycle.start_session("recovery-agent")

    started = next(e for e in ledger.read("s_mandate") if e.type == "session_started")
    assert started.payload["mandate_hash"] == mandate.hash()
    assert started.payload["merchant_id"] == "acme_retail"


@pytest.mark.anyio
async def test_a_refusal_is_recorded_in_the_ledger_with_the_violated_field() -> None:
    """A blocked action must leave evidence, or the demo has nothing to show."""
    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    mandate = _mandate()
    lifecycle = _lifecycle(ledger, mandate)
    lifecycle.start_session("recovery-agent")

    with pytest.raises(BelayError):
        await lifecycle.govern_and_execute(
            "create_refund", {"amount": 5000000}, read_only_hint=False, executor=upstream
        )

    failed = [e for e in ledger.read("s_mandate") if e.type == "step_failed"]
    assert len(failed) == 1
    assert failed[0].payload["mandate_field"] == "forbidden_actions"
    assert failed[0].payload["mandate_hash"] == mandate.hash()
    assert failed[0].payload["error"]["code"] == "mandate_violation"


@pytest.mark.anyio
async def test_the_chain_still_verifies_after_a_refusal() -> None:
    from belay.ledger.verify import verify_chain, verify_coherence

    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    lifecycle = _lifecycle(ledger, _mandate())
    lifecycle.start_session("recovery-agent")
    with pytest.raises(BelayError):
        await lifecycle.govern_and_execute(
            "create_refund", {"amount": 1}, read_only_hint=False, executor=upstream
        )

    events = ledger.read("s_mandate")
    assert verify_chain(events).ok
    assert verify_coherence(events).ok


# ----------------------------------------------------------------- degradation


@pytest.mark.anyio
async def test_no_mandate_means_no_mandate_check() -> None:
    """Backwards compatible: the control plane still works without a mandate."""
    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    lifecycle = _lifecycle(ledger, None)
    lifecycle.start_session("recovery-agent")

    await lifecycle.govern_and_execute(
        "create_refund", {"amount": 5000000}, read_only_hint=False, executor=upstream
    )
    assert len(upstream.calls) == 1

    started = next(e for e in ledger.read("s_mandate") if e.type == "session_started")
    assert "mandate_hash" not in started.payload


@pytest.mark.anyio
async def test_without_a_describer_the_mandate_still_enforces_action_names() -> None:
    """Name-only enforcement is degraded but real: it is what refuses a refund.

    Amount ceilings cannot apply without a describer, which is exactly why
    `action_describer` is never defaulted to a guess at argument names.
    """
    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    lifecycle = _lifecycle(ledger, _mandate(), describe=False)
    lifecycle.start_session("recovery-agent")

    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "create_refund", {"amount": 5000000}, read_only_hint=False, executor=upstream
        )
    assert excinfo.value.detail["field"] == "forbidden_actions"

    # Over the ceiling, but with no describer there is no amount to compare.
    await lifecycle.govern_and_execute(
        "create_payment_link",
        {"amount": 99999999, "currency": "INR"},
        read_only_hint=False,
        executor=upstream,
    )
    assert len(upstream.calls) == 1


@pytest.mark.anyio
async def test_a_describer_that_raises_refuses_the_action() -> None:
    """Fail closed. A malformed argument payload must not skip the mandate check."""

    def broken(tool: str, args: dict[str, Any]) -> tuple[Money | None, str | None]:
        raise RuntimeError("unparseable amount")

    ledger = LedgerStore()
    upstream = _RecordingUpstream()
    lifecycle = Lifecycle(
        contract_set=_contract_set("create_payment_link"),
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id="s_broken",
        mandate=_mandate(),
        action_describer=broken,
    )
    lifecycle.start_session("recovery-agent")

    with pytest.raises(BelayError) as excinfo:
        await lifecycle.govern_and_execute(
            "create_payment_link", {"amount": "???"}, read_only_hint=False, executor=upstream
        )
    assert excinfo.value.code == "mandate_violation"
    assert upstream.calls == []
