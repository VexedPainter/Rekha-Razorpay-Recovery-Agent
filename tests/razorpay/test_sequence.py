"""Multi-step recovery sequencing.

Two properties carry the design, and the tests are ordered to make them obvious:

1. **An approved plan is not a standing authorization.** Guardrails are checked
   before the model's plan is consulted, so a plan cannot talk its way past a
   contact limit, a closed window, or a payment that already succeeded.
2. **At most one live payment link per payment.** Two live links means the customer
   can pay twice, and the merchant refunds one having paid fees on both.
"""

from __future__ import annotations

import json

from belay.finance.money import Money
from belay.ledger.store import LedgerStore
from belay.ledger.verify import verify_chain
from belay.razorpay.forecast import record_proposal
from belay.razorpay.sequence import (
    SEQUENCE_STOPPED,
    ContactPolicy,
    Halt,
    NextStep,
    SequenceState,
    StopReason,
    derive_state,
    event_epoch,
    next_step,
    record_stop,
)
from belay.razorpay.webhooks import ingest, sign_payload

INR = "INR"
SECRET = "seq_secret"
PAYMENT = "pay_seq1"
REFERENCE = f"recover-{PAYMENT}"
HOUR = 3600.0


def _plan(*steps: tuple[str, int]) -> list[dict[str, object]]:
    return [
        {"strategy": strategy, "wait_hours": hours, "rationale": f"step: {strategy}"}
        for strategy, hours in steps
    ]


def _ledger_with_plan(*steps: tuple[str, int]) -> LedgerStore:
    ledger = LedgerStore()
    ledger.append("s", "session_started", {"merchant_id": "acme"}, initiated_by="agent")
    record_proposal(
        ledger,
        "s",
        payment_id=PAYMENT,
        reference_id=REFERENCE,
        cause_class="insufficient_funds",
        strategy="wait",
        amount=Money.from_major("4200.00", INR),
        expected_recovery=Money.from_major("2100.00", INR),
        confidence="medium",
        diagnosis="salary cycle",
        reasoning="payday is the 1st",
        prompt_version="seq@0001",
        provider="test",
        model="test",
        selected=True,
        follow_up=list(_plan(*steps)),
    )
    return ledger


def _record_action(
    ledger: LedgerStore,
    tool: str,
    link_id: str,
    *,
    seq: int,
    amount: str = "4200.00",
) -> None:
    ledger.append(
        "s",
        "result_recorded",
        {
            "tool": tool,
            "result": {
                "id": link_id,
                "amount": Money.from_major(amount, INR).minor_units,
                "currency": INR,
                "reference_id": REFERENCE,
            },
        },
        step_seq=seq,
    )


def _record_payment(
    ledger: LedgerStore, link_id: str, *, seq: int, amount: str = "4200.00"
) -> None:
    paid = Money.from_major(amount, INR).minor_units
    body = json.dumps(
        {
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": link_id,
                        "amount": paid,
                        "amount_paid": paid,
                        "currency": INR,
                        "reference_id": REFERENCE,
                    }
                },
                "payment": {"entity": {"id": f"pay_new_{seq}"}},
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id=f"evt_{seq}")


# --------------------------------------------------- guardrails beat the plan


def test_a_paid_payment_stops_the_sequence_however_many_steps_remain() -> None:
    """The only stop reason anybody wanted. Checked before the plan, so a model
    proposing four more contacts cannot chase a customer who already paid."""
    ledger = _ledger_with_plan(("upi_link", 24), ("remind", 24), ("payment_link", 24))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)
    _record_payment(ledger, "plink_1", seq=2)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger))
    assert state.is_recovered

    decision = next_step(state)
    assert isinstance(decision, Halt)
    assert decision.reason is StopReason.RECOVERED


def test_the_contact_limit_cannot_be_exceeded_by_a_longer_plan() -> None:
    """A model proposing six contacts gets three. Contact limits are a merchant
    policy decision with brand and legal consequences, not a model output."""
    ledger = _ledger_with_plan(*[("remind", 24)] * 6)
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)
    _record_action(ledger, "send_payment_link", "plink_1", seq=2)
    _record_action(ledger, "send_payment_link", "plink_1", seq=3)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger))
    assert state.touches == 3

    decision = next_step(state, policy=ContactPolicy(max_touches=3))
    assert isinstance(decision, Halt)
    assert decision.reason is StopReason.CONTACT_LIMIT


def test_the_window_closes_even_with_contacts_and_plan_remaining() -> None:
    """Chasing a three-week-old failure is not persistence, it is a bad experience."""
    ledger = _ledger_with_plan(("remind", 1), ("payment_link", 1))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 400 * HOUR)
    decision = next_step(state, policy=ContactPolicy(max_window_hours=168))
    assert isinstance(decision, Halt)
    assert decision.reason is StopReason.WINDOW_CLOSED


