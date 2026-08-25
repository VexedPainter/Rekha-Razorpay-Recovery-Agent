"""Cumulative spend and velocity limits -- the fan-out defence.

The scenario this whole module exists for: forty payment links of Rs 4,000 each.
Every one passes a Rs 5,000 per-action ceiling. Together they breach a Rs 50,000
daily budget by more than three times.

Three properties are load-bearing, and each has a test whose failure would mean
the limit is unsound rather than merely wrong:

1. **Partial progress.** The crossing action is refused and everything under the
   ceiling stays allowed. A budget is spent up to its limit, not voided on
   approach.
2. **Only authorized-and-executed actions count.** A denied or still-pending
   action spent nothing, so counting it would shrink the budget for money that
   never moved.
3. **A paused-then-approved action counts exactly once.** This is the
   double-counting trap. A paused call re-plans under a new `step_seq` when
   retried, so the fold keys on `plan_id`; keying on `step_seq` would charge the
   merchant twice for every human-approved recovery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from belay.clock import FixedClock
from belay.contracts.model import Contract, ContractSet
from belay.errors import BelayError
from belay.finance.mandate import MerchantMandate
from belay.finance.money import Money
from belay.ledger.store import LedgerStore
from belay.policy.cumulative import CumulativeTracker, fold_authorized_actions
from belay.policy.model import PolicyDoc, ToolRule
from belay.proxy.lifecycle import Lifecycle

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def _contract_set() -> ContractSet:
    link = Contract.model_validate(
        {
            "belay_contract": "0.1",
            "tool": "create_payment_link",
            "reversibility": "irreversible",
            "idempotency_key": "$args.reference_id",
            "effects": [
                {"type": "create", "resource": "razorpay.payment_link", "count": "1"},
                {
                    "type": "spend",
                    "resource": "razorpay.payment_link",
                    "count": "1",
                    "amount_from": {
                        "minor_units": "$args.amount",
                        "currency": "$args.currency",
                    },
                },
            ],
        }
    )
    read = Contract.model_validate(
        {
            "belay_contract": "0.1",
            "tool": "fetch_payment",
            "reversibility": "irreversible",
            "effects": [{"type": "read", "resource": "razorpay.payment", "count": "1"}],
        }
    )
    return ContractSet(
        contracts={"create_payment_link": link, "fetch_payment": read},
        set_hash="sha256:cumulative-test",
    )


def _mandate(**overrides: Any) -> MerchantMandate:
    base: dict[str, Any] = {
        "merchant_id": "acme_retail",
        "currency": "INR",
        "allowed_actions": ["create_payment_link", "fetch_payment"],
        "max_per_action": Money.from_major("5000.00", "INR"),
        "max_cumulative": Money.from_major("50000.00", "INR"),
        "window": "1d",
    }
    base.update(overrides)
    return MerchantMandate.model_validate(base)


def _describe(tool: str, args: dict[str, Any]) -> tuple[Money | None, str | None]:
    raw = args.get("amount")
    amount = (
        Money(minor_units=raw, currency=args.get("currency", "INR"))
        if isinstance(raw, int) and not isinstance(raw, bool)
        else None
    )
    return amount, args.get("method")


class _Upstream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, args))
        return {"id": f"plink_{len(self.calls)}", "status": "created"}


def _lifecycle(
    ledger: LedgerStore,
    session_id: str,
    mandate: MerchantMandate,
    *,
    now: datetime = NOW,
    policy: PolicyDoc | None = None,
) -> Lifecycle:
    return Lifecycle(
        contract_set=_contract_set(),
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id=session_id,
        clock=FixedClock(now),
        policy=policy or PolicyDoc(tools=[ToolRule(match="*", verdict="allow")]),
        mandate=mandate,
        action_describer=_describe,
    )


async def _spend(
    lifecycle: Lifecycle, upstream: _Upstream, rupees: str, reference: str
) -> Any:
    return await lifecycle.govern_and_execute(
        "create_payment_link",
        {
            "amount": Money.from_major(rupees, "INR").minor_units,
            "currency": "INR",
            "reference_id": reference,
            "method": "upi",
        },
        read_only_hint=False,
        executor=upstream,
    )


# ------------------------------------------------------------------ THE SCENARIO


@pytest.mark.anyio
async def test_fan_out_of_sub_cap_actions_is_stopped_at_the_aggregate_ceiling() -> None:
    """Rs 4,000 links against a Rs 5,000/action and Rs 50,000/day mandate.

    Twelve succeed (Rs 48,000). The thirteenth would reach Rs 52,000 and is
    refused. Per-action limits alone would have allowed all forty.
    """
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    lifecycle = _lifecycle(ledger, "s_fanout", _mandate())
    lifecycle.start_session("recovery-agent")

    allowed = 0
    refusal: BelayError | None = None
    for index in range(40):
        try:
            await _spend(lifecycle, upstream, "4000.00", f"fan-{index}")
            allowed += 1
        except BelayError as exc:
            refusal = exc
            break

    assert allowed == 12, f"expected 12 x Rs 4,000 = Rs 48,000, got {allowed}"
    assert refusal is not None
    assert refusal.code == "cumulative_limit_exceeded"
    assert refusal.detail["field"] == "max_cumulative"
    assert refusal.detail["already_spent"] == "INR 48,000.00"
    assert refusal.detail["limit"] == "INR 50,000.00"

    # Partial progress: the twelve that fit really executed.
    assert len(upstream.calls) == 12


@pytest.mark.anyio
async def test_the_actions_under_the_ceiling_stay_allowed() -> None:
    """A budget is spent up to its limit, not voided on approach.

    An off-by-one here means a merchant who authorizes Rs 50,000 of recovery can
    never spend the last of it.
    """
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    lifecycle = _lifecycle(ledger, "s_exact", _mandate())
    lifecycle.start_session("recovery-agent")

    for index in range(10):
        await _spend(lifecycle, upstream, "5000.00", f"exact-{index}")

    tracker = CumulativeTracker(ledger)
    spent = tracker.spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(days=1)
    )
    assert spent == Money.from_major("50000.00", "INR")
    assert len(upstream.calls) == 10

    # Exactly at the ceiling is fine; one paisa more is not.
    with pytest.raises(BelayError) as excinfo:
        await _spend(lifecycle, upstream, "0.01", "exact-over")
    assert excinfo.value.code == "cumulative_limit_exceeded"


@pytest.mark.anyio
async def test_the_ceiling_spans_sessions() -> None:
    """A daily ceiling that resets by starting a new session is not a ceiling."""
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()

    first = _lifecycle(ledger, "s_span_1", _mandate())
    first.start_session("recovery-agent")
    for index in range(9):
        await _spend(first, upstream, "5000.00", f"span-a-{index}")

    second = _lifecycle(ledger, "s_span_2", _mandate())
    second.start_session("recovery-agent")
    await _spend(second, upstream, "5000.00", "span-b-0")

    with pytest.raises(BelayError) as excinfo:
        await _spend(second, upstream, "1000.00", "span-b-1")
    assert excinfo.value.code == "cumulative_limit_exceeded"
    assert len(upstream.calls) == 10


# ------------------------------------------------------- what must NOT be counted


@pytest.mark.anyio
async def test_a_denied_action_does_not_consume_the_budget() -> None:
    """A refused action spent nothing. Charging for it would shrink the budget
    for money that never moved."""
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    lifecycle = _lifecycle(ledger, "s_denied", _mandate())
    lifecycle.start_session("recovery-agent")

    # Over the per-action ceiling, so refused before it ever plans.
    with pytest.raises(BelayError):
        await _spend(lifecycle, upstream, "9000.00", "denied-1")

    spent = CumulativeTracker(ledger).spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(days=1)
    )
    assert spent == Money.zero("INR")
    assert upstream.calls == []


@pytest.mark.anyio
async def test_a_pending_approval_does_not_consume_the_budget() -> None:
    """Money parked awaiting a human has not moved."""
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    # Default policy: irreversible actions pause for approval.
    lifecycle = _lifecycle(ledger, "s_pending", _mandate(), policy=PolicyDoc())
    lifecycle.start_session("recovery-agent")

    outcome = await _spend(lifecycle, upstream, "4000.00", "pending-1")
    assert isinstance(outcome, dict)
    assert outcome["status"] == "pending_approval"

    spent = CumulativeTracker(ledger).spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(days=1)
    )
    assert spent == Money.zero("INR")
    assert upstream.calls == []


@pytest.mark.anyio
async def test_a_paused_then_approved_action_counts_exactly_once() -> None:
    """The double-counting trap.

    A paused call re-plans under a NEW `step_seq` when retried after approval, so
    the fold keys on `plan_id`. Keying on `step_seq` would charge the merchant
    twice for every human-approved recovery -- and the error would be invisible
    until a budget ran out at half its stated value.
    """
    from belay.approvals.queue import ApprovalQueue

    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    lifecycle = _lifecycle(ledger, "s_approved", _mandate(), policy=PolicyDoc())
    lifecycle.start_session("recovery-agent")

    args = {
        "amount": Money.from_major("4000.00", "INR").minor_units,
        "currency": "INR",
        "reference_id": "approve-1",
        "method": "upi",
    }

    parked = await lifecycle.govern_and_execute(
        "create_payment_link", dict(args), read_only_hint=False, executor=upstream
    )
    assert isinstance(parked, dict)

    queue = ApprovalQueue(engine=ledger.engine, clock=FixedClock(NOW))
    item = queue.get(parked["approval_id"])
    assert item is not None
    queue.approve(item.approval_id, "merchant-ops")
    ledger.append(
        "s_approved",
        "approval_resolved",
        {"approval_id": item.approval_id, "plan_id": item.plan_id, "state": "approved"},
    )

    # The agent retries the identical call; it now proceeds.
    await lifecycle.govern_and_execute(
        "create_payment_link", dict(args), read_only_hint=False, executor=upstream
    )

    spent = CumulativeTracker(ledger).spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(days=1)
    )
    assert spent == Money.from_major("4000.00", "INR"), (
        f"expected Rs 4,000 charged once, got {spent} -- the fold is double-counting "
        f"a paused-then-approved action"
    )


@pytest.mark.anyio
async def test_a_read_consumes_no_budget() -> None:
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    lifecycle = _lifecycle(ledger, "s_read", _mandate())
    lifecycle.start_session("recovery-agent")

    await lifecycle.govern_and_execute(
        "fetch_payment", {"payment_id": "pay_x"}, read_only_hint=True, executor=upstream
    )
    spent = CumulativeTracker(ledger).spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(days=1)
    )
    assert spent == Money.zero("INR")


# ------------------------------------------------------------------------ window


@pytest.mark.anyio
async def test_spend_outside_the_window_no_longer_counts() -> None:
    """Yesterday's recoveries do not consume today's budget.

    Two `LedgerStore` views over one shared engine, with different clocks: the
    same durable events, written 25 hours apart. That is what makes this a real
    window test rather than an assertion about a single instant.
    """
    yesterday = NOW - timedelta(days=1, hours=1)
    ledger_then = LedgerStore(clock=FixedClock(yesterday))
    ledger_now = LedgerStore(engine=ledger_then.engine, clock=FixedClock(NOW))
    upstream = _Upstream()

    # Yesterday: the merchant's whole daily budget is spent.
    old = _lifecycle(ledger_then, "s_old", _mandate(), now=yesterday)
    old.start_session("recovery-agent")
    for index in range(10):
        await _spend(old, upstream, "5000.00", f"old-{index}")

    tracker = CumulativeTracker(ledger_now)
    # Measured as of yesterday, the budget is exhausted...
    assert tracker.spent_by_merchant(
        "acme_retail", currency="INR", now=yesterday, window=timedelta(days=1)
    ) == Money.from_major("50000.00", "INR")
    # ...and measured today, it has rolled off entirely.
    assert tracker.spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(days=1)
    ) == Money.zero("INR")

    # So today's agent can spend again, against the same durable ledger.
    today = _lifecycle(ledger_now, "s_new", _mandate(), now=NOW)
    today.start_session("recovery-agent")
    await _spend(today, upstream, "5000.00", "new-0")
    assert len(upstream.calls) == 11

    assert tracker.spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(days=1)
    ) == Money.from_major("5000.00", "INR")


@pytest.mark.anyio
async def test_an_action_exactly_at_the_window_edge_still_counts() -> None:
    """Boundary convention, shared with `QuotaTracker`: exactly `window` old
    counts; older does not. One convention, so two limits cannot disagree about
    whether the same action is inside the window."""
    edge = NOW - timedelta(days=1)
    ledger_then = LedgerStore(clock=FixedClock(edge))
    ledger_now = LedgerStore(engine=ledger_then.engine, clock=FixedClock(NOW))
    upstream = _Upstream()

    at_edge = _lifecycle(ledger_then, "s_edge", _mandate(), now=edge)
    at_edge.start_session("recovery-agent")
    await _spend(at_edge, upstream, "5000.00", "edge-0")

    tracker = CumulativeTracker(ledger_now)
    assert tracker.spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(days=1)
    ) == Money.from_major("5000.00", "INR")
    assert tracker.spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(hours=23)
    ) == Money.zero("INR")


@pytest.mark.anyio
async def test_another_merchants_spend_is_not_counted() -> None:
    """Budgets are per merchant. One tenant cannot exhaust another's."""
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()

    other = _lifecycle(ledger, "s_other", _mandate(merchant_id="other_corp"))
    other.start_session("recovery-agent")
    for index in range(10):
        await _spend(other, upstream, "5000.00", f"other-{index}")

    tracker = CumulativeTracker(ledger)
    assert tracker.spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(days=1)
    ) == Money.zero("INR")
    assert tracker.spent_by_merchant(
        "other_corp", currency="INR", now=NOW, window=timedelta(days=1)
    ) == Money.from_major("50000.00", "INR")


