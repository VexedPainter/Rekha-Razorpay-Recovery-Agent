"""Scoring the AI's forecasts against what actually happened.

The agent does not merely classify failures. It makes a *probabilistic forecast*:
"this failure has roughly a 70% chance of recovering, so its expected value is
INR 1,968." Webhooks later reveal whether the customer paid.

This module compares those two, which is the difference between an agent that
sounds confident and an agent that is measurably calibrated. Without it, an agent
that says "80%" about everything looks identical to one that has learned something.

Three measurements, each answering a different question:

**Brier score** -- accuracy of the probabilities themselves. The mean squared error
between forecast and outcome, lower is better. Two reference points matter:
`always 0.5` scores 0.25, and a forecaster that always predicts the base rate scores
the base rate's variance. Beating the second one is the bar; beating only the first
is nearly meaningless.

**Calibration curve** -- honesty of the probabilities. Of the forecasts where the
agent said "around 70%", did about 70% actually recover? A model can be accurate on
average while being systematically overconfident, and only the curve shows that.

**Per-cause recovery rate** -- measured reality per failure class, which is the
feedback the prompt can actually consume. If `insufficient_funds` recovers at 20%
and the agent keeps forecasting 40%, that is a correctable error rather than a
mystery.

## The selection bias, stated up front

Outcomes are only observable for recoveries that were actually attempted. A forecast
about a payment nobody chased has no outcome, so calibration is necessarily computed
over the attempted subset -- and the agent chose that subset precisely because it was
confident. So these numbers describe calibration *on the cases the agent selected*,
not on the cohort as a whole, and they will look better than a random sample would.

That is unavoidable without deliberately chasing payments the agent judged hopeless
(which would cost the merchant real money to measure). It is reported rather than
hidden: `coverage` says what fraction of forecasts could be scored at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from belay.finance.money import Money
from belay.finance.money import total as sum_money
from belay.ledger.model import Event
from belay.razorpay.forecast import RECOVERY_PROPOSED
from belay.razorpay.webhooks import correlate_recoveries

#: Forecast buckets for the calibration curve. Ten would be finer but leaves too
#: few samples per bucket in a 200-payment cohort to say anything -- a bucket with
#: two observations is noise wearing a percentage sign.
BUCKETS: tuple[tuple[float, float, str], ...] = (
    (0.00, 0.20, "0-20%"),
    (0.20, 0.40, "20-40%"),
    (0.40, 0.60, "40-60%"),
    (0.60, 0.80, "60-80%"),
    (0.80, 1.01, "80-100%"),
)


@dataclass(frozen=True)
class ScoredForecast:
    """One forecast, joined to its observed outcome."""

    payment_id: str
    reference_id: str
    cause_class: str
    strategy: str
    confidence: str
    forecast: float
    #: 1.0 if the customer paid, 0.0 if they did not. `None` when unobservable.
    outcome: float | None
    authorized: Money
    recovered: Money

    @property
    def observed(self) -> bool:
        return self.outcome is not None

    @property
    def squared_error(self) -> float | None:
        if self.outcome is None:
            return None
        return (self.forecast - self.outcome) ** 2


@dataclass
class Bucket:
    label: str
    forecasts: list[float] = field(default_factory=list)
    outcomes: list[float] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.outcomes)

    @property
    def mean_forecast(self) -> float:
        return sum(self.forecasts) / len(self.forecasts) if self.forecasts else 0.0

    @property
    def actual_rate(self) -> float:
        return sum(self.outcomes) / len(self.outcomes) if self.outcomes else 0.0

    @property
    def gap(self) -> float:
        """Forecast minus reality. Positive means overconfident."""
        return self.mean_forecast - self.actual_rate


@dataclass
class CalibrationReport:
    """How good the agent's probabilities actually are."""

    scored: list[ScoredForecast] = field(default_factory=list)
    prompt_version: str = ""
    model: str = ""

    @property
    def observable(self) -> list[ScoredForecast]:
        return [s for s in self.scored if s.observed]

    @property
    def coverage(self) -> float:
        """Fraction of forecasts with an observable outcome. See the selection-bias
        note in this module's docstring -- this number is how a reader judges how
        much weight the rest deserves."""
        if not self.scored:
            return 0.0
        return len(self.observable) / len(self.scored)

    @property
    def brier_score(self) -> float | None:
        """Mean squared error of the forecasts. Lower is better; 0.25 is coin-flip."""
        errors = [s.squared_error for s in self.observable if s.squared_error is not None]
        return sum(errors) / len(errors) if errors else None

    @property
    def base_rate(self) -> float:
        """Actual recovery rate over observable cases. The number to beat."""
        observed = self.observable
        if not observed:
            return 0.0
        return sum(s.outcome or 0.0 for s in observed) / len(observed)

    @property
    def baseline_brier(self) -> float | None:
        """Brier score of always forecasting the base rate.

        The honest comparison. Beating 0.25 (always saying 50%) proves almost
        nothing; beating this means the agent's per-payment judgement carries
        information beyond the average.
        """
        observed = self.observable
        if not observed:
            return None
        rate = self.base_rate
        return sum((rate - (s.outcome or 0.0)) ** 2 for s in observed) / len(observed)

    @property
    def skill(self) -> float | None:
        """How much better than the base-rate baseline, as a fraction.

        Positive means the forecasts carry real information. Zero or negative means
        the agent would do just as well quoting the average to everyone -- which is
        a result worth reporting honestly rather than burying.
        """
        brier, baseline = self.brier_score, self.baseline_brier
        if brier is None or baseline is None or baseline == 0:
            return None
        return (baseline - brier) / baseline

    def curve(self) -> list[Bucket]:
        """Forecast buckets against measured outcome rates."""
        buckets = [Bucket(label=label) for _, _, label in BUCKETS]
        for scored in self.observable:
            for index, (low, high, _) in enumerate(BUCKETS):
                if low <= scored.forecast < high:
                    buckets[index].forecasts.append(scored.forecast)
                    buckets[index].outcomes.append(scored.outcome or 0.0)
                    break
        return buckets

    def by_cause(self) -> dict[str, Bucket]:
        """Measured recovery rate per failure class.

        The feedback a prompt can consume: if `insufficient_funds` recovers at 20%
        and the agent forecasts 40%, that is correctable.
        """
        causes: dict[str, Bucket] = {}
        for scored in self.observable:
            bucket = causes.setdefault(scored.cause_class, Bucket(label=scored.cause_class))
            bucket.forecasts.append(scored.forecast)
            bucket.outcomes.append(scored.outcome or 0.0)
        return causes

    @property
    def value_error(self) -> Money:
        """Forecast recoverable value minus what was actually recovered.

        Positive means the agent over-promised in rupees. Distinct from Brier score
        because a small probability error on a large payment costs more than a large
        error on a small one, and a merchant cares about the money.
        """
        currency = self.scored[0].authorized.currency if self.scored else "INR"
        forecast_value = sum_money(
            [
                Money(
                    minor_units=int(s.forecast * s.authorized.minor_units),
                    currency=s.authorized.currency,
                )
                for s in self.observable
            ],
            currency=currency,
        )
        actual = sum_money([s.recovered for s in self.observable], currency=currency)
        return forecast_value - actual


