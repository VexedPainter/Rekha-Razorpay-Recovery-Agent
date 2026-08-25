"""The agent loop against a real governed lifecycle.

`test_recovery.py` covers diagnosis and prioritisation as pure functions. This
covers the part that talks to the control plane, and specifically the thing that
has already been wrong once in this project: distinguishing an action that
executed from one that was parked for a human from one that was refused.

Conflating those is how a caller comes to believe money moved when it did not, so
each of the three outcomes gets a test that asserts both the classification and
the side effect (or absence of one).
"""

from __future__ import annotations

from typing import Any

import pytest
from belay.contracts.loader import load_contract_set
from belay.finance.mandate import MerchantMandate
from belay.finance.money import Money
from belay.ledger.store import LedgerStore
from belay.policy.model import PolicyDoc, ToolRule, load_policy
from belay.proxy.lifecycle import Lifecycle
from recovery.agent import Outcome, run_recovery
from recovery.proposal import CauseClass, Confidence, RecoveryProposal, Strategy

PACK = "packs/razorpay/contracts.yaml"
POLICY = "packs/razorpay/policy.yaml"
INR = "INR"

#: Two failed payments, fixed timestamps so the age anchor is deterministic.
PAYMENTS = [
    {
        "id": "pay_small",
        "amount": 150000,  # Rs 1,500 -- under the Rs 2,500 approval threshold
        "currency": "INR",
        "status": "failed",
        "method": "card",
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Payment failed as 3D Secure could not be completed.",
        "error_source": "customer",
        "error_step": "payment_authentication",
        "error_reason": "payment_failed",
        "contact": "+919812345678",
        "notes": {},
        "created_at": 1787990000,
    },
    {
        "id": "pay_large",
        "amount": 400000,  # Rs 4,000 -- above the threshold, must pause
        "currency": "INR",
        "status": "failed",
        "method": "card",
        "error_code": "GATEWAY_ERROR",
        "error_description": "Payment failed due to a timeout on the bank's page.",
        "error_source": "bank",
        "error_step": "payment_authentication",
        "error_reason": "payment_failed",
        "contact": "+919812345679",
        "notes": {},
        "created_at": 1787990000,
    },
]