def test_guardrails_are_checked_before_the_plan_is_read() -> None:
    """Ordering matters: a plan is only ever allowed to narrow what the guardrails
    already permit."""
    ledger = _ledger_with_plan(("upi_link", 0))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)
    _record_payment(ledger, "plink_1", seq=2)

    # Recovered AND over the contact limit AND outside the window. Recovery wins,
    # because it is a fact rather than a limit.
    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 999 * HOUR)
    decision = next_step(state, policy=ContactPolicy(max_touches=1, max_window_hours=1))
    assert isinstance(decision, Halt)
    assert decision.reason is StopReason.RECOVERED


# ------------------------------------------- at most one live link per payment


def test_escalating_the_method_requires_cancelling_the_live_link_first() -> None:
    """THE FINANCIAL INVARIANT. A UPI link and a card link live at once means the
    customer can pay both, and the merchant refunds one having paid fees on two."""
    ledger = _ledger_with_plan(("payment_link", 24))
    _record_action(ledger, "create_payment_link_upi", "plink_upi", seq=1)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 25 * HOUR)
    assert state.live_link_ids == ("plink_upi",)

    decision = next_step(state)
    assert isinstance(decision, NextStep)
    assert decision.tool == "create_payment_link"
    assert decision.requires_cancel_of == "plink_upi", (
        "creating a second link while one is live must demand the first be cancelled"
    )


def test_a_reminder_needs_no_cancel_because_it_creates_no_new_demand() -> None:
    """Why `REMIND` exists as a distinct strategy: the common second touch should
    nudge the existing link rather than mint another one."""
    ledger = _ledger_with_plan(("remind", 24))
    _record_action(ledger, "create_payment_link_upi", "plink_upi", seq=1)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 25 * HOUR)
    decision = next_step(state)
    assert isinstance(decision, NextStep)
    assert decision.tool == "send_payment_link"
    assert decision.requires_cancel_of is None


def test_a_cancelled_link_leaves_no_live_demand() -> None:
    ledger = _ledger_with_plan(("payment_link", 24))
    _record_action(ledger, "create_payment_link_upi", "plink_upi", seq=1)
    _record_action(ledger, "cancel_payment_link", "plink_upi", seq=2)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 25 * HOUR)
    assert state.live_link_ids == ()
    decision = next_step(state)
    assert isinstance(decision, NextStep)
    assert decision.requires_cancel_of is None


def test_cancelling_does_not_consume_a_contact() -> None:
    """Withdrawing a demand is not pestering somebody, so it must not spend a touch
    the customer never experienced."""
    ledger = _ledger_with_plan(("remind", 24))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)
    _record_action(ledger, "cancel_payment_link", "plink_1", seq=2)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger))
    assert state.touches == 1


def test_a_paid_link_is_no_longer_an_outstanding_demand() -> None:
    ledger = _ledger_with_plan(("remind", 1))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)
    _record_payment(ledger, "plink_1", seq=2)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger))
    assert state.live_link_ids == ()


# ----------------------------------------------------------------- timing


def test_a_step_is_not_due_until_its_delay_has_elapsed() -> None:
    ledger = _ledger_with_plan(("remind", 48))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 10 * HOUR)
    decision = next_step(state)
    assert isinstance(decision, NextStep)
    assert not decision.is_due
    assert 37 < decision.wait_hours_remaining < 39


def test_the_minimum_gap_slows_an_impatient_plan_rather_than_refusing_it() -> None:
    """A model proposing an immediate second contact is throttled, not halted. The
    plan's intent is honoured; its timing is corrected."""
    ledger = _ledger_with_plan(("remind", 0))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 1 * HOUR)
    decision = next_step(state, policy=ContactPolicy(min_gap_hours=12))
    assert isinstance(decision, NextStep)
    assert not decision.is_due
    assert 10 < decision.wait_hours_remaining < 12


def test_a_wait_step_advances_the_plan_without_spending_a_contact() -> None:
    """The bug this test pins: an earlier version incremented the touch count to
    move past a `wait`, which could fire a false contact-limit halt. Waiting is not
    contacting."""
    ledger = _ledger_with_plan(("wait", 26), ("upi_link", 0))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)

    # 30h later: the 26h wait has elapsed, so the UPI step is due.
    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 30 * HOUR)
    decision = next_step(state, policy=ContactPolicy(max_touches=2))
    assert isinstance(decision, NextStep), f"expected a step, got {decision}"
    assert decision.strategy == "upi_link"
    assert decision.is_due


def test_waits_are_summed_into_the_delay_of_the_next_real_action() -> None:
    """"Wait 12h, wait 12h, then remind" and "remind in 24h" are the same intent."""
    ledger = _ledger_with_plan(("wait", 12), ("wait", 12), ("remind", 0))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)

    early = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 20 * HOUR)
    pending = next_step(early)
    assert isinstance(pending, NextStep)
    assert not pending.is_due

    late = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 25 * HOUR)
    due = next_step(late)
    assert isinstance(due, NextStep)
    assert due.strategy == "remind"
    assert due.is_due


def test_a_plan_that_ends_in_a_wait_is_exhausted_not_stuck() -> None:
    ledger = _ledger_with_plan(("wait", 12))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 99 * HOUR)
    decision = next_step(state)
    assert isinstance(decision, Halt)
    assert decision.reason is StopReason.PLAN_EXHAUSTED