# ---------------------------------------------------------------------- velocity


@pytest.mark.anyio
async def test_velocity_bounds_action_count_regardless_of_amount() -> None:
    """A hundred Rs 1 links is a pattern worth stopping even though the money is
    trivial -- so velocity is deliberately independent of value."""
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    mandate = _mandate(max_actions_per_window=5, max_cumulative=None)
    lifecycle = _lifecycle(ledger, "s_velocity", mandate)
    lifecycle.start_session("recovery-agent")

    for index in range(5):
        await _spend(lifecycle, upstream, "1.00", f"vel-{index}")

    with pytest.raises(BelayError) as excinfo:
        await _spend(lifecycle, upstream, "1.00", "vel-over")
    assert excinfo.value.code == "velocity_limit_exceeded"
    assert excinfo.value.detail["count"] == 5
    assert excinfo.value.detail["limit"] == 5
    assert len(upstream.calls) == 5


# ---------------------------------------------------------------------- evidence


@pytest.mark.anyio
async def test_a_limit_refusal_is_recorded_in_the_ledger() -> None:
    """A blocked action that leaves no evidence is indistinguishable from one
    that was never attempted, which would make the limits unauditable."""
    from belay.ledger.verify import verify_chain, verify_coherence

    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    lifecycle = _lifecycle(ledger, "s_evidence", _mandate())
    lifecycle.start_session("recovery-agent")

    for index in range(12):
        await _spend(lifecycle, upstream, "4000.00", f"ev-{index}")
    with pytest.raises(BelayError):
        await _spend(lifecycle, upstream, "4000.00", "ev-over")

    events = ledger.read("s_evidence")
    failed = [e for e in events if e.type == "step_failed"]
    assert len(failed) == 1
    assert failed[0].payload["error"]["code"] == "cumulative_limit_exceeded"
    assert failed[0].payload["mandate_field"] == "max_cumulative"
    assert failed[0].payload["mandate_hash"] == _mandate().hash()
    assert verify_chain(events).ok
    assert verify_coherence(events).ok


