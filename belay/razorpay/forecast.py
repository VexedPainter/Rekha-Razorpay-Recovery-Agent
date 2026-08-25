"""Recording an AI forecast into the ledger, so it can be scored later.

The AI predicts "this failure has roughly a 60% chance of recovering, so the
expected value is INR 1,440". Webhooks later reveal what actually happened. Nothing
could compare those two, because the forecast was never written down -- it lived
only in the process that made it.

That is the gap this module closes, and it is a prerequisite for calibration rather
than a nicety: a forecast that is not recorded cannot be scored, and a forecaster
that is never scored is indistinguishable from a random one.

**Why this lives in `belay/` and not in `recovery/`.** The AI layer is forbidden
from importing `belay.ledger`, and that restriction is load-bearing -- an agent able
to write its own evidence could rewrite its own track record. So the control plane
takes the proposal it was handed and records it. The AI cannot choose what is
remembered about it.

The event carries `reference_id`, which is the same handle the payment link, the
webhook, and the settlement entry all use. That is what lets a forecast be joined to
its outcome months later from evidence alone.
"""

from __future__ import annotations

from typing import Any

from belay.finance.money import Money
from belay.ledger.model import Event
from belay.ledger.store import LedgerStore

#: Ledger event type for one recorded AI forecast.
RECOVERY_PROPOSED = "recovery_proposed"


def record_proposal(
    ledger: LedgerStore,
    session_id: str,
    *,
    payment_id: str,
    reference_id: str,
    cause_class: str,
    strategy: str,
    amount: Money,
    expected_recovery: Money,
    confidence: str,
    diagnosis: str,
    reasoning: str,
    prompt_version: str,
    provider: str,
    model: str,
    selected: bool,
    step_seq: int | None = None,
) -> Event:
    """Append one AI forecast to the ledger.

    `selected` distinguishes a forecast the system acted on from one it declined to
    pursue. Both are recorded, because a calibration score computed only over the
    actions taken is biased: the agent chose those precisely because it was
    confident about them, so scoring only those measures confidence on easy cases.
    """
    return ledger.append(
        session_id,
        RECOVERY_PROPOSED,
        {
            "payment_id": payment_id,
            "reference_id": reference_id,
            "cause_class": cause_class,
            "strategy": strategy,
            "amount": amount.model_dump(mode="json"),
            "expected_recovery": expected_recovery.model_dump(mode="json"),
            # The forecast as a probability, which is what a calibration score
            # needs. Derived rather than asked for separately: an expected value of
            # INR 1,440 on an INR 2,400 failure IS a 60% forecast, and asking the
            # model for both invites the two to disagree.
            "implied_probability": (
                expected_recovery.minor_units / amount.minor_units
                if amount.minor_units > 0
                else 0.0
            ),
            "confidence": confidence,
            "diagnosis": diagnosis,
            "reasoning": reasoning,
            "prompt_version": prompt_version,
            "provider": provider,
            "model": model,
            "selected": selected,
        },
        step_seq=step_seq,
    )


def recorded_proposals(events: list[Event]) -> list[dict[str, Any]]:
    """Every recorded forecast in an event list, oldest first."""
    return [dict(event.payload) for event in events if event.type == RECOVERY_PROPOSED]
