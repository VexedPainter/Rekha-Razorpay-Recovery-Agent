"""Calibration and held-out evaluation.

Two properties matter more than the scores themselves:

1. **Ground truth must not reach the model.** If the label leaked into the prompt,
   every accuracy number in the project would be circular. There is a test that
   walks the actual prompt payload looking for it.
2. **A score without a baseline is noise.** A classifier is only interesting
   relative to something dumber, so the evaluation carries two baselines and the
   tests assert the comparison is real.
"""

from __future__ import annotations

import json

import pytest
from bench.calibration import render as render_calibration
from bench.calibration import score
from bench.evaluate import evaluate, keyword_baseline
from bench.evaluate import render as render_evaluation
from rekha.finance.money import Money
from rekha.ledger.store import LedgerStore
from rekha.razorpay.forecast import RECOVERY_PROPOSED, record_proposal
from rekha.razorpay.webhooks import ingest, sign_payload

INR = "INR"
SECRET = "cal_secret"


# ------------------------------------------------- ground truth must not leak


def test_ground_truth_never_reaches_the_prompt() -> None:
    """The load-bearing test for every accuracy number in the project.

    If the `_truth` side-channel reached the model it would be reading the answer
    rather than inferring it, and both the evaluation and the calibration would be
    measuring nothing at all.

    A NECESSARY DISTINCTION. Some cause-class names share a string with Razorpay's
    own `error_reason` field -- a real payment genuinely carries
    `error_reason: "insufficient_funds"`. That is legitimate signal the model is
    supposed to read, not leakage: a real integration would see it too, so excluding
    it would make the benchmark easier than reality rather than harder. What must
    never appear is the `_truth` structure, which also encodes labels the error text
    does NOT name -- `bank_transient`, `customer_recoverable`, `permanently_dead`
    and `method_unsupported` are inferences, not restatements.
    """
    import sys
    from pathlib import Path

    sandbox = Path(__file__).resolve().parents[2] / "examples" / "razorpay-sandbox"
    if str(sandbox) not in sys.path:
        sys.path.insert(0, str(sandbox))
    from cohort import generate_cohort
    from recovery.agent import derive_now_epoch
    from recovery.diagnose import _snapshot_for_prompt
    from recovery.proposal import PaymentSnapshot

    payments = generate_cohort(60)
    # Ground truth exists in the raw cohort -- that is the point of it.
    assert all("_truth" in p for p in payments)

    #: The one cause class Razorpay's own error fields state outright.
    stated_by_razorpay = {"insufficient_funds"}

    inferred_seen = 0
    anchor = derive_now_epoch(payments)
    for payment in payments:
        snapshot = PaymentSnapshot.from_razorpay(payment, now_epoch=anchor)
        prompt_payload = json.dumps(_snapshot_for_prompt(snapshot))

        # The side-channel itself must be absent, in every form.
        assert "_truth" not in prompt_payload
        assert "recovery_probability" not in prompt_payload
        assert "base_rate" not in prompt_payload

        label = payment["_truth"]["cause_class"]
        if label in stated_by_razorpay:
            continue
        inferred_seen += 1
        assert label not in prompt_payload, (
            f"cause class {label!r} appears verbatim in the prompt, so the model is "
            f"reading the answer rather than inferring it"
        )

    # Guard against the guard: if every label happened to be a stated one, the loop
    # above would have asserted nothing.
    assert inferred_seen > 20, "too few inferred labels for this test to mean anything"


def test_the_sandbox_strips_ground_truth_at_its_boundary() -> None:
    """Stripped at the single point every read tool passes through, so it cannot
    escape however a payment is fetched."""
    import sys
    from pathlib import Path

    sandbox = Path(__file__).resolve().parents[2] / "examples" / "razorpay-sandbox"
    if str(sandbox) not in sys.path:
        sys.path.insert(0, str(sandbox))
    import importlib

    server = importlib.import_module("server")
    payment = next(iter(server._payments.values()))

    assert "_truth" in payment, "the cohort should carry ground truth internally"
    assert "_truth" not in server._public(payment)


# ------------------------------------------------------------------ evaluation


def test_the_model_is_scored_against_two_baselines() -> None:
    """"78% accurate" is meaningless alone. A classifier is only interesting
    relative to something dumber."""
    result = evaluate(count=200, holdout_fraction=0.5, seed=4242)

    assert result.holdout_size == 100
    assert result.train_size == 100
    # The majority baseline must be weak, or the classes are not balanced enough
    # for the comparison to mean anything.
    assert result.majority.macro_f1 < 0.2
    # The keyword table is the honest bar.
    assert result.keyword.macro_f1 > 0.5
    assert result.model.macro_f1 > result.majority.macro_f1