@pytest.mark.anyio
async def test_the_refusal_names_the_numbers_that_caused_it() -> None:
    """An agent's self-explanation and the demo both need the arithmetic, not
    just a refusal."""
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    lifecycle = _lifecycle(ledger, "s_numbers", _mandate())
    lifecycle.start_session("recovery-agent")

    for index in range(12):
        await _spend(lifecycle, upstream, "4000.00", f"num-{index}")
    with pytest.raises(BelayError) as excinfo:
        await _spend(lifecycle, upstream, "4000.00", "num-over")

    detail = excinfo.value.detail
    assert detail["already_spent"] == "INR 48,000.00"
    assert detail["this_action"] == "INR 4,000.00"
    assert detail["limit"] == "INR 50,000.00"
    assert detail["window"] == "1d"


@pytest.mark.anyio
async def test_actions_above_the_approval_threshold_pause_and_spend_nothing() -> None:
    """The gap a demo found that these tests had missed.

    The shipped mandate carries an `approval_threshold`; the fixture mandate in
    this file did not. So every test here exercised the autonomous path, and the
    interaction between "paused for approval" and "counts toward the budget" went
    untested. A demo script then reported forty paused actions as forty
    successes, because it treated the `pending_approval` return value as a
    result.

    Conflating those two is a real bug class: it is how a caller ends up
    believing money moved when it did not. Pinned here in both directions --
    the return shape is a pause, and the budget is untouched.
    """
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    mandate = _mandate(approval_threshold=Money.from_major("2500.00", "INR"))
    lifecycle = _lifecycle(ledger, "s_threshold", mandate)
    lifecycle.start_session("recovery-agent")

    # Over the threshold: pauses, even though policy here allows everything.
    parked = await _spend(lifecycle, upstream, "4000.00", "thr-over")
    assert isinstance(parked, dict)
    assert parked["status"] == "pending_approval"
    assert upstream.calls == []

    # Under the threshold: fully autonomous.
    executed = await _spend(lifecycle, upstream, "2000.00", "thr-under")
    assert not (isinstance(executed, dict) and executed.get("status") == "pending_approval")
    assert len(upstream.calls) == 1

    # Only the executed one consumed budget.
    spent = CumulativeTracker(ledger).spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(days=1)
    )
    assert spent == Money.from_major("2000.00", "INR")