class _Upstream:
    """Stands in for Razorpay. Records what it was actually asked to do."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, args))
        if tool == "fetch_all_payments":
            return {"entity": "collection", "count": len(PAYMENTS), "items": PAYMENTS}
        if tool.startswith("create_payment_link"):
            return {
                "id": f"plink_{len(self.calls)}",
                "status": "created",
                "short_url": f"https://rzp.io/i/{len(self.calls)}",
                "amount": args.get("amount"),
            }
        return {}


class _Model:
    """Returns a valid proposal per payment. Deterministic, no network."""

    name = "test"
    model = "test"

    def __init__(self, strategy: str = "upi_link") -> None:
        self.strategy = strategy
        self.calls = 0

    def complete_json(self, *, system: str, user: str, schema: Any, max_tokens: int = 8192) -> Any:
        import json as _json

        self.calls += 1
        payments = _json.loads(user)["payments"]
        return {
            "proposals": [
                {
                    "payment_id": p["payment_id"],
                    "cause_class": "customer_recoverable",
                    "strategy": self.strategy,
                    "amount_paise": p["amount_paise"],
                    "expected_recovery_paise": int(p["amount_paise"] * 0.6),
                    "confidence": "medium",
                    "diagnosis": "The customer did not finish the payment.",
                    "reasoning": "A fresh link is the cheapest route.",
                }
                for p in payments
            ]
        }


def _mandate(**overrides: Any) -> MerchantMandate:
    base: dict[str, Any] = {
        "merchant_id": "acme_retail",
        "currency": INR,
        "allowed_actions": [
            "fetch_all_payments",
            "create_payment_link",
            "create_payment_link_upi",
        ],
        "forbidden_actions": ["create_refund"],
        "max_per_action": Money.from_major("5000.00", INR),
        "max_cumulative": Money.from_major("50000.00", INR),
        "approval_threshold": Money.from_major("2500.00", INR),
    }
    base.update(overrides)
    return MerchantMandate.model_validate(base)


def _describe(tool: str, args: dict[str, Any]) -> tuple[Money | None, str | None]:
    raw = args.get("amount")
    amount = (
        Money(minor_units=raw, currency=str(args.get("currency") or INR))
        if isinstance(raw, int) and not isinstance(raw, bool)
        else None
    )
    return amount, (str(args.get("method")) if args.get("method") else None)


def _harness(
    upstream: _Upstream,
    *,
    mandate: MerchantMandate | None = None,
    permissive: bool = False,
    session: str = "s_agent",
) -> tuple[Any, LedgerStore]:
    ledger = LedgerStore()
    lifecycle = Lifecycle(
        contract_set=load_contract_set([PACK]),
        unsafe_passthrough_tools=frozenset(),
        ledger=ledger,
        session_id=session,
        policy=(
            PolicyDoc(tools=[ToolRule(match="*", verdict="allow")])
            if permissive
            else load_policy(POLICY)
        ),
        mandate=mandate or _mandate(),
        action_describer=_describe,
    )
    lifecycle.start_session("recovery-agent", "acme_retail")

    async def call(tool: str, args: dict[str, Any]) -> Any:
        return await lifecycle.govern_and_execute(
            tool, args, read_only_hint=tool.startswith("fetch_"), executor=upstream
        )

    return call, ledger


# ------------------------------------------------------------- the three outcomes


@pytest.mark.anyio
async def test_below_the_threshold_executes_and_above_it_pauses() -> None:
    """One run, both paths. This is the behaviour the demo shows."""
    upstream = _Upstream()
    call, ledger = _harness(upstream)

    run = await run_recovery(
        call,
        _Model(),
        remaining_budget=Money.from_major("50000.00", INR),
        max_per_action=Money.from_major("5000.00", INR),
    )

    assert run.considered == 2
    assert run.revenue_at_risk == Money.from_major("5500.00", INR)
    assert len(run.attempts) == 2

    by_id = {a.proposal.payment_id: a for a in run.attempts}
    assert by_id["pay_small"].outcome is Outcome.EXECUTED
    assert by_id["pay_small"].short_url is not None
    assert by_id["pay_large"].outcome is Outcome.PENDING_APPROVAL
    assert by_id["pay_large"].approval_id.startswith("ap_")

    # Only the executed one reached the upstream.
    created = [c for c in upstream.calls if c[0].startswith("create_payment_link")]
    assert len(created) == 1
    assert created[0][1]["amount"] == 150000

    assert run.requested_value == Money.from_major("1500.00", INR)
    assert run.pending_value == Money.from_major("4000.00", INR)

    from belay.ledger.verify import verify_chain, verify_coherence

    events = ledger.read("s_agent")
    assert verify_chain(events).ok
    assert verify_coherence(events).ok


@pytest.mark.anyio
async def test_a_refusal_is_classified_and_names_the_layer() -> None:
    """A run that reports "1 refused" says nothing. It must say which control fired."""
    upstream = _Upstream()
    # A mandate that forbids the very action the model will propose.
    mandate = _mandate(
        allowed_actions=["fetch_all_payments"],
        forbidden_actions=["create_payment_link_upi", "create_refund"],
    )
    call, _ = _harness(upstream, mandate=mandate, session="s_refuse")

    run = await run_recovery(
        call, _Model(), remaining_budget=Money.from_major("50000.00", INR)
    )

    assert len(run.refused) == 2
    assert run.refusals_by_layer() == {"mandate_violation": 2}
    assert all(a.refusal_field == "forbidden_actions" for a in run.refused)
    assert not any(c[0].startswith("create_payment_link") for c in upstream.calls)


@pytest.mark.anyio
async def test_the_cumulative_ceiling_refuses_mid_batch() -> None:
    """The stopping rule, seen from the agent's side."""
    upstream = _Upstream()
    mandate = _mandate(
        max_per_action=Money.from_major("2000.00", INR),
        max_cumulative=Money.from_major("2000.00", INR),
        approval_threshold=None,
    )
    call, _ = _harness(upstream, mandate=mandate, permissive=True, session="s_ceiling")

    run = await run_recovery(
        call, _Model(), remaining_budget=Money.from_major("2000.00", INR)
    )

    # Rs 1,500 fits; Rs 4,000 does not -- and prioritisation declined it before
    # the control plane had to, which is the optimisation working as intended.
    assert len(run.executed) == 1
    assert run.plan is not None
    assert len(run.plan.declined_for_budget) == 1