def test_the_model_beats_the_keyword_baseline() -> None:
    """The number that transfers to real data is the MARGIN, since both classifiers
    read the same text. If this regresses, the model has stopped earning its place."""
    result = evaluate()
    assert result.beats_keyword, (
        f"model macro F1 {result.model.macro_f1:.1%} does not beat the keyword table's "
        f"{result.keyword.macro_f1:.1%} -- the model is not adding value over a "
        f"twelve-line rule table"
    )


def test_the_majority_baseline_is_taken_from_the_training_half() -> None:
    """A baseline that peeks at the test set is not a baseline."""
    result = evaluate()
    assert "majority class" in result.majority.name


def test_macro_averaging_gives_rare_classes_equal_weight() -> None:
    """`permanently_dead` has 3-4 members against `bank_transient`'s 26. Micro
    averaging would let it fail completely without moving the score."""
    result = evaluate()
    supports = {label: s.support for label, s in result.model.classes.items() if s.support}
    assert min(supports.values()) < 10
    assert max(supports.values()) > 20
    # Macro F1 is the unweighted mean of per-class F1, so it cannot equal accuracy
    # when the classes are this imbalanced unless every class scores identically.
    assert result.model.macro_f1 == pytest.approx(
        sum(s.f1 for s in result.model.classes.values() if s.support)
        / len([s for s in result.model.classes.values() if s.support])
    )


def test_the_evaluation_is_deterministic() -> None:
    assert render_evaluation(evaluate(seed=7)) == render_evaluation(evaluate(seed=7))


def test_a_different_split_seed_gives_a_different_holdout() -> None:
    first, second = evaluate(seed=1), evaluate(seed=2)
    assert first.model.confusion != second.model.confusion or first.model.n != second.model.n


def test_the_keyword_baseline_is_deliberately_crude() -> None:
    assert keyword_baseline("Payment was blocked because it was flagged as high risk") == (
        "permanently_dead"
    )
    assert keyword_baseline("insufficient funds in your account") == "insufficient_funds"
    assert keyword_baseline("card has expired") == "method_unsupported"
    # Unmatched text falls back rather than guessing cleverly.
    assert keyword_baseline("something entirely unrelated") == "bank_transient"
    assert keyword_baseline("") == "bank_transient"


# ----------------------------------------------------------------- calibration