@pytest.mark.anyio
async def test_twenty_five_autonomous_recoveries_then_the_ceiling(
) -> None:
    """The demo's exact arithmetic, pinned so `examples/demo_fanout.py` cannot rot.

    Rs 2,000 links against the shipped mandate: each under the Rs 5,000
    per-action ceiling AND under the Rs 2,500 approval threshold, so each is
    individually authorized with no human review. Twenty-five execute
    (Rs 50,000). The twenty-sixth is refused.
    """
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    mandate = _mandate(approval_threshold=Money.from_major("2500.00", "INR"))
    lifecycle = _lifecycle(ledger, "s_demo_arith", mandate)
    lifecycle.start_session("recovery-agent")

    executed = 0
    refusal: BelayError | None = None
    for index in range(40):
        try:
            outcome = await _spend(lifecycle, upstream, "2000.00", f"arith-{index}")
        except BelayError as exc:
            refusal = exc
            break
        if isinstance(outcome, dict) and outcome.get("status") == "pending_approval":
            continue
        executed += 1

    assert executed == 25
    assert refusal is not None
    assert refusal.code == "cumulative_limit_exceeded"
    assert len(upstream.calls) == 25
    prevented = Money.from_major("2000.00", "INR") * 40 - Money.from_major("50000.00", "INR")
    assert prevented == Money.from_major("30000.00", "INR")