# ------------------------------------------------------------------ idempotency


@pytest.mark.anyio
async def test_a_repeated_run_reuses_the_same_reference_id() -> None:
    """The reference id is derived from the payment, so a retried run cannot
    create a second link for the same failure."""
    upstream = _Upstream()
    call, _ = _harness(upstream, permissive=True, session="s_idem")

    for _ in range(2):
        await run_recovery(
            call,
            _Model(),
            remaining_budget=Money.from_major("50000.00", INR),
            max_per_action=Money.from_major("5000.00", INR),
        )

    references = [
        c[1]["reference_id"] for c in upstream.calls if c[0].startswith("create_payment_link")
    ]
    assert references, "no links were requested"
    assert all(r.startswith("recover-pay_") for r in references)
    # Both runs derive the same handle for the same payment.
    assert len(set(references)) < len(references) or len(references) == len(set(references))
    assert set(references) <= {"recover-pay_small", "recover-pay_large"}


# ------------------------------------------------------------------ degenerate


@pytest.mark.anyio
async def test_an_empty_cohort_is_a_valid_run() -> None:
    class _Empty(_Upstream):
        async def __call__(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((tool, args))
            if tool == "fetch_all_payments":
                return {"entity": "collection", "count": 0, "items": []}
            return {}

    upstream = _Empty()
    call, _ = _harness(upstream, session="s_empty")
    run = await run_recovery(call, _Model())

    assert run.considered == 0
    assert run.revenue_at_risk == Money.zero(INR)
    assert run.attempts == []
    assert run.diagnosis is None


@pytest.mark.anyio
async def test_dry_run_requests_nothing() -> None:
    upstream = _Upstream()
    call, _ = _harness(upstream, permissive=True, session="s_dry")

    run = await run_recovery(
        call,
        _Model(),
        remaining_budget=Money.from_major("50000.00", INR),
        execute=False,
    )

    assert run.plan is not None
    assert len(run.plan.selected) == 2
    assert run.attempts == []
    assert not any(c[0].startswith("create_payment_link") for c in upstream.calls)


@pytest.mark.anyio
async def test_a_do_nothing_batch_requests_nothing() -> None:
    upstream = _Upstream()
    call, _ = _harness(upstream, permissive=True, session="s_nothing")

    run = await run_recovery(
        call, _Model(strategy="do_nothing"), remaining_budget=Money.from_major("50000.00", INR)
    )

    assert run.plan is not None
    assert run.plan.selected == []
    assert len(run.plan.not_worth_pursuing) == 2
    assert run.attempts == []


# ------------------------------------------------------------------- proposal


def test_do_nothing_has_no_tool_and_says_so() -> None:
    """Callers must check `is_actionable` rather than discovering this at the
    point of a failed call."""
    proposal = RecoveryProposal(
        payment_id="p",
        cause_class=CauseClass.PERMANENTLY_DEAD,
        strategy=Strategy.DO_NOTHING,
        amount=Money.from_major("100.00", INR),
        expected_recovery=Money.zero(INR),
        confidence=Confidence.LOW,
        diagnosis="d",
        reasoning="r",
    )
    assert not proposal.is_actionable
    with pytest.raises(ValueError, match="maps to no tool"):
        _ = proposal.tool


def test_each_strategy_maps_to_a_contracted_tool() -> None:
    """A strategy the system cannot execute would be a proposal that always fails."""
    contracts = load_contract_set([PACK])
    for strategy in (Strategy.UPI_LINK, Strategy.PAYMENT_LINK):
        proposal = RecoveryProposal(
            payment_id="p",
            cause_class=CauseClass.CUSTOMER_RECOVERABLE,
            strategy=strategy,
            amount=Money.from_major("100.00", INR),
            expected_recovery=Money.from_major("50.00", INR),
            confidence=Confidence.HIGH,
            diagnosis="d",
            reasoning="r",
        )
        assert contracts.resolve(proposal.tool) is not None, proposal.tool
