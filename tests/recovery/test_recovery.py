"""The AI recovery layer: `recovery/`.

What is worth testing about a layer whose output is non-deterministic is not the
model's judgement -- it is the *bounds* around that judgement. So these tests
concentrate on refusals and clamps: what happens when the model returns nonsense,
claims to recover more than was lost, answers about a payment nobody asked about,
or is simply unreachable.

The default suite never touches a network. `ReplayProvider` serves recorded
fixtures, so the real code path runs offline; a test that mocked out
`diagnose_batch` itself would only prove the mock works.
"""

from __future__ import annotations

from typing import Any

import pytest
from belay.finance.money import Money
from recovery.agent import derive_now_epoch
from recovery.diagnose import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_PROMPT,
    PROMPT_DIR,
    diagnose_batch,
    load_prompt,
)
from recovery.prioritize import prioritize
from recovery.proposal import (
    CauseClass,
    Confidence,
    PaymentSnapshot,
    RecoveryProposal,
    Strategy,
)
from recovery.providers import ProviderError

INR = "INR"


def _snapshot(payment_id: str = "pay_1", rupees: str = "1000.00", **kw: Any) -> PaymentSnapshot:
    base: dict[str, Any] = {
        "payment_id": payment_id,
        "amount": Money.from_major(rupees, INR),
        "method": "card",
        "error_code": "BAD_REQUEST_ERROR",
        "error_description": "Payment failed as 3D Secure could not be completed.",
        "error_step": "payment_authentication",
        "error_reason": "payment_failed",
        "age_hours": 4,
        "contact_present": True,
    }
    base.update(kw)
    return PaymentSnapshot(**base)