# ------------------------------------------------- operator `per: session` caps


@pytest.mark.anyio
async def test_a_per_session_policy_cap_aggregates_across_the_session() -> None:
    """`Cap.per: session` now genuinely aggregates.

    It was declared in the model and never read by `PolicyEngine` for the whole
    life of the project, so it silently behaved as `per: call`. A cap an operator
    believes is a budget, but which only ever sees one action, reads as
    protection in the policy document while providing none.
    """
    from belay.policy.model import Cap, CapMatch

    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    policy = PolicyDoc(
        tools=[ToolRule(match="*", verdict="allow")],
        caps=[
            Cap(
                match=CapMatch(effect="spend"),
                max_amount=Money.from_major("10000.00", "INR"),
                per="session",
                over="deny",
            )
        ],
    )
    # No mandate ceiling, so the operator's session cap is the only limit.
    lifecycle = _lifecycle(
        ledger,
        "s_session_cap",
        _mandate(max_cumulative=None),
        policy=policy,
    )
    lifecycle.start_session("recovery-agent")

    for index in range(2):
        await _spend(lifecycle, upstream, "5000.00", f"sess-{index}")
    assert len(upstream.calls) == 2

    with pytest.raises(BelayError) as excinfo:
        await _spend(lifecycle, upstream, "5000.00", "sess-over")
    assert excinfo.value.code == "policy_denied"
    assert any("per session" in reason for reason in excinfo.value.detail["reasons"])
    assert len(upstream.calls) == 2