# ------------------------------------------------------- plan and state edges


def test_a_payment_with_no_contacts_yet_defers_to_the_primary_strategy() -> None:
    """The opening action lives on the proposal, not in `follow_up`, so this module
    must not claim to own it."""
    ledger = _ledger_with_plan(("remind", 24))
    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger))
    assert state.touches == 0

    decision = next_step(state)
    assert isinstance(decision, Halt)
    assert "opening action" in decision.detail


def test_an_empty_plan_means_one_shot_and_done() -> None:
    """The behaviour of the whole system before sequencing existed, preserved."""
    ledger = _ledger_with_plan()
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 99 * HOUR)
    decision = next_step(state)
    assert isinstance(decision, Halt)
    assert decision.reason is StopReason.PLAN_EXHAUSTED


def test_the_model_may_stop_early_and_that_is_respected() -> None:
    """A plan can narrow what the guardrails permit. Three contacts are allowed; the
    model asks for one and gets one."""
    ledger = _ledger_with_plan(("do_nothing", 0))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 99 * HOUR)
    decision = next_step(state, policy=ContactPolicy(max_touches=3))
    assert isinstance(decision, Halt)
    assert decision.reason is StopReason.MODEL_STOPPED


def test_an_unknown_strategy_halts_rather_than_guessing() -> None:
    """A model inventing a strategy gets nothing. Guessing at intent is how an
    unreviewed capability arrives."""
    ledger = _ledger_with_plan(("wire_transfer_to_agent", 0))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger) + 99 * HOUR)
    decision = next_step(state)
    assert isinstance(decision, Halt)
    assert decision.reason is StopReason.MODEL_STOPPED
    assert "maps to no tool" in decision.detail


def test_a_payment_nobody_proposed_anything_for_has_no_sequence() -> None:
    ledger = _ledger_with_plan(("remind", 1))
    state = derive_state(ledger.read("s"), "pay_unrelated", now_epoch=_now(ledger))
    assert state.reference_id is None
    assert state.plan == ()


def test_state_ignores_actions_belonging_to_a_different_payment() -> None:
    """Scoped by `reference_id`. Counting another payment's links would let one
    customer's recovery consume another's contact budget."""
    ledger = _ledger_with_plan(("remind", 1))
    _record_action(ledger, "create_payment_link_upi", "plink_mine", seq=1)
    ledger.append(
        "s",
        "result_recorded",
        {
            "tool": "create_payment_link",
            "result": {
                "id": "plink_theirs",
                "amount": 100000,
                "currency": INR,
                "reference_id": "recover-pay_other",
            },
        },
        step_seq=2,
    )

    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger))
    assert state.touches == 1
    assert state.live_link_ids == ("plink_mine",)


def test_derivation_is_pure_and_repeatable() -> None:
    """A stored cursor can disagree with the evidence; a derived one cannot."""
    ledger = _ledger_with_plan(("remind", 12), ("payment_link", 24))
    _record_action(ledger, "create_payment_link_upi", "plink_1", seq=1)
    events, now = ledger.read("s"), _now(ledger)
    assert derive_state(events, PAYMENT, now_epoch=now) == derive_state(
        events, PAYMENT, now_epoch=now
    )


# ------------------------------------------------------------------ recording


def test_stopping_is_recorded_with_its_reason() -> None:
    """"We stopped because they paid" and "we stopped because we hit the limit" are
    very different facts about a merchant's recovery process."""
    ledger = _ledger_with_plan()
    halt = Halt(PAYMENT, StopReason.CONTACT_LIMIT, "3 of 3 contacts used")
    record_stop(ledger, "s", halt, step_seq=9)

    stopped = [e for e in ledger.read("s") if e.type == SEQUENCE_STOPPED]
    assert len(stopped) == 1
    assert stopped[0].payload["reason"] == "contact_limit"
    assert stopped[0].payload["detail"] == "3 of 3 contacts used"
    assert verify_chain(ledger.read("s")).ok


def test_the_recorded_plan_survives_in_the_ledger() -> None:
    """A sequence spans days, so the plan has to outlive the process that made it --
    and being in the ledger makes it evidence a merchant can review."""
    ledger = _ledger_with_plan(("wait", 26), ("upi_link", 0), ("remind", 48))
    state = derive_state(ledger.read("s"), PAYMENT, now_epoch=_now(ledger))
    assert [s["strategy"] for s in state.plan] == ["wait", "upi_link", "remind"]
    assert state.plan[0]["wait_hours"] == 26


def test_state_is_frozen() -> None:
    """Derived facts must not be editable in place, or a caller can quietly grant
    itself another contact."""
    import dataclasses

    import pytest

    state = SequenceState(payment_id=PAYMENT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.touches = 99  # type: ignore[misc]


def _now(ledger: LedgerStore) -> float:
    """The timestamp of the newest event, so tests are anchored to the ledger rather
    than to the wall clock -- the same discipline the fixtures use."""
    return max(event_epoch(event) for event in ledger.read("s"))
