"""Cumulative spend and action velocity, folded from the ledger.

The gap this closes, in one sentence: **per-action limits do not bound an
agent.** Forty payment links of Rs 4,000 each pass a Rs 5,000 per-action ceiling
while breaching a Rs 50,000 daily budget by more than three times. An agent
trusted to move money autonomously has to be bounded in aggregate, or it is not
bounded at all.

`belay/policy/model.py::Cap` declared `per: session` from the beginning and
`PolicyEngine` never read it -- its own docstring said so, and proposed the fix:
"add a session accumulator (likely in the ledger, via
`plan_created`/`policy_evaluated` replay)". This module is that accumulator.

Design constraints inherited from `belay/policy/quota.py`, which solved the same
shape of problem for *counting* irreversible actions:

- **The ledger is the only source of truth.** No second in-memory tally to drift
  out of sync, and no state that a crash could lose. A restarted process
  recomputes the same number from the same events.
- **Only authorized AND executed actions count.** A denied action spent nothing.
  A paused action still awaiting a human spent nothing. Counting either would
  make the budget shrink for actions that never happened.
- **Keyed by `plan_id`, never `step_seq`.** This is the subtlety that makes the
  whole thing correct. A paused call's retry re-plans under a *new* `step_seq`
  once approved, so matching an approval back to the step that actually executed
  only works through the `plan_id` both share. Keying by `step_seq` double-counts
  every human-approved action -- the exact bug this note exists to prevent.

Two scopes, because two different people set them and both must hold:

- `spent_by_merchant` -- the MERCHANT's aggregate ceiling from their mandate,
  across every session that merchant's agent has run in the window.
- `spent_in_session` -- the OPERATOR's `per: session` cap from `policy.yaml`,
  bounded to one session.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from belay.finance.money import Money
from belay.finance.money import total as sum_money
from belay.ledger.model import Event
from belay.ledger.store import LedgerStore

#: The only event types the fold needs. Reading just these avoids loading
#: `state_captured` / `result_recorded` payloads, which carry whole upstream
#: responses and dominate the table's size.
FOLD_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "session_started",
        "plan_created",
        "policy_evaluated",
        "approval_resolved",
        "step_committed",
    }
)


@dataclass(frozen=True)
class AuthorizedAction:
    """One action that was both authorized and actually executed.

    The unit both cumulative spend and velocity are computed over. `amounts`
    holds every `spend` effect's value from the plan -- a list rather than a
    single value because one action may declare several (a link creation
    declares both a `create` and a `spend` effect, and a future action might
    declare more than one monetary leg).
    """

    session_id: str
    merchant_id: str | None
    identity: str | None
    step_seq: int
    plan_id: str
    tool: str
    reversibility: str
    verdict: str
    at: datetime
    amounts: tuple[Money, ...]

    def spend_in(self, currency: str) -> Money:
        """This action's total `spend` value in `currency`, ignoring other currencies."""
        return sum_money([a for a in self.amounts if a.currency == currency], currency=currency)


def _money_from_payload(raw: Any) -> Money | None:
    """Rebuild `Money` from a serialized `EffectEstimate.amount`.

    Returns `None` for a malformed value rather than raising: this runs over
    historical ledger events, and one unparseable old event must not make every
    future limit check fail. A `spend` effect whose amount cannot be read is
    surfaced by `unaccounted_spend_effects` below instead of being silently
    treated as zero.
    """
    if not isinstance(raw, dict):
        return None
    minor = raw.get("minor_units")
    currency = raw.get("currency")
    if isinstance(minor, bool) or not isinstance(minor, int) or not isinstance(currency, str):
        return None
    try:
        return Money(minor_units=minor, currency=currency)
    except Exception:  # pragma: no cover - defensive against old event shapes
        return None


def _spend_amounts(plan_payload: dict[str, Any]) -> tuple[tuple[Money, ...], int]:
    """`(amounts, unreadable_count)` for the `spend` effects of one plan."""
    amounts: list[Money] = []
    unreadable = 0
    for effect in plan_payload.get("effects", []):
        if not isinstance(effect, dict) or effect.get("type") != "spend":
            continue
        amount = _money_from_payload(effect.get("amount"))
        if amount is None:
            unreadable += 1
        else:
            amounts.append(amount)
    return tuple(amounts), unreadable


@dataclass(frozen=True)
class FoldResult:
    """Authorized actions, plus an honest count of what could not be read."""

    actions: tuple[AuthorizedAction, ...]
    #: `spend` effects whose amount could not be parsed from the ledger. Not
    #: silently zero: a limit computed while ignoring an unreadable spend would
    #: under-count the budget, which fails in the permissive direction. Callers
    #: that must not fail open check this.
    unaccounted_spend_effects: int