def score(events: list[Event], *, currency: str = "INR") -> CalibrationReport:
    """Join recorded forecasts to observed outcomes and score them. Pure fold.

    Purity matters here for the same reason it does in settlement verification: a
    calibration claim that cannot be recomputed from evidence is a marketing number.
    """
    report = CalibrationReport()

    # Outcomes: what actually happened, keyed by the reference both sides share.
    outcomes: dict[str, tuple[bool, Money]] = {}
    for recovery in correlate_recoveries(events, currency=currency):
        if recovery.reference_id:
            outcomes[recovery.reference_id] = (recovery.is_recovered, recovery.paid)

    for event in events:
        if event.type != RECOVERY_PROPOSED:
            continue
        payload = event.payload
        reference = str(payload.get("reference_id", ""))
        amount = _money(payload.get("amount"), currency)
        if amount is None or amount.minor_units <= 0:
            continue

        forecast = payload.get("implied_probability")
        if isinstance(forecast, bool) or not isinstance(forecast, int | float):
            continue

        observed = outcomes.get(reference)
        report.scored.append(
            ScoredForecast(
                payment_id=str(payload.get("payment_id", "")),
                reference_id=reference,
                cause_class=str(payload.get("cause_class", "unknown")),
                strategy=str(payload.get("strategy", "unknown")),
                confidence=str(payload.get("confidence", "unknown")),
                forecast=min(1.0, max(0.0, float(forecast))),
                outcome=(1.0 if observed[0] else 0.0) if observed else None,
                authorized=amount,
                recovered=observed[1] if observed else Money.zero(amount.currency),
            )
        )
        if not report.prompt_version:
            report.prompt_version = str(payload.get("prompt_version", ""))
            report.model = f"{payload.get('provider')}/{payload.get('model')}"

    return report


