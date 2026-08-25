"""Advancing a multi-step recovery, one step at a time, from evidence.

A single failed payment is rarely recovered by a single action. The customer who
ignored a UPI collect request on Tuesday might pay a reminder on Friday; an
insufficient-funds failure two days before payday should not be retried until after
it. So the agent proposes a *sequence*: an opening action plus what to do if it does
not work.

This module decides what actually happens. It is deliberately dull, deterministic
and separate from the AI.

## An approved plan is not a standing authorization

The obvious way to implement sequencing is to let an approved plan run: the merchant
says yes once, and the agent takes four actions over a week. That is wrong, and it is
the failure mode worth designing against.

Approval is granted against the situation as it stood. By day four the amount may
have been refunded, the customer may have paid by other means, the merchant's
cumulative cap may be exhausted, or the mandate may have been narrowed. So every
step is re-derived from current evidence and re-checked against the mandate, the caps
and the velocity limits at the moment it would run. The plan is a *recommendation
with an order*, and nothing more.

Concretely: this module never executes. It returns the next step it believes is
permitted, and the caller puts that through the same `resolve -> plan -> policy ->
approve -> execute` path any first action goes through. There is no fast path for a
step that was "already approved", because that fast path is exactly how an
unsupervised agent gets four actions out of one approval.

## At most one live payment link per payment

The invariant that matters financially. If step 2 creates a UPI link and step 3
creates a card link, the customer now holds two valid demands for the same order and
can pay both. The merchant has to refund one, having paid fees on both, and looks
incompetent to a customer who did what they were asked.

So escalating the method requires CANCELLING the live link first, and that cancel is
part of the sequence rather than a hopeful assumption. `Strategy.REMIND` exists
precisely so the common second touch -- nudge the customer about the link they
already have -- creates no new obligation at all.

## Stopping rules are the control plane's, not the model's

A model asked "how many times should I contact this customer?" will give a plausible
answer that varies between runs and between payments. Contact limits are a merchant
policy decision with legal and brand consequences, so they are configuration with a
conservative default, enforced here. The model may propose fewer steps than the limit
allows; it cannot propose more and have them run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from rekha.finance.money import Money
from rekha.ledger.model import Event
from rekha.ledger.store import LedgerStore
from rekha.razorpay.forecast import RECOVERY_PROPOSED
from rekha.razorpay.webhooks import WEBHOOK_RECEIVED

#: Ledger event type recording that a sequence stopped, and why. A sequence that
#: ends silently is indistinguishable from one that crashed.
SEQUENCE_STOPPED = "recovery_sequence_stopped"

#: Tools that create a NEW payment obligation, as opposed to nudging an existing one.
_LINK_CREATING_TOOLS = frozenset({"create_payment_link", "create_payment_link_upi"})


def event_epoch(event: Event) -> float:
    """An event's timestamp as epoch seconds.

    The ledger stores `at` as an ISO-8601 string, which is the right choice for an
    audit record a human may read years later, but sequencing arithmetic needs a
    number. Converted at the boundary rather than storing both, because two
    representations of one timestamp is two chances to disagree.
    """
    return datetime.fromisoformat(event.at).timestamp()


class StopReason(StrEnum):
    """Why a sequence went no further. Every one of these is a normal outcome."""

    #: The customer paid. The only reason anybody wanted.
    RECOVERED = "recovered"
    #: The plan ran out of steps.
    PLAN_EXHAUSTED = "plan_exhausted"
    #: The contact limit was reached. A guardrail, not a failure.
    CONTACT_LIMIT = "contact_limit"
    #: Too long has passed since the original failure for chasing to be decent.
    WINDOW_CLOSED = "window_closed"
    #: The model itself concluded further contact was not worth it.
    MODEL_STOPPED = "model_stopped"


@dataclass(frozen=True)
class SequenceState:
    """What has actually happened to one payment so far, folded from the ledger.

    Derived, never stored. A stored cursor can disagree with the evidence -- and
    when it does, an agent acts on a version of reality nobody can audit.
    """

    payment_id: str
    reference_id: str | None = None
    #: Contacts already made. Counts actions that reached a customer, so a cancel
    #: does not consume a touch: withdrawing a demand is not pestering somebody.
    touches: int = 0
    #: Links created and not yet cancelled or paid. More than one is a bug.
    live_link_ids: tuple[str, ...] = ()
    #: What webhooks say arrived. Zero is not "not yet" -- it is "no evidence".
    paid: Money | None = None
    #: The steps proposed for this payment, in order, as recorded at proposal time.
    plan: tuple[dict[str, Any], ...] = ()
    #: Hours since the most recent contact, or None if never contacted.
    hours_since_last_touch: float | None = None
    #: Hours since the original payment failed.
    age_hours: float = 0.0
    events_seen: tuple[str, ...] = field(default=())

    @property
    def is_recovered(self) -> bool:
        return self.paid is not None and bool(self.paid) and not self.paid.is_negative

    @property
    def has_live_link(self) -> bool:
        return bool(self.live_link_ids)


@dataclass(frozen=True)
class ContactPolicy:
    """Merchant limits on chasing one payment. Deliberately conservative.

    Defaults chosen to be defensible rather than optimal: three contacts over seven
    days, at least twelve hours apart. A real merchant may well permit more, and
    that is a configuration change -- but over-contacting is the failure mode that
    damages a brand, and it is not recoverable by apologising afterwards.
    """

    max_touches: int = 3
    max_window_hours: int = 168
    min_gap_hours: int = 12


@dataclass(frozen=True)
class NextStep:
    """The one action the control plane believes is permitted next.

    `requires_cancel_of` is populated when the step would create a second live
    demand. The caller must cancel that link first, and the cancel goes through
    policy like anything else.
    """

    payment_id: str
    strategy: str
    tool: str | None
    rationale: str = ""
    requires_cancel_of: str | None = None
    #: Set when a step is not due yet, so a caller can schedule rather than poll.
    wait_hours_remaining: float = 0.0

    @property
    def is_due(self) -> bool:
        return self.wait_hours_remaining <= 0


@dataclass(frozen=True)
class Halt:
    """The sequence is over. Carries the reason so it can be recorded and explained."""

    payment_id: str
    reason: StopReason
    detail: str = ""


def _tool_for(strategy: str) -> str | None:
    """Strategy to tool, for the control-plane side.

    Mirrors `RecoveryProposal.tool` deliberately rather than importing it: `rekha/`
    must not depend on `recovery/`, because the control plane has to be able to
    judge a proposal without the AI layer installed at all.
    """
    return {
        "upi_link": "create_payment_link_upi",
        "payment_link": "create_payment_link",
        "remind": "send_payment_link",
    }.get(strategy)


def derive_state(
    events: list[Event],
    payment_id: str,
    *,
    now_epoch: float,
    currency: str = "INR",
) -> SequenceState:
    """Fold the ledger into what is true about one payment right now.

    Pure. Given the same events and the same clock it returns the same state, which
    is what makes a sequencing decision reproducible in an audit months later.
    """
    reference_id: str | None = None
    plan: tuple[dict[str, Any], ...] = ()
    created_at: float | None = None
    touches = 0
    last_touch_epoch: float | None = None
    live: dict[str, None] = {}
    paid: Money | None = None
    seen: list[str] = []
    our_link_ids: set[str] = set()

    for event in events:
        if event.type == RECOVERY_PROPOSED and event.payload.get("payment_id") == payment_id:
            reference_id = event.payload.get("reference_id") or reference_id
            raw_plan = event.payload.get("follow_up")
            if isinstance(raw_plan, list):
                plan = tuple(step for step in raw_plan if isinstance(step, dict))
            if created_at is None:
                created_at = event_epoch(event)

    if reference_id is None:
        # Nothing was ever proposed for this payment, so there is no sequence.
        return SequenceState(payment_id=payment_id, plan=())

    for event in events:
        if event.type == "result_recorded":
            result = event.payload.get("result")
            if not isinstance(result, dict):
                continue
            if result.get("reference_id") != reference_id:
                continue
            tool = str(event.payload.get("tool") or "")
            link_id = result.get("id")
            if tool in _LINK_CREATING_TOOLS and isinstance(link_id, str):
                live[link_id] = None
                our_link_ids.add(link_id)
                touches += 1
                last_touch_epoch = event_epoch(event)
            elif tool == "send_payment_link":
                # A reminder about a link we already sent. A contact, but no new
                # obligation, so the live set is unchanged.
                touches += 1
                last_touch_epoch = event_epoch(event)
            elif tool == "cancel_payment_link" and isinstance(link_id, str):
                live.pop(link_id, None)

        elif event.type == WEBHOOK_RECEIVED:
            link_id = event.payload.get("payment_link_id")
            if not isinstance(link_id, str) or link_id not in our_link_ids:
                continue
            seen.append(str(event.payload.get("event")))
            if not event.payload.get("is_payment"):
                continue
            raw = event.payload.get("amount_paid") or event.payload.get("amount")
            if not isinstance(raw, dict):
                continue
            minor, cur = raw.get("minor_units"), raw.get("currency")
            if isinstance(minor, bool) or not isinstance(minor, int) or not isinstance(cur, str):
                continue
            amount = Money(minor_units=minor, currency=cur)
            if paid is None or amount > paid:
                paid = amount
            # A paid link is no longer an outstanding demand.
            live.pop(link_id, None)

    return SequenceState(
        payment_id=payment_id,
        reference_id=reference_id,
        touches=touches,
        live_link_ids=tuple(sorted(live)),
        paid=paid,
        plan=plan,
        hours_since_last_touch=(
            (now_epoch - last_touch_epoch) / 3600.0 if last_touch_epoch is not None else None
        ),
        age_hours=((now_epoch - created_at) / 3600.0 if created_at is not None else 0.0),
        events_seen=tuple(seen),
    )


def next_step(
    state: SequenceState,
    *,
    policy: ContactPolicy | None = None,
) -> NextStep | Halt:
    """Decide the next action for one payment, or halt with a reason.

    Checked in order of authority: facts first, then merchant limits, then the
    model's plan. A plan can only ever narrow what the guardrails already permit,
    which is why a compromised or hallucinating model cannot talk its way into a
    fourth contact.
    """
    limits = policy or ContactPolicy()

    # 1. FACTS. The customer paid, so there is nothing left to want.
    if state.is_recovered:
        return Halt(state.payment_id, StopReason.RECOVERED, f"paid {state.paid}")

    # 2. MERCHANT LIMITS. Not negotiable by the model.
    if state.touches >= limits.max_touches:
        return Halt(
            state.payment_id,
            StopReason.CONTACT_LIMIT,
            f"{state.touches} of {limits.max_touches} contacts used",
        )
    if state.age_hours > limits.max_window_hours:
        return Halt(
            state.payment_id,
            StopReason.WINDOW_CLOSED,
            f"{state.age_hours:.0f}h since failure exceeds {limits.max_window_hours}h",
        )

    # 3. THE MODEL'S PLAN.
    #
    # The opening action lives on the proposal, not in `follow_up`, so a payment
    # with no contacts yet is not this module's business -- the caller executes the
    # primary strategy through the normal path first.
    if state.touches == 0:
        return Halt(
            state.payment_id,
            StopReason.PLAN_EXHAUSTED,
            "opening action has not run; execute the proposal's primary strategy first",
        )

    # Steps already taken are consumed by contacts made: the first contact used the
    # opening action, so `follow_up[touches - 1]` is next.
    #
    # WAIT STEPS ADVANCE THE CURSOR WITHOUT CONSUMING A CONTACT, which is the whole
    # point of them. Rather than recursing -- which corrupted the touch count and
    # could fire a false contact-limit halt -- their delays are accumulated into the
    # delay of the next real action. "Wait 26h, then send a UPI link" and "send a UPI
    # link in 26h" describe the same intent, and collapsing them keeps one cursor.
    accumulated_wait = 0.0
    step: dict[str, Any] | None = None
    for candidate in state.plan[state.touches - 1 :]:
        strategy_name = str(candidate.get("strategy") or "")
        if strategy_name == "wait":
            accumulated_wait += float(candidate.get("wait_hours") or 0)
            continue
        step = candidate
        break

    if step is None:
        detail = (
            f"plan of {len(state.plan)} steps ends in a wait"
            if state.plan
            else "no follow-up steps proposed"
        )
        return Halt(state.payment_id, StopReason.PLAN_EXHAUSTED, detail)

    strategy = str(step.get("strategy") or "")
    rationale = str(step.get("rationale") or "")

    if strategy == "do_nothing":
        return Halt(state.payment_id, StopReason.MODEL_STOPPED, rationale or "plan ends here")

    tool = _tool_for(strategy)
    if tool is None:
        return Halt(
            state.payment_id, StopReason.MODEL_STOPPED, f"strategy {strategy!r} maps to no tool"
        )

    # Timing. The model's requested delay, plus any waits standing in front of it,
    # floored by the merchant's minimum gap -- so a plan asking for an immediate
    # second touch is slowed rather than refused.
    requested_wait = accumulated_wait + float(step.get("wait_hours") or 0)
    required_wait = max(requested_wait, float(limits.min_gap_hours))
    elapsed = state.hours_since_last_touch
    if elapsed is not None and elapsed < required_wait:
        return NextStep(
            payment_id=state.payment_id,
            strategy=strategy,
            tool=tool,
            rationale=rationale,
            wait_hours_remaining=required_wait - elapsed,
        )

    # THE FINANCIAL INVARIANT. A step that creates a new demand while one is still
    # live must cancel the old one first, or the customer can pay twice.
    requires_cancel: str | None = None
    if tool in _LINK_CREATING_TOOLS and state.has_live_link:
        requires_cancel = state.live_link_ids[0]

    return NextStep(
        payment_id=state.payment_id,
        strategy=strategy,
        tool=tool,
        rationale=rationale,
        requires_cancel_of=requires_cancel,
    )


def record_stop(
    ledger: LedgerStore, session_id: str, halt: Halt, *, step_seq: int | None = None
) -> Event:
    """Write down that a sequence ended, and why.

    Worth an event of its own. "We stopped contacting this customer because they
    paid" and "we stopped because we hit the contact limit" are very different facts
    about a merchant's recovery process, and a sequence that just stops appearing in
    the log is indistinguishable from one that crashed.
    """
    return ledger.append(
        session_id,
        SEQUENCE_STOPPED,
        {
            "payment_id": halt.payment_id,
            "reason": str(halt.reason),
            "detail": halt.detail,
        },
        step_seq=step_seq,
    )