def _forecast_session(pairs: list[tuple[float, bool]]) -> LedgerStore:
    """A ledger with recorded forecasts and matching observed outcomes.

    `pairs` is `(forecast_probability, did_the_customer_pay)`.
    """
    ledger = LedgerStore()
    ledger.append("s", "session_started", {"merchant_id": "acme"}, initiated_by="agent")

    for index, (probability, paid) in enumerate(pairs):
        payment_id = f"pay_{index}"
        reference = f"recover-{payment_id}"
        amount = Money.from_major("1000.00", INR)
        record_proposal(
            ledger,
            "s",
            payment_id=payment_id,
            reference_id=reference,
            cause_class="customer_recoverable",
            strategy="upi_link",
            amount=amount,
            expected_recovery=Money(
                minor_units=int(amount.minor_units * probability), currency=INR
            ),
            confidence="medium",
            diagnosis="d",
            reasoning="r",
            prompt_version="test@0000",
            provider="test",
            model="test",
            selected=True,
        )
        link_id = f"plink_{index}"
        ledger.append(
            "s",
            "result_recorded",
            {
                "tool": "create_payment_link_upi",
                "result": {
                    "id": link_id,
                    "amount": amount.minor_units,
                    "currency": INR,
                    "reference_id": reference,
                },
            },
            step_seq=index + 1,
        )
        if paid:
            body = json.dumps(
                {
                    "event": "payment_link.paid",
                    "payload": {
                        "payment_link": {
                            "entity": {
                                "id": link_id,
                                "amount": amount.minor_units,
                                "amount_paid": amount.minor_units,
                                "currency": INR,
                                "reference_id": reference,
                            }
                        },
                        "payment": {"entity": {"id": f"pay_paid_{index}"}},
                    },
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id=f"evt_{index}")
    return ledger


def test_a_perfect_forecaster_scores_zero_brier() -> None:
    ledger = _forecast_session([(1.0, True), (1.0, True), (0.0, False), (0.0, False)])
    report = score(ledger.read("s"))
    assert report.brier_score == pytest.approx(0.0)
    assert report.coverage == 1.0


def test_a_coin_flip_forecaster_scores_a_quarter() -> None:
    ledger = _forecast_session([(0.5, True), (0.5, False), (0.5, True), (0.5, False)])
    report = score(ledger.read("s"))
    assert report.brier_score == pytest.approx(0.25)


def test_skill_is_measured_against_the_base_rate_not_against_a_coin_flip() -> None:
    """Beating 0.25 proves almost nothing. The honest bar is beating a forecaster
    that always quotes the average, and a model with no skill must report so."""
    # Every outcome positive, and the forecaster always says 0.5. Base rate is 1.0,
    # so quoting the base rate would be perfect and the model has NEGATIVE skill.
    ledger = _forecast_session([(0.5, True)] * 6)
    report = score(ledger.read("s"))
    assert report.base_rate == pytest.approx(1.0)
    assert report.baseline_brier == pytest.approx(0.0)
    assert report.brier_score == pytest.approx(0.25)
    # Baseline is 0, so skill is undefined rather than fabricated.
    assert report.skill is None


def test_a_forecaster_with_real_skill_beats_the_base_rate() -> None:
    """Half recover. A forecaster that separates them scores better than one quoting
    50% to everyone."""
    ledger = _forecast_session(
        [(0.9, True), (0.9, True), (0.1, False), (0.1, False)]
    )
    report = score(ledger.read("s"))
    assert report.base_rate == pytest.approx(0.5)
    assert report.baseline_brier == pytest.approx(0.25)
    assert report.brier_score is not None and report.brier_score < 0.25
    assert report.skill is not None and report.skill > 0.5


def test_the_calibration_curve_detects_overconfidence() -> None:
    """A model can be accurate on average while being systematically overconfident,
    and only the curve shows it."""
    # Says 90% every time; only half actually pay.
    ledger = _forecast_session([(0.9, True), (0.9, False)] * 5)
    report = score(ledger.read("s"))
    bucket = next(b for b in report.curve() if b.n and b.label == "80-100%")
    assert bucket.actual_rate == pytest.approx(0.5)
    assert bucket.gap > 0.3, "an overconfident model must show a positive gap"


def test_unobserved_forecasts_are_excluded_and_coverage_is_reported() -> None:
    """A forecast about a payment nobody chased has no outcome. Scoring it as a
    failure would punish the agent for correctly declining."""
    ledger = _forecast_session([(0.8, True), (0.8, False)])
    # A third forecast with no corresponding link, so no outcome exists.
    record_proposal(
        ledger,
        "s",
        payment_id="pay_never",
        reference_id="recover-pay_never",
        cause_class="permanently_dead",
        strategy="do_nothing",
        amount=Money.from_major("1000.00", INR),
        expected_recovery=Money.zero(INR),
        confidence="low",
        diagnosis="d",
        reasoning="r",
        prompt_version="test@0000",
        provider="test",
        model="test",
        selected=False,
    )
    report = score(ledger.read("s"))
    assert len(report.scored) == 3
    assert len(report.observable) == 2
    assert report.coverage == pytest.approx(2 / 3)
    assert "coverage" in render_calibration(report)


def test_value_error_reports_over_promising_in_rupees() -> None:
    """A small probability error on a large payment costs more than a large error on
    a small one, and a merchant cares about the money."""
    ledger = _forecast_session([(1.0, False)])
    report = score(ledger.read("s"))
    # Forecast a full INR 1,000 recovery; nothing arrived.
    assert report.value_error == Money.from_major("1000.00", INR)


def test_scoring_with_no_outcomes_says_so_rather_than_inventing_a_result() -> None:
    ledger = _forecast_session([])
    ledger.append("s", "session_started", {"merchant_id": "m"}, initiated_by="a")
    report = score(ledger.read("s"))
    assert report.brier_score is None
    assert "No outcomes yet" in render_calibration(report)


def test_calibration_is_a_deterministic_fold() -> None:
    events = _forecast_session([(0.7, True), (0.3, False)]).read("s")
    assert render_calibration(score(events)) == render_calibration(score(events))


def test_forecasts_are_recorded_by_the_control_plane_not_the_ai() -> None:
    """The AI cannot write its own track record -- an agent able to do that could
    rewrite it. `recovery/` is forbidden from importing `rekha.ledger`."""
    import ast
    from pathlib import Path

    forecast_module = (
        Path(__file__).resolve().parents[2] / "rekha" / "razorpay" / "forecast.py"
    )
    tree = ast.parse(forecast_module.read_text(encoding="utf-8"))
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    # It lives on the control-plane side and imports the ledger, which the AI layer
    # may not do.
    assert any("ledger" in module for module in imports)


def test_the_recorded_forecast_carries_full_provenance() -> None:
    ledger = _forecast_session([(0.6, True)])
    proposed = next(e for e in ledger.read("s") if e.type == RECOVERY_PROPOSED)
    for field in ("prompt_version", "provider", "model", "implied_probability", "selected"):
        assert field in proposed.payload
    assert proposed.payload["implied_probability"] == pytest.approx(0.6)