@pytest.mark.anyio
async def test_a_per_session_cap_does_not_leak_across_sessions() -> None:
    """`per: session` means what it says. The merchant-wide ceiling is the
    mandate's job, and it is scoped differently on purpose."""
    from belay.policy.model import Cap, CapMatch

    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    policy = PolicyDoc(
        tools=[ToolRule(match="*", verdict="allow")],
        caps=[
            Cap(
                match=CapMatch(effect="spend"),
                max_amount=Money.from_major("5000.00", "INR"),
                per="session",
                over="deny",
            )
        ],
    )

    first = _lifecycle(ledger, "s_leak_1", _mandate(max_cumulative=None), policy=policy)
    first.start_session("recovery-agent")
    await _spend(first, upstream, "5000.00", "leak-a")

    second = _lifecycle(ledger, "s_leak_2", _mandate(max_cumulative=None), policy=policy)
    second.start_session("recovery-agent")
    await _spend(second, upstream, "5000.00", "leak-b")

    assert len(upstream.calls) == 2


def test_a_per_session_cap_without_an_amount_is_refused_at_load_time() -> None:
    """Fail loudly rather than half-implement.

    `max_count` and `max_recipients` have no unambiguous aggregate meaning -- a
    count of what, effects or actions? Silently treating them as `per: call` is
    exactly the bug that existed before, so it is now a load-time error.
    """
    from belay.policy.model import Cap, CapMatch

    with pytest.raises(BelayError) as excinfo:
        Cap(match=CapMatch(effect="create"), max_count=5, per="session", over="deny")
    assert excinfo.value.code == "contract_invalid"
    assert "max_count" in excinfo.value.detail["reason"]

    with pytest.raises(BelayError):
        Cap(match=CapMatch(effect="send"), max_recipients=5, per="session", over="deny")

    with pytest.raises(BelayError) as excinfo:
        Cap(match=CapMatch(effect="spend"), per="session", over="deny")
    assert "requires `max_amount`" in excinfo.value.detail["reason"]


def test_a_per_call_cap_still_accepts_counts() -> None:
    """The `per: session` restriction must not have broken ordinary caps."""
    from belay.policy.model import Cap, CapMatch

    cap = Cap(match=CapMatch(effect="create"), max_count=1, over="pause")
    assert cap.per == "call"
    assert cap.max_count == 1


# ------------------------------------------------------------------- pure fold


def test_the_fold_is_deterministic_and_pure() -> None:
    """Same events in, same result out -- so a limit decision can be replayed."""
    ledger = LedgerStore(clock=FixedClock(NOW))
    ledger.append("s", "session_started", {"merchant_id": "m"}, initiated_by="agent")
    ledger.append(
        "s",
        "plan_created",
        {
            "plan_id": "p_1",
            "tool": "create_payment_link",
            "reversibility": "irreversible",
            "effects": [
                {
                    "type": "spend",
                    "resource": "r",
                    "amount": {"minor_units": 100, "currency": "INR"},
                }
            ],
        },
        step_seq=1,
    )
    ledger.append("s", "policy_evaluated", {"verdict": "allow"}, step_seq=1)
    ledger.append("s", "step_committed", {"tool": "create_payment_link"}, step_seq=1)

    events = ledger.read("s")
    first = fold_authorized_actions(events)
    second = fold_authorized_actions(events)
    assert first == second
    assert len(first.actions) == 1
    assert first.actions[0].spend_in("INR") == Money(minor_units=100, currency="INR")