def _money(raw: object, currency: str) -> Money | None:
    if not isinstance(raw, dict):
        return None
    minor, cur = raw.get("minor_units"), raw.get("currency")
    if isinstance(minor, bool) or not isinstance(minor, int):
        return None
    return Money(minor_units=minor, currency=str(cur or currency))


def render(report: CalibrationReport) -> str:
    """A report a human reads, including what it cannot conclude."""
    lines = [
        "FORECAST CALIBRATION",
        f"  prompt                   : {report.prompt_version}",
        f"  model                    : {report.model}",
        f"  forecasts made           : {len(report.scored)}",
        f"  outcomes observable      : {len(report.observable)}  "
        f"({report.coverage:.0%} coverage)",
        "",
    ]

    if not report.observable:
        lines += [
            "  No outcomes yet -- ingest webhooks before scoring.",
            "  A forecast with no observed outcome cannot be scored, and reporting",
            "  one anyway would be inventing a result.",
        ]
        return "\n".join(lines)

    brier = report.brier_score
    baseline = report.baseline_brier
    skill = report.skill
    lines += [
        "ACCURACY",
        f"  actual recovery rate     : {report.base_rate:.0%}",
        f"  Brier score              : {brier:.4f}   (0 = perfect, 0.25 = coin flip)",
        f"  base-rate baseline       : {baseline:.4f}   (always forecasting the average)",
    ]
    if skill is not None:
        verdict = (
            "the forecasts carry information beyond the average"
            if skill > 0.02
            else "no better than quoting the average to everyone"
        )
        lines.append(f"  skill vs baseline        : {skill:+.1%}   -- {verdict}")
    lines.append("")

    lines.append("CALIBRATION CURVE  (is 70% actually 70%?)")
    for bucket in report.curve():
        if bucket.n == 0:
            continue
        arrow = "overconfident" if bucket.gap > 0.1 else (
            "underconfident" if bucket.gap < -0.1 else "well calibrated"
        )
        lines.append(
            f"  said {bucket.label:<9} n={bucket.n:<4} actually recovered "
            f"{bucket.actual_rate:>5.0%}   {arrow}"
        )
    lines.append("")

    lines.append("MEASURED RECOVERY RATE BY FAILURE CLASS")
    for cause, bucket in sorted(
        report.by_cause().items(), key=lambda kv: -kv[1].n
    ):
        lines.append(
            f"  {cause:<24} n={bucket.n:<4} forecast {bucket.mean_forecast:>5.0%}  "
            f"actual {bucket.actual_rate:>5.0%}  gap {bucket.gap:+.0%}"
        )
    lines += [
        "",
        "VALUE ERROR",
        f"  over/under-promised      : {report.value_error}",
        "  (positive = the agent forecast more recoverable value than arrived)",
        "",
        "WHAT THIS CANNOT TELL YOU",
        "  Outcomes are only observable for recoveries that were attempted, and the",
        "  agent chose those because it was confident. So this measures calibration",
        f"  on the {report.coverage:.0%} it selected, not on the whole cohort -- and it",
        "  will look better than a random sample would. Measuring the rest would mean",
        "  chasing payments the agent judged hopeless, at the merchant's expense.",
    ]
    return "\n".join(lines)