def fold_authorized_actions(events: list[Event]) -> FoldResult:
    """Fold a ledger event list into the actions that were authorized and executed.

    Pure: no I/O, no clock. The same events always produce the same result, which
    is what lets a limit decision be replayed and audited after the fact.
    """
    by_session: dict[str, list[Event]] = {}
    for event in events:
        by_session.setdefault(event.session_id, []).append(event)

    actions: list[AuthorizedAction] = []
    unaccounted = 0

    for session_id, session_events in by_session.items():
        merchant_id: str | None = None
        identity: str | None = None
        plan_of: dict[int, dict[str, Any]] = {}
        plan_id_of: dict[int, str] = {}
        verdict_of: dict[int, str] = {}
        at_of: dict[int, datetime] = {}
        committed: set[int] = set()
        approved_plan_ids: set[str] = set()

        for event in session_events:
            if event.type == "session_started":
                merchant_id = event.payload.get("merchant_id")
                identity = event.initiated_by
                continue
            if event.type == "approval_resolved":
                if event.payload.get("state") == "approved":
                    plan_id = event.payload.get("plan_id")
                    if isinstance(plan_id, str):
                        approved_plan_ids.add(plan_id)
                continue

            step = event.step_seq
            if step is None:
                continue
            if event.type == "plan_created":
                plan_of[step] = event.payload
                plan_id = event.payload.get("plan_id")
                if isinstance(plan_id, str):
                    plan_id_of[step] = plan_id
            elif event.type == "policy_evaluated":
                verdict = event.payload.get("verdict")
                if isinstance(verdict, str):
                    verdict_of[step] = verdict
                at_of[step] = datetime.fromisoformat(event.at)
            elif event.type == "step_committed":
                committed.add(step)

        for step, plan_payload in plan_of.items():
            verdict = verdict_of.get(step)
            plan_id = plan_id_of.get(step)
            when = at_of.get(step)
            if verdict is None or plan_id is None or when is None:
                continue

            # Authorized AND executed. `allow` needs only the commit; `pause`
            # additionally needs a human's approval against this plan_id.
            if verdict == "allow":
                executed = step in committed
            elif verdict == "pause":
                executed = plan_id in approved_plan_ids and step in committed
            else:  # deny, or anything unrecognised -- never counts
                executed = False
            if not executed:
                continue

            amounts, unreadable = _spend_amounts(plan_payload)
            unaccounted += unreadable
            actions.append(
                AuthorizedAction(
                    session_id=session_id,
                    merchant_id=merchant_id,
                    identity=identity,
                    step_seq=step,
                    plan_id=plan_id,
                    tool=str(plan_payload.get("tool", "")),
                    reversibility=str(plan_payload.get("reversibility", "")),
                    verdict=verdict,
                    at=when,
                    amounts=amounts,
                )
            )

    actions.sort(key=lambda a: (a.at, a.session_id, a.step_seq))
    return FoldResult(actions=tuple(actions), unaccounted_spend_effects=unaccounted)


def in_window(
    actions: tuple[AuthorizedAction, ...], *, now: datetime, window: timedelta
) -> tuple[AuthorizedAction, ...]:
    """Actions whose deciding moment falls within `window` of `now`.

    Boundary rule matches `belay/policy/quota.py`: an action exactly `window` old
    still counts. One convention, so two limits cannot disagree about whether the
    same action is inside the window.
    """
    cutoff = now - window
    return tuple(action for action in actions if cutoff <= action.at <= now)


@dataclass
class CumulativeTracker:
    """Aggregate spend and action counts over a rolling window, from the ledger."""

    ledger: LedgerStore

    def _fold(self) -> FoldResult:
        return fold_authorized_actions(self.ledger.read_by_types(FOLD_EVENT_TYPES))

    def spent_by_merchant(
        self, merchant_id: str, *, currency: str, now: datetime, window: timedelta
    ) -> Money:
        """Total authorized-and-executed spend for `merchant_id` within the window.

        Spans every session that merchant's agent has run: a mandate's daily
        ceiling must not be resettable by starting a new session.
        """
        actions = in_window(self._fold().actions, now=now, window=window)
        return sum_money(
            [
                action.spend_in(currency)
                for action in actions
                if action.merchant_id == merchant_id
            ],
            currency=currency,
        )

    def actions_by_merchant(
        self, merchant_id: str, *, now: datetime, window: timedelta
    ) -> int:
        """Count of authorized-and-executed actions for `merchant_id` in the window.

        Velocity is deliberately independent of amount: a hundred Rs 1 links is a
        pattern worth stopping even though the money involved is trivial.
        """
        actions = in_window(self._fold().actions, now=now, window=window)
        return sum(1 for action in actions if action.merchant_id == merchant_id)

    def spent_in_session(self, session_id: str, *, currency: str) -> Money:
        """Total authorized-and-executed spend within one session.

        Backs `Cap.per: session` in `PolicyEngine`. Unwindowed by design: a
        session is already a bounded scope, and `per: session` says nothing about
        time.
        """
        result = fold_authorized_actions(self.ledger.read(session_id))
        return sum_money(
            [action.spend_in(currency) for action in result.actions], currency=currency
        )

    def unaccounted_spend_effects(self) -> int:
        """`spend` effects in the ledger whose amount could not be parsed."""
        return self._fold().unaccounted_spend_effects