def test_an_unreadable_spend_amount_is_reported_not_silently_zero() -> None:
    """Under-counting a budget fails in the permissive direction, so an amount
    that cannot be parsed is surfaced rather than treated as zero."""
    ledger = LedgerStore(clock=FixedClock(NOW))
    ledger.append("s", "session_started", {"merchant_id": "m"}, initiated_by="agent")
    ledger.append(
        "s",
        "plan_created",
        {
            "plan_id": "p_1",
            "tool": "create_payment_link",
            "reversibility": "irreversible",
            "effects": [{"type": "spend", "resource": "r", "amount": {"value": 100.0}}],
        },
        step_seq=1,
    )
    ledger.append("s", "policy_evaluated", {"verdict": "allow"}, step_seq=1)
    ledger.append("s", "step_committed", {"tool": "create_payment_link"}, step_seq=1)

    result = fold_authorized_actions(ledger.read("s"))
    assert result.unaccounted_spend_effects == 1
    assert result.actions[0].amounts == ()


def test_read_by_types_returns_only_the_requested_types() -> None:
    """The fold reads 5 of ~20 event types; `state_captured` and
    `result_recorded` carry whole upstream responses and dominate table size."""
    ledger = LedgerStore(clock=FixedClock(NOW))
    ledger.append("s", "session_started", {})
    ledger.append("s", "result_recorded", {"huge": "x" * 1000}, step_seq=1)
    ledger.append("s", "step_committed", {}, step_seq=1)

    got = ledger.read_by_types({"session_started", "step_committed"})
    assert [e.type for e in got] == ["session_started", "step_committed"]
    assert ledger.read_by_types(set()) == []


def test_the_ledger_stamps_events_with_its_injected_clock() -> None:
    """Rolling-window limits compare an event's `at` against `Clock.now()`.

    If the ledger stamped wall-clock while the policy engine consulted an
    injected clock, the window would be unsound -- and untestable without
    rewriting `at` after the fact, which breaks the hash chain. Pinned so the
    two can never drift apart again.
    """
    ledger = LedgerStore(clock=FixedClock(NOW))
    event = ledger.append("s", "session_started", {})
    assert datetime.fromisoformat(event.at) == NOW


# -------------------------------------------------------------------- property


@pytest.mark.anyio
@pytest.mark.parametrize(
    "amounts_rupees",
    [
        ["1000.00"] * 60,
        ["4999.99"] * 15,
        ["100.00", "5000.00", "2500.00", "4999.00", "1.00"] * 6,
        ["0.01"] * 40,
    ],
)
async def test_cumulative_spend_never_exceeds_the_ceiling(
    amounts_rupees: list[str],
) -> None:
    """The invariant, over several action sequences.

    Whatever the order and sizes, authorized-and-executed spend inside the window
    must never end up above the mandate's ceiling. This is the property the
    module exists to guarantee, and a per-action limit cannot provide it.
    """
    ledger = LedgerStore(clock=FixedClock(NOW))
    upstream = _Upstream()
    lifecycle = _lifecycle(ledger, "s_prop", _mandate())
    lifecycle.start_session("recovery-agent")

    for index, rupees in enumerate(amounts_rupees):
        try:
            await _spend(lifecycle, upstream, rupees, f"prop-{index}")
        except BelayError as exc:
            assert exc.code in (
                "cumulative_limit_exceeded",
                "velocity_limit_exceeded",
                "mandate_violation",
                "policy_denied",
            )

    tracker = CumulativeTracker(ledger)
    spent = tracker.spent_by_merchant(
        "acme_retail", currency="INR", now=NOW, window=timedelta(days=1)
    )
    assert spent <= Money.from_major("50000.00", "INR"), (
        f"cumulative spend reached {spent}, over the mandate ceiling"
    )
    # Every spend effect in the ledger was readable, so the number above is
    # complete rather than an under-count that happens to satisfy the assertion.
    assert tracker.unaccounted_spend_effects() == 0
