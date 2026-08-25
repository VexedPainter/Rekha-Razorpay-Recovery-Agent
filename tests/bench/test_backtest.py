"""The backtest: what does sequencing actually earn?

The properties worth pinning are about honesty rather than arithmetic:

1. **Outcomes come from ground truth, never from the model's own forecast.** The
   latter would make every calibration score perfect by construction.
2. **The arms are paired.** Same payments, same seed, so the difference is caused
   by sequencing rather than by one arm drawing an easier cohort.
3. **The fatigue assumption is reported, not buried.** It drives the result more
   than anything else in the comparison.
"""

from __future__ import annotations

import pytest
from belay.razorpay.sequence import ContactPolicy
from bench.backtest import (
    _attempt_succeeds,
    _brier,
    _discrimination,
    render,
    render_sweep,
    run,
)


def test_the_backtest_produces_a_sample_worth_reading() -> None:
    """The whole reason this exists: calibration on n=5 is noise. A conclusion needs
    a denominator."""
    result = run(count=200)
    assert len(result.sequenced.observations) >= 100, (
        "too few observed outcomes for any calibration claim to mean anything"
    )


def test_sequencing_recovers_more_than_a_single_attempt() -> None:
    result = run(count=200)
    assert result.sequenced.recovered > result.single.recovered
    assert result.uplift.minor_units > 0


def test_the_arms_are_paired_so_the_difference_is_attributable() -> None:
    """Independent samples would let one arm draw an easier cohort and the gap would
    measure luck. Both arms see the same payments and the same draws for the first
    attempt, so single-shot can never beat sequenced on the same seed."""
    result = run(count=200)
    assert result.single.at_risk == result.sequenced.at_risk
    # Sequenced takes a superset of single-shot's actions, so it cannot do worse.
    assert result.sequenced.payments_recovered >= result.single.payments_recovered


def test_more_contacts_is_not_automatically_better() -> None:
    """The efficiency column exists so the uplift cannot be read as free. Sequencing
    buys its extra recovery with extra contacts, and both are reported."""
    result = run(count=200)
    assert result.sequenced.contacts > result.single.contacts
    assert result.single.rupees_per_contact > 0
    assert "INR/contact" in render(result)
    assert "goodwill" in render(result)


def test_the_fatigue_assumption_changes_the_answer_and_is_swept() -> None:
    """If the conclusion holds only at one flattering value, that must be visible."""
    weak = run(count=200, fatigue=0.3)
    strong = run(count=200, fatigue=1.0)
    assert strong.uplift > weak.uplift
    # Single-shot is unaffected by fatigue: it only ever makes one contact.
    assert weak.single.recovered == strong.single.recovered

    sweep = render_sweep([weak, strong])
    assert "ASSUMPTION" in render(weak) or "assumption" in render(weak)
    assert "0.30" in sweep and "1.00" in sweep


def test_no_fatigue_means_a_later_contact_is_as_good_as_the_first() -> None:
    import random

    rng = random.Random(1)
    assert _attempt_succeeds(rng, 1.0, 5, fatigue=1.0) is True
    # With harsh fatigue a fifth contact on a certain payment still fails.
    assert _attempt_succeeds(random.Random(1), 1.0, 5, fatigue=0.1) is False


def test_outcomes_are_drawn_from_ground_truth_not_from_the_forecast() -> None:
    """THE LOAD-BEARING PROPERTY. If outcomes came from the model's own numbers, a
    confident model would appear perfectly calibrated no matter how wrong it was.

    Demonstrated by the numbers disagreeing: the model's mean forecast and the
    realised rate differ substantially, which is only possible if they have
    independent sources.
    """
    result = run(count=200)
    obs = result.sequenced.observations
    mean_forecast = sum(p for p, _ in obs) / len(obs)
    actual_rate = sum(1 for _, hit in obs if hit) / len(obs)
    assert abs(mean_forecast - actual_rate) > 0.05, (
        "forecast and outcome track each other suspiciously closely -- check that "
        "outcomes are not being drawn from the model's own prediction"
    )


def test_the_contact_policy_bounds_the_simulation_too() -> None:
    """A backtest that ignored the contact limit would report an uplift the real
    system could never deliver."""
    capped = run(count=200, policy=ContactPolicy(max_touches=1))
    assert capped.sequenced.contacts == capped.single.contacts
    assert capped.uplift.minor_units == 0, (
        "with a one-contact limit, sequencing cannot add anything"
    )


def test_the_backtest_is_deterministic() -> None:
    assert render(run(count=100, seed=7)) == render(run(count=100, seed=7))


def test_a_different_seed_changes_the_outcome_but_not_the_cohort() -> None:
    first, second = run(count=200, seed=1), run(count=200, seed=2)
    assert first.single.at_risk == second.single.at_risk
    assert first.sequenced.recovered != second.sequenced.recovered


# ------------------------------------------------- calibration vs discrimination


def test_discrimination_and_calibration_are_different_questions() -> None:
    """A forecaster uniformly 30 points too pessimistic ranks perfectly and scores
    badly on Brier. Conflating the two would mean reporting a useful model as
    useless, or the reverse."""
    # Perfect ranking, terrible levels.
    observations = [(0.1, True)] * 50 + [(0.05, False)] * 50
    _, _, separation = _discrimination(observations)
    brier, baseline, skill = _brier(observations)
    assert separation > 0, "ranking is perfect here"
    assert skill is not None and skill < 0, "levels are bad enough to lose to the base rate"


def test_a_forecaster_with_no_ranking_signal_shows_zero_separation() -> None:
    observations = [(0.4, True), (0.4, False)] * 25
    _, _, separation = _discrimination(observations)
    assert separation == pytest.approx(0.0)


def test_discrimination_needs_both_outcomes_to_be_defined() -> None:
    """With no failures there is nothing to separate from, and inventing a number
    would be worse than returning zero."""
    assert _discrimination([(0.9, True), (0.8, True)]) == (0.0, 0.0, 0.0)
    assert _discrimination([]) == (0.0, 0.0, 0.0)


def test_the_report_explains_a_negative_skill_rather_than_hiding_it() -> None:
    """An unflattering number with no explanation invites the wrong conclusion; an
    unflattering number that is left out invites a worse one."""
    result = run(count=200)
    text = render(result)
    brier, baseline, skill = _brier(result.sequenced.observations)
    assert "Discrimination" in text and "Calibration" in text
    if skill is not None and skill < 0:
        assert "READ THESE TWO TOGETHER" in text
        assert "pessimistic" in text


def test_brier_is_undefined_rather_than_fabricated_on_a_degenerate_baseline() -> None:
    _, _, skill = _brier([(0.5, True)] * 10)
    assert skill is None


def test_an_empty_run_does_not_divide_by_zero() -> None:
    result = run(count=0)
    assert result.sequenced.recovered.minor_units == 0
    assert "BACKTEST" in render(result)
