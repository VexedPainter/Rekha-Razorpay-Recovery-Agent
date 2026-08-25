"""Prioritisation: which recoveries to attempt, given a finite budget.

The split here is the project's thesis applied to one function. The model
contributes `expected_recovery` -- a judgement about how likely a customer is to
pay, which is genuinely hard and genuinely fuzzy. Everything after that is
arithmetic, and arithmetic belongs in code:

- ranking by expected value
- packing under the mandate's remaining budget
- refusing anything the mandate would refuse anyway

Deterministic given the same scores. Two runs over the same proposals produce the
same selection, in the same order, which is what makes a batch metric comparable
and a demo rehearsable.

Note what this module does NOT do: it does not decide whether an action is
permitted. It filters out proposals that would obviously be refused, purely to
avoid wasting a call and a ledger entry on a foregone conclusion -- but the
control plane still checks every one independently. A filter here is an
optimisation, never an authorization. If this module were removed entirely, the
system would be slower and equally safe.
"""

from __future__ import annotations

from rekha.finance.money import Money
from rekha.finance.money import total as sum_money

from recovery.proposal import RecoveryPlan, RecoveryProposal


def _sort_key(proposal: RecoveryProposal) -> tuple[int, int, str]:
    """Highest expected recovery first; ties broken by lower request, then id.

    Preferring the smaller request on a tie is deliberate: two proposals with the
    same expected value but different asks are not equally good, because the
    cheaper one leaves more budget for the next recovery. The `payment_id` tail
    makes the order total, so the result cannot vary between runs on tie order.
    """
    return (
        -proposal.expected_recovery.minor_units,
        proposal.amount.minor_units,
        proposal.payment_id,
    )


def prioritize(
    proposals: list[RecoveryProposal],
    *,
    currency: str = "INR",
    remaining_budget: Money | None = None,
    max_per_action: Money | None = None,
    max_actions: int | None = None,
    min_expected_recovery: Money | None = None,
) -> RecoveryPlan:
    """Rank `proposals` and select those that fit.

    `remaining_budget` is what is left of the mandate's aggregate ceiling right
    now -- passed in rather than computed here, because reading it requires the
    ledger and this package is forbidden from touching it. That constraint is the
    boundary doing its job: the AI layer is told what it has to work with, and
    cannot look it up or reinterpret it.

    Everything not selected is reported, split by reason:
      `not_worth_pursuing`  -- the model proposed `do_nothing`, or the expected
                               value is below the floor, or the ask exceeds the
                               per-action ceiling so it can never be authorized
      `declined_for_budget` -- worth pursuing, no room left

    A batch that silently omitted the second category would look like an agent
    that recovered everything worth recovering.
    """
    plan = RecoveryPlan(currency=currency)

    candidates: list[RecoveryProposal] = []
    for proposal in proposals:
        if not proposal.is_actionable:
            plan.not_worth_pursuing.append(proposal)
            continue
        if proposal.amount.currency != currency:
            plan.not_worth_pursuing.append(proposal)
            continue
        if max_per_action is not None and proposal.amount > max_per_action:
            # Cannot be authorized at any point in the window, so pursuing it
            # would spend a request to be told no. The mandate is still what
            # actually refuses it.
            plan.not_worth_pursuing.append(proposal)
            continue
        if (
            min_expected_recovery is not None
            and proposal.expected_recovery < min_expected_recovery
        ):
            plan.not_worth_pursuing.append(proposal)
            continue
        candidates.append(proposal)

    candidates.sort(key=_sort_key)

    spent = Money.zero(currency)
    for proposal in candidates:
        if max_actions is not None and len(plan.selected) >= max_actions:
            plan.declined_for_budget.append(proposal)
            continue
        if remaining_budget is not None and spent + proposal.amount > remaining_budget:
            # Skip, do not stop. A large proposal that does not fit must not block
            # smaller ones that do -- greedy-by-value with a skip recovers more
            # money than halting at the first thing too big to afford.
            plan.declined_for_budget.append(proposal)
            continue
        plan.selected.append(proposal)
        spent = spent + proposal.amount

    return plan


def revenue_at_risk(snapshots_amounts: list[Money], *, currency: str = "INR") -> Money:
    """Total value of the failed payments considered. The batch's denominator."""
    return sum_money(
        [amount for amount in snapshots_amounts if amount.currency == currency],
        currency=currency,
    )