class _Canned:
    """A provider returning exactly what a test hands it. No network."""

    name = "canned"
    model = "canned"

    def __init__(self, *responses: dict[str, Any] | Exception) -> None:
        self._responses = list(responses)
        self.calls = 0

    def complete_json(self, *, system: str, user: str, schema: Any, max_tokens: int = 8192) -> Any:
        self.calls += 1
        response = self._responses[min(self.calls - 1, len(self._responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response


def _entry(payment_id: str = "pay_1", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "payment_id": payment_id,
        "cause_class": "customer_recoverable",
        "strategy": "upi_link",
        "amount_paise": 100000,
        "expected_recovery_paise": 55000,
        "confidence": "medium",
        "diagnosis": "The customer left before finishing.",
        "reasoning": "A UPI link avoids the failed card step.",
    }
    base.update(kw)
    return base


# ------------------------------------------------------------------- provenance


def test_the_prompt_is_versioned_by_content_hash() -> None:
    """Editing a prompt must change the recorded version, or provenance is a
    label rather than a fact."""
    text, version = load_prompt()
    # Asserted against DEFAULT_PROMPT rather than a hardcoded name: pinning "v1"
    # here meant that promoting v2 to the default failed a test about hashing,
    # which is not what this test is about.
    assert version.startswith(f"{DEFAULT_PROMPT}@")
    assert len(version.split("@")[1]) == 8
    assert "notes" in text and "instructions" in text  # the injection guidance


def test_every_shipped_prompt_carries_the_injection_defence() -> None:
    """A new prompt must not quietly drop it. The guidance is the only thing
    standing between a customer-supplied order note and the model treating it as
    an instruction, and it is easy to lose when rewriting."""
    for path in sorted((PROMPT_DIR).glob("*.md")):
        text = path.read_text(encoding="utf-8").lower()
        assert "notes" in text, f"{path.name} does not mention the notes field"
        assert "never as instructions" in text, f"{path.name} lacks injection guidance"
        assert "refund" in text, f"{path.name} does not state the refund boundary"


def test_every_proposal_carries_its_prompt_and_model() -> None:
    provider = _Canned({"proposals": [_entry()]})
    report = diagnose_batch([_snapshot()], provider)
    proposal = report.proposals[0]
    assert proposal.prompt_version == report.prompt_version
    assert proposal.provider == "canned"
    assert proposal.model == "canned"


# --------------------------------------------------------------------- clamping


def test_expected_recovery_is_clamped_to_the_original_amount() -> None:
    """A model claiming to recover more than was lost is wrong in a way no prompt
    reliably prevents, so the bound is in code."""
    provider = _Canned({"proposals": [_entry(expected_recovery_paise=99_999_999)]})
    report = diagnose_batch([_snapshot(rupees="1000.00")], provider)
    assert report.proposals[0].expected_recovery == Money.from_major("1000.00", INR)


def test_a_non_positive_request_is_rejected_not_patched() -> None:
    """A recovery for zero or a negative amount is not a recovery.

    Clamping to zero would produce a worthless payment link; clamping up to the
    original would be deciding on the model's behalf. Rejected, and the payment
    falls back to a conservative `do_nothing`.
    """
    for bad in (-1, 0):
        report = diagnose_batch(
            [_snapshot(rupees="1000.00")], _Canned({"proposals": [_entry(amount_paise=bad)]})
        )
        assert report.proposals[0].strategy is Strategy.DO_NOTHING
        assert any("must be positive" in why for _, why in report.rejected)


def test_a_negative_expected_recovery_is_rejected() -> None:
    report = diagnose_batch(
        [_snapshot()], _Canned({"proposals": [_entry(expected_recovery_paise=-500)]})
    )
    assert report.proposals[0].strategy is Strategy.DO_NOTHING
    assert any("must not be negative" in why for _, why in report.rejected)


def test_the_currency_comes_from_the_payment_not_the_model() -> None:
    """The model is given paise and never asked for a currency, so it cannot
    denominate a recovery in something the mandate does not authorize."""
    report = diagnose_batch([_snapshot()], _Canned({"proposals": [_entry()]}))
    assert report.proposals[0].amount.currency == INR


# -------------------------------------------------------------- malformed output


@pytest.mark.parametrize(
    "bad",
    [
        {"proposals": [{"payment_id": "pay_1"}]},                      # missing fields
        {"proposals": [_entry(cause_class="invented_cause")]},         # bad enum
        {"proposals": [_entry(strategy="wire_transfer")]},             # bad strategy
        {"proposals": [_entry(amount_paise="lots")]},                  # bad type
        {"proposals": ["not an object"]},                              # wrong shape
        {"proposals": "not a list"},                                   # wrong shape
        {},                                                            # no proposals
    ],
)
def test_malformed_output_never_becomes_a_proposal(bad: dict[str, Any]) -> None:
    """Rejected wholesale and replaced with a conservative `do_nothing` -- never
    patched into something half-valid."""
    report = diagnose_batch([_snapshot()], _Canned(bad))
    assert len(report.proposals) == 1
    assert report.proposals[0].strategy is Strategy.DO_NOTHING
    assert report.proposals[0].expected_recovery == Money.zero(INR)


def test_an_answer_about_an_unrequested_payment_is_dropped() -> None:
    """The model does not get to widen the batch it was given."""
    provider = _Canned({"proposals": [_entry(payment_id="pay_never_asked")]})
    report = diagnose_batch([_snapshot("pay_1")], provider)
    assert [p.payment_id for p in report.proposals] == ["pay_1"]
    assert report.proposals[0].strategy is Strategy.DO_NOTHING
    assert any("not in the request" in why for _, why in report.rejected)


def test_a_duplicate_answer_counts_once() -> None:
    """A model does not get two votes on one payment."""
    provider = _Canned({"proposals": [_entry(), _entry(expected_recovery_paise=1)]})
    report = diagnose_batch([_snapshot("pay_1")], provider)
    assert len(report.proposals) == 1
    assert report.proposals[0].expected_recovery == Money.from_major("550.00", INR)
    assert any("duplicate" in why for _, why in report.rejected)


# ------------------------------------------------------------------- fail closed


def test_a_provider_failure_proposes_nothing() -> None:
    """An unreachable model is not permission to act unadvised."""
    provider = _Canned(ProviderError("429 rate limited"))
    report = diagnose_batch([_snapshot("pay_1"), _snapshot("pay_2")], provider)
    assert len(report.proposals) == 2
    assert all(p.strategy is Strategy.DO_NOTHING for p in report.proposals)
    assert len(report.omitted) == 2
    assert "429" in report.proposals[0].reasoning


def test_every_input_payment_gets_exactly_one_proposal() -> None:
    """The batch metric the track bar asks for is only meaningful if the
    denominator is the whole batch."""
    snapshots = [_snapshot(f"pay_{i}") for i in range(10)]
    # The model answers for only three of them.
    provider = _Canned({"proposals": [_entry(f"pay_{i}") for i in range(3)]})
    report = diagnose_batch(snapshots, provider)
    assert len(report.proposals) == 10
    assert [p.payment_id for p in report.proposals] == [s.payment_id for s in snapshots]
    assert len(report.omitted) == 7


# ---------------------------------------------------------------------- batching


def test_batching_splits_the_cohort() -> None:
    """200 payments at 25 per call is 8 requests, which a free tier can serve.
    One call per payment would be 200 and would rate-limit."""
    snapshots = [_snapshot(f"pay_{i}") for i in range(60)]
    provider = _Canned({"proposals": []})
    report = diagnose_batch(snapshots, provider, batch_size=25)
    assert provider.calls == 3
    assert report.calls == 3
    assert len(report.proposals) == 60


def test_the_default_batch_size_keeps_a_full_cohort_within_a_free_tier() -> None:
    """The batch size is a real constraint discovered against real free tiers, not
    a round number.

    Measured: Gemini free tier completes 15 payments per request in ~24s and
    reliably drops the connection at 25 (25 proposals of prose is a lot of output
    tokens). Groq's free tier caps at 8,000 tokens/minute, which batch-10 already
    exceeds. So 10 is the reliable ceiling, giving 20 calls for a 200-payment
    cohort -- against Gemini's free limit of 20 requests, which is exactly why one
    call per payment (200 requests) was never viable.
    """
    assert DEFAULT_BATCH_SIZE <= 15, "larger batches drop the connection on a free tier"
    assert 200 / DEFAULT_BATCH_SIZE <= 20, "a full cohort must fit a free request quota"


# ------------------------------------------------------------- reproducible time


def test_the_age_anchor_comes_from_the_data_not_the_clock() -> None:
    """Anchoring to wall-clock would make every request unique, staling fixtures
    within the hour and making two runs over the same data disagree."""
    payments = [{"created_at": 1000}, {"created_at": 5000}, {"created_at": 3000}]
    assert derive_now_epoch(payments) == 5000 + 3600
    assert derive_now_epoch(payments) == derive_now_epoch(payments)


def test_the_anchor_falls_back_to_the_clock_only_when_there_is_no_data() -> None:
    import time

    assert abs(derive_now_epoch([]) - int(time.time())) < 5


# -------------------------------------------------------------- prioritisation


def _proposal(
    payment_id: str, ask: str, expected: str, strategy: Strategy = Strategy.UPI_LINK
) -> RecoveryProposal:
    return RecoveryProposal(
        payment_id=payment_id,
        cause_class=CauseClass.CUSTOMER_RECOVERABLE,
        strategy=strategy,
        amount=Money.from_major(ask, INR),
        expected_recovery=Money.from_major(expected, INR),
        confidence=Confidence.MEDIUM,
        diagnosis="d",
        reasoning="r",
    )


def test_selection_is_by_expected_value_and_is_deterministic() -> None:
    proposals = [
        _proposal("a", "1000.00", "300.00"),
        _proposal("b", "1000.00", "900.00"),
        _proposal("c", "1000.00", "600.00"),
    ]
    first = prioritize(proposals, remaining_budget=Money.from_major("2000.00", INR))
    second = prioritize(
        list(reversed(proposals)), remaining_budget=Money.from_major("2000.00", INR)
    )
    assert [p.payment_id for p in first.selected] == ["b", "c"]
    assert [p.payment_id for p in first.selected] == [p.payment_id for p in second.selected]
    assert [p.payment_id for p in first.declined_for_budget] == ["a"]


def test_a_proposal_too_large_to_afford_does_not_block_smaller_ones() -> None:
    """Skip, do not stop: greedy-by-value with a skip recovers more than halting
    at the first unaffordable item."""
    plan = prioritize(
        [
            _proposal("big", "5000.00", "4000.00"),
            _proposal("small", "500.00", "400.00"),
        ],
        remaining_budget=Money.from_major("1000.00", INR),
    )
    assert [p.payment_id for p in plan.selected] == ["small"]
    assert [p.payment_id for p in plan.declined_for_budget] == ["big"]


def test_do_nothing_is_reported_not_silently_dropped() -> None:
    plan = prioritize([_proposal("x", "100.00", "0.00", strategy=Strategy.DO_NOTHING)])
    assert plan.selected == []
    assert [p.payment_id for p in plan.not_worth_pursuing] == ["x"]


def test_a_proposal_over_the_per_action_ceiling_is_not_pursued() -> None:
    """Filtering here is an optimisation, never an authorization -- the mandate
    still refuses it independently. It avoids spending a request to be told no."""
    plan = prioritize(
        [_proposal("x", "9000.00", "5000.00")],
        max_per_action=Money.from_major("5000.00", INR),
    )
    assert plan.selected == []
    assert [p.payment_id for p in plan.not_worth_pursuing] == ["x"]


def test_max_actions_caps_the_batch() -> None:
    proposals = [_proposal(f"p{i}", "100.00", f"{100 - i}.00") for i in range(10)]
    plan = prioritize(proposals, max_actions=3)
    assert len(plan.selected) == 3
    assert len(plan.declined_for_budget) == 7


def test_a_budget_of_zero_selects_nothing_but_reports_everything() -> None:
    plan = prioritize(
        [_proposal("x", "100.00", "50.00")], remaining_budget=Money.zero(INR)
    )
    assert plan.selected == []
    assert len(plan.declined_for_budget) == 1


def test_plan_totals_are_exact() -> None:
    plan = prioritize(
        [_proposal("a", "1000.50", "600.25"), _proposal("b", "2000.25", "1000.75")],
        remaining_budget=Money.from_major("5000.00", INR),
    )
    assert plan.total_requested == Money.from_major("3000.75", INR)
    assert plan.total_expected_recovery == Money.from_major("1601.00", INR)
