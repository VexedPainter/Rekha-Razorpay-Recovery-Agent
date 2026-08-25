"""`RecoveryProposal`: the sole output of the AI layer.

The AI's entire power is to fill in one of these. It cannot execute it, approve
it, record it, or widen the mandate that will judge it. Everything downstream
treats a proposal as an untrusted request, which is why the fields are strict and
why the model is validated wholesale rather than field-by-field: a partially
applied proposal is a proposal nobody wrote.

Two ideas worth noting in the design:

**Deterministic bounds on a non-deterministic output.** A model can claim any
`expected_recovery` it likes. `clamped()` caps it at the original payment amount,
because recovering more than was lost is not a recovery -- it is a hallucination
with a rupee sign in front of it. Bounding after the fact, rather than trusting
the prompt, is the same discipline the whole project applies to the model.

**Provenance.** `prompt_version` records which prompt produced the reasoning, and
`provider` / `model` record what answered. So a decision in the ledger can be
traced to the exact instruction and the exact model that produced it, rather than
to "the AI said so" -- and a later change of prompt or model is visible as a
change in evidence rather than an invisible drift in behaviour.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field
from rekha.finance.money import Money


class CauseClass(StrEnum):
    """Why the payment failed, in terms that imply what to do about it.

    Deliberately a small closed set. Razorpay's raw error surface is large,
    text-heavy and sparse, and the point of diagnosis is to collapse it into the
    handful of distinctions that actually change the recovery decision. A
    twenty-value taxonomy would look thorough and change nothing.
    """

    #: The customer could pay and simply did not complete: OTP not entered,
    #: bank page abandoned, UPI collect request ignored. A fresh link usually works.
    CUSTOMER_RECOVERABLE = "customer_recoverable"

    #: A transient failure at the bank or gateway. The same instrument will
    #: probably work on a retry, with no customer action needed.
    BANK_TRANSIENT = "bank_transient"

    #: The customer wanted to pay and could not afford to. Recoverable, but
    #: later, or for less -- not by retrying immediately.
    INSUFFICIENT_FUNDS = "insufficient_funds"

    #: Authentication itself failed (3DS, OTP mismatch). Often recoverable with
    #: a fresh attempt, sometimes indicates a card the customer has lost access to.
    AUTHENTICATION_FAILED = "authentication_failed"

    #: The instrument cannot work: expired card, unsupported method, limit set on
    #: the card. Recoverable only via a *different* payment method.
    METHOD_UNSUPPORTED = "method_unsupported"

    #: Risk-blocked, fraud-flagged, or otherwise refused for reasons that will
    #: not change. Chasing it wastes money and annoys a customer.
    PERMANENTLY_DEAD = "permanently_dead"


class Strategy(StrEnum):
    """The intervention proposed. Maps to what the control plane can actually do.

    Every value here except `DO_NOTHING` corresponds to a tool in
    `packs/razorpay/contracts.yaml`. A strategy the system cannot execute would
    be a proposal that always fails, so the enum is bounded by the contract pack
    rather than by what sounds plausible.
    """

    #: Send a standard payment link the customer can pay by any method.
    PAYMENT_LINK = "payment_link"

    #: Send a UPI-only link. Cheaper, faster, and the dominant Indian rail --
    #: usually the right choice when the original attempt was card-based and the
    #: customer has a UPI id on file.
    UPI_LINK = "upi_link"

    #: Resend an EXISTING link rather than creating another one. A second touch
    #: with no second payment obligation: the customer who ignored a link on
    #: Tuesday may act on the same link on Friday. Maps to `send_payment_link`.
    #:
    #: Distinct from the two link-creation strategies on purpose. Creating a
    #: second link to chase the first is how a customer ends up able to pay twice.
    REMIND = "remind"

    #: Deliberately do nothing YET, and reconsider after a stated delay. Not the
    #: same as `DO_NOTHING`: this asserts the payment is recoverable but that now
    #: is the wrong moment -- an insufficient-funds failure two days before payday
    #: is the canonical case. Has no tool, because waiting is not an action.
    WAIT = "wait"

    #: Nothing is worth doing. A real and important answer: an agent that
    #: recommends chasing every failure is not exercising judgement.
    DO_NOTHING = "do_nothing"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class PaymentSnapshot(BaseModel):
    """The subset of a Razorpay payment the AI layer is shown.

    Deliberately narrow. The AI gets what it needs to reason about
    recoverability, and nothing that would let it act: no keys, no order tokens,
    no card details. `notes` IS included, because that is where a real prompt
    injection would arrive -- from a customer-supplied order note or a
    compromised upstream -- and the system's claim is that it survives one, not
    that it is never exposed to one.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    payment_id: str
    amount: Money
    method: str | None = None
    error_code: str | None = None
    error_description: str | None = None
    error_source: str | None = None
    error_step: str | None = None
    error_reason: str | None = None
    age_hours: int = 0
    has_saved_method: bool = False
    contact_present: bool = False
    #: Free-text merchant/customer notes. Untrusted by construction.
    notes: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_razorpay(
        cls,
        payment: dict[str, object],
        *,
        now_epoch: int,
        has_saved_method: bool = False,
    ) -> PaymentSnapshot:
        """Build from a raw Razorpay payment object."""
        raw_amount = payment.get("amount")
        amount = Money(
            minor_units=int(raw_amount) if isinstance(raw_amount, int) else 0,
            currency=str(payment.get("currency") or "INR"),
        )
        created = payment.get("created_at")
        age_hours = (
            max(0, (now_epoch - int(created)) // 3600) if isinstance(created, int) else 0
        )
        raw_notes = payment.get("notes")
        notes = (
            {str(k): str(v) for k, v in raw_notes.items()}
            if isinstance(raw_notes, dict)
            else {}
        )
        return cls(
            payment_id=str(payment.get("id", "")),
            amount=amount,
            method=_optional_str(payment.get("method")),
            error_code=_optional_str(payment.get("error_code")),
            error_description=_optional_str(payment.get("error_description")),
            error_source=_optional_str(payment.get("error_source")),
            error_step=_optional_str(payment.get("error_step")),
            error_reason=_optional_str(payment.get("error_reason")),
            age_hours=age_hours,
            has_saved_method=has_saved_method,
            contact_present=bool(payment.get("contact") or payment.get("email")),
            notes=notes,
        )


def _optional_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value else None


class RecoveryStep(BaseModel):
    """One later step in a recovery sequence, with when to take it.

    The timing is the point. `insufficient_funds` two days before payday and
    `insufficient_funds` the day after payday are the same error code and
    completely different decisions, and nothing in the payment record says which
    one you are looking at -- that judgement is the model's contribution.

    A REQUEST, NOT A SCHEDULE. Proposing a step does not reserve the right to take
    it. Every step is re-checked against the mandate, the caps and the velocity
    limits at the moment it would run, by `rekha.razorpay.sequence`. An approved
    plan is not a standing authorization to act four times.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: Strategy
    #: Hours to wait after the PREVIOUS step before taking this one. Relative
    #: rather than absolute so a plan stays valid if approval is delayed -- an
    #: absolute timestamp proposed on Monday and approved on Thursday would
    #: describe a moment already past.
    wait_hours: int = Field(default=0, ge=0, le=336)
    #: Why this step, at this delay. Shown to the merchant, never parsed.
    rationale: str = ""


class RecoveryProposal(BaseModel):
    """One proposed recovery action, plus what to do if it does not work.

    Strict (`extra="forbid"`): a model that invents a field is producing something
    nobody designed, and accepting it silently is how an unreviewed capability
    creeps in.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payment_id: str
    cause_class: CauseClass
    strategy: Strategy
    #: What to charge. Normally the original amount; a model may propose less
    #: (e.g. part payment on an insufficient-funds failure). The mandate and
    #: policy decide whether it is permitted -- this is a request, not a decision.
    amount: Money
    #: Estimated recoverable value, used only for ranking under a budget.
    expected_recovery: Money
    confidence: Confidence
    #: Plain-language diagnosis, shown to the merchant. Never parsed.
    diagnosis: str
    #: Why this strategy. Recorded in evidence so a human reviewing an approval
    #: sees the argument, not just the number.
    reasoning: str

    #: What to do if `strategy` does not produce a payment, in order.
    #:
    #: Empty means one shot and done, which was the whole system's behaviour
    #: before sequencing existed. `strategy` remains the FIRST action rather than
    #: becoming `follow_up[0]`, so everything that ranks, records or executes a
    #: single proposal keeps working unchanged and the sequence is purely additive.
    follow_up: tuple[RecoveryStep, ...] = ()

    #: Provenance of the decision. Not model-supplied -- filled in by the caller.
    prompt_version: str = "unknown"
    provider: str = "unknown"
    model: str = "unknown"

    def clamped(self, original: Money) -> RecoveryProposal:
        """Bound the model's numbers deterministically.

        `expected_recovery` can never exceed the original loss, and neither can
        be negative. A model asserting it will recover more than was lost is not
        optimistic, it is wrong, and the fix belongs in code rather than in a
        sterner prompt.
        """
        expected = self.expected_recovery
        if expected.currency != original.currency or expected > original:
            expected = original
        if expected.is_negative:
            expected = Money.zero(original.currency)
        amount = self.amount
        if amount.is_negative or amount.currency != original.currency:
            amount = original
        return self.model_copy(update={"expected_recovery": expected, "amount": amount})

    @property
    def is_actionable(self) -> bool:
        """Whether this proposal asks for anything to be done at all."""
        return self.strategy not in (Strategy.DO_NOTHING, Strategy.WAIT)

    @property
    def tool(self) -> str:
        """The Razorpay tool this strategy maps to.

        Raises for the strategies that have no tool by design (`DO_NOTHING`,
        `WAIT`) -- callers must check `is_actionable` rather than discovering it
        here.
        """
        if self.strategy is Strategy.UPI_LINK:
            return "create_payment_link_upi"
        if self.strategy is Strategy.PAYMENT_LINK:
            return "create_payment_link"
        if self.strategy is Strategy.REMIND:
            return "send_payment_link"
        raise ValueError(f"{self.strategy} maps to no tool; check `is_actionable` first")


class RecoveryPlan(BaseModel):
    """A prioritized batch of proposals, plus what was left out and why.

    `declined_for_budget` exists so the batch is reported honestly. An agent that
    silently drops the proposals it could not afford looks like it recovered
    everything worth recovering, which is a more flattering claim than the true
    one.
    """

    model_config = ConfigDict(extra="forbid")

    selected: list[RecoveryProposal] = Field(default_factory=list)
    declined_for_budget: list[RecoveryProposal] = Field(default_factory=list)
    not_worth_pursuing: list[RecoveryProposal] = Field(default_factory=list)
    currency: str = "INR"

    @property
    def total_expected_recovery(self) -> Money:
        from rekha.finance.money import total as sum_money

        return sum_money(
            [proposal.expected_recovery for proposal in self.selected], currency=self.currency
        )

    @property
    def total_requested(self) -> Money:
        from rekha.finance.money import total as sum_money

        return sum_money(
            [proposal.amount for proposal in self.selected], currency=self.currency
        )
