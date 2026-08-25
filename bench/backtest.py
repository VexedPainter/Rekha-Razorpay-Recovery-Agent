"""Does sequencing recover more money than a single attempt?

The calibration report exists but scores five outcomes, which is not evidence of
anything. This runs the recovery loop forward over simulated days until there is a
sample worth reading, and answers the question the merchant actually has: what did
the extra machinery earn, in rupees?

## Why this is not circular

Outcomes are drawn from each payment's TRUE recovery probability -- the value the
cohort generator chose when it invented the payment, which the model never sees.
Drawing from the model's own forecast would make every calibration score perfect by
construction and the whole exercise worthless.

The AI's forecast and the simulated outcome are therefore independent, which is the
only arrangement under which "was the forecast any good?" has an answer.

## Paired arms, one cohort

Two arms over the SAME payments with the SAME random seed:

  single-shot  the opening action only, follow-up plans discarded
  sequenced    the full plan, advanced by `rekha.razorpay.sequence`

Paired rather than independent samples: the difference between arms is then
attributable to sequencing rather than to one arm drawing an easier cohort. Both
arms consume the same underlying draws for their first attempt, so the first
attempt is not merely similar between arms, it is identical.

## The decay assumption, stated because it drives the result

A customer who ignored two payment links is less likely to pay the third. Modelled
as a multiplicative penalty per contact, and it is an ASSUMPTION, not a measurement
-- nothing in this repo establishes the real figure. It is the single most
load-bearing number in the comparison: at 1.0 sequencing looks free and always
wins, and near 0.3 it barely helps. So it is a parameter, reported in the output,
and swept by `--sweep` so a reader sees how sensitive the conclusion is rather than
one flattering point estimate.
"""

from __future__ import annotations

import argparse
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "examples" / "razorpay-sandbox"))

from cohort import generate_cohort  # noqa: E402
from recovery.agent import derive_now_epoch  # noqa: E402
from recovery.diagnose import diagnose_batch  # noqa: E402
from recovery.proposal import PaymentSnapshot, RecoveryProposal, Strategy  # noqa: E402
from recovery.providers import resolve_provider  # noqa: E402
from rekha.finance.money import Money  # noqa: E402
from rekha.razorpay.sequence import ContactPolicy  # noqa: E402

INR = "INR"

#: Each additional contact is less effective than the last. An assumption, not a
#: measurement -- see the module docstring. Swept rather than asserted.
DEFAULT_FATIGUE = 0.65


@dataclass
class ArmResult:
    """What one arm achieved over the simulated window."""

    name: str
    recovered: Money = field(default_factory=lambda: Money.zero(INR))
    at_risk: Money = field(default_factory=lambda: Money.zero(INR))
    payments_recovered: int = 0
    payments_attempted: int = 0
    contacts: int = 0
    #: Forecast-vs-outcome pairs, for scoring calibration on a real sample.
    observations: list[tuple[float, bool]] = field(default_factory=list)

    @property
    def recovery_rate(self) -> float:
        return (
            self.payments_recovered / self.payments_attempted if self.payments_attempted else 0.0
        )

    @property
    def rupees_per_contact(self) -> float:
        """The efficiency number a merchant cares about. Contacting more customers
        recovers more money and is not automatically better: every contact costs
        goodwill, and a channel has a real per-message price."""
        return self.recovered.minor_units / 100 / self.contacts if self.contacts else 0.0


def _attempt_succeeds(
    rng: random.Random, base_probability: float, contact_index: int, fatigue: float
) -> bool:
    """One contact's outcome, drawn from ground truth and decayed by fatigue."""
    return rng.random() < base_probability * (fatigue**contact_index)


def _run_arm(
    name: str,
    proposals: list[RecoveryProposal],
    truth: dict[str, dict[str, object]],
    *,
    sequenced: bool,
    seed: int,
    fatigue: float,
    policy: ContactPolicy,
) -> ArmResult:
    """Simulate one arm over the whole cohort.

    Both arms draw from a per-payment RNG seeded identically, so the first attempt
    on a given payment resolves the same way in both. Any difference in the result
    is then caused by the later steps only.
    """
    result = ArmResult(name=name)

    for proposal in proposals:
        ground = truth.get(proposal.payment_id)
        if ground is None:
            continue
        result.at_risk = result.at_risk + proposal.amount
        base = float(ground["recovery_probability"])  # type: ignore[arg-type]

        # Per-payment RNG so the arms stay aligned regardless of iteration order.
        rng = random.Random(f"{seed}:{proposal.payment_id}")

        if not proposal.is_actionable and not proposal.follow_up:
            # The agent declined. Correctly declining is not a failure, and it is
            # not an observation either: nobody chased it, so nothing was observed.
            continue

        # The steps this arm will actually take: the opening action, plus the plan
        # if this arm honours plans. `wait` steps are not contacts.
        planned: list[Strategy] = []
        if proposal.is_actionable:
            planned.append(proposal.strategy)
        if sequenced:
            planned.extend(
                step.strategy
                for step in proposal.follow_up
                if step.strategy not in (Strategy.WAIT, Strategy.DO_NOTHING)
            )
        elif not planned:
            # FAIRNESS TO THE BASELINE. When the model opens with `wait`, discarding
            # the plan would leave single-shot doing nothing at all, forever -- and
            # crediting sequencing with the entire recovery on those payments.
            #
            # That would be a rigged comparison. A system without sequencing has no
            # `wait` strategy to begin with (v1 of the prompt did not offer one); it
            # would send a link immediately. So the baseline takes the first real
            # action from the plan, right now, with no delay. This costs the
            # sequenced arm some of its measured uplift, which is the point: a
            # baseline chosen to lose is not a baseline.
            first_action = next(
                (
                    step.strategy
                    for step in proposal.follow_up
                    if step.strategy not in (Strategy.WAIT, Strategy.DO_NOTHING)
                ),
                None,
            )
            if first_action is not None:
                planned.append(first_action)
        planned = planned[: policy.max_touches]
        if not planned:
            continue

        result.payments_attempted += 1
        recovered = False
        for index, _strategy in enumerate(planned):
            result.contacts += 1
            if _attempt_succeeds(rng, base, index, fatigue):
                recovered = True
                result.recovered = result.recovered + proposal.amount
                result.payments_recovered += 1
                break

        # The forecast that was recorded, against what happened. Only payments that
        # were actually chased appear here -- the same selection bias the
        # calibration report already declares.
        implied = (
            proposal.expected_recovery.minor_units / proposal.amount.minor_units
            if proposal.amount.minor_units
            else 0.0
        )
        result.observations.append((implied, recovered))

    return result


@dataclass
class Backtest:
    single: ArmResult
    sequenced: ArmResult
    fatigue: float
    cohort_size: int

    @property
    def uplift(self) -> Money:
        return self.sequenced.recovered - self.single.recovered

    @property
    def uplift_pct(self) -> float:
        base = self.single.recovered.minor_units
        return (self.uplift.minor_units / base * 100) if base else 0.0


def run(
    *,
    count: int = 200,
    seed: int = 20260826,
    fatigue: float = DEFAULT_FATIGUE,
    policy: ContactPolicy | None = None,
) -> Backtest:
    payments = generate_cohort(count)
    anchor = derive_now_epoch(payments)
    truth = {str(p["id"]): p["_truth"] for p in payments}
    snapshots = [PaymentSnapshot.from_razorpay(p, now_epoch=anchor) for p in payments]

    report = diagnose_batch(snapshots, resolve_provider("replay"))
    limits = policy or ContactPolicy()

    return Backtest(
        single=_run_arm(
            "single-shot",
            report.proposals,
            truth,
            sequenced=False,
            seed=seed,
            fatigue=fatigue,
            policy=limits,
        ),
        sequenced=_run_arm(
            "sequenced",
            report.proposals,
            truth,
            sequenced=True,
            seed=seed,
            fatigue=fatigue,
            policy=limits,
        ),
        fatigue=fatigue,
        cohort_size=count,
    )


def _brier(observations: list[tuple[float, bool]]) -> tuple[float, float, float | None]:
    """`(brier, base_rate_baseline, skill)` over forecast/outcome pairs."""
    n = len(observations)
    if not n:
        return 0.0, 0.0, None
    brier = sum((p - (1.0 if hit else 0.0)) ** 2 for p, hit in observations) / n
    rate = sum(1 for _, hit in observations if hit) / n
    baseline = sum((rate - (1.0 if hit else 0.0)) ** 2 for _, hit in observations) / n
    skill = (baseline - brier) / baseline if baseline > 0 else None
    return brier, baseline, skill


def _discrimination(observations: list[tuple[float, bool]]) -> tuple[float, float, float]:
    """`(mean forecast when recovered, when not, separation)`.

    A DIFFERENT QUESTION FROM CALIBRATION, and the one that matters here.

    Calibration asks "when you say 30%, does it happen 30% of the time?".
    Discrimination asks "do the payments you rate higher actually recover more
    often?". A forecaster can be excellent at the second and poor at the first by
    being uniformly too pessimistic -- every forecast shifted down by 15 points
    ranks identically and scores far worse on Brier.

    The distinction is not academic here. The prompt states that the number is used
    ONLY to rank payments against a limited budget, and for ranking, level error
    cancels out entirely. So a negative Brier skill alongside positive separation
    means the forecasts are fit for the purpose they are actually put to, and unfit
    for a purpose nobody uses them for. Both are reported, because quoting only the
    flattering one would be the whole problem with this kind of measurement.
    """
    won = [p for p, hit in observations if hit]
    lost = [p for p, hit in observations if not hit]
    if not won or not lost:
        return 0.0, 0.0, 0.0
    mean_won = sum(won) / len(won)
    mean_lost = sum(lost) / len(lost)
    return mean_won, mean_lost, mean_won - mean_lost


def render(result: Backtest) -> str:
    single, seq = result.single, result.sequenced
    lines = [
        f"BACKTEST  {result.cohort_size} failed payments, outcomes drawn from ground truth",
        f"  contact fatigue assumption : {result.fatigue:.2f} per additional contact",
        "",
        f"  {'arm':<14} {'recovered':>14} {'payments':>9} {'contacts':>9} {'INR/contact':>12}",
    ]
    for arm in (single, seq):
        lines.append(
            f"  {arm.name:<14} {arm.recovered!s:>14} "
            f"{arm.payments_recovered:>4}/{arm.payments_attempted:<4} "
            f"{arm.contacts:>9} {arm.rupees_per_contact:>12,.0f}"
        )
    lines += [
        "",
        f"  UPLIFT FROM SEQUENCING     : {result.uplift} ({result.uplift_pct:+.1f}%)",
        f"  extra contacts spent       : {seq.contacts - single.contacts}",
    ]
    if seq.contacts > single.contacts:
        per = (result.uplift.minor_units / 100) / (seq.contacts - single.contacts)
        lines.append(f"  earned per extra contact   : INR {per:,.0f}")
    lines.append("")

    brier, baseline, skill = _brier(seq.observations)
    mean_won, mean_lost, separation = _discrimination(seq.observations)
    n_obs = len(seq.observations)
    mean_forecast = sum(p for p, _ in seq.observations) / n_obs if n_obs else 0.0
    actual_rate = sum(1 for _, hit in seq.observations if hit) / n_obs if n_obs else 0.0
    lines += [
        f"  FORECAST QUALITY on {n_obs} observed outcomes",
        "",
        "    Calibration -- are the LEVELS right?",
        f"      mean forecast            : {mean_forecast:.3f}",
        f"      actual recovery rate     : {actual_rate:.3f}",
        f"      Brier score              : {brier:.4f}",
        f"      base-rate baseline       : {baseline:.4f}",
        "      skill vs baseline        : "
        + (f"{skill:+.1%}" if skill is not None else "undefined (degenerate baseline)"),
        "",
        "    Discrimination -- is the RANKING right?",
        f"      mean forecast, recovered : {mean_won:.3f}",
        f"      mean forecast, missed    : {mean_lost:.3f}",
        f"      separation               : {separation:+.3f}",
        "",
    ]
    if skill is not None and skill < 0 <= separation:
        lines += [
            "    READ THESE TWO TOGETHER. The model ranks payments correctly -- the",
            "    ones it rates higher do recover more often -- while being uniformly",
            "    too pessimistic about the level. Brier punishes the level error, so",
            "    skill reads negative even though the ordering carries real signal.",
            "",
            "    Which matters, because of what the number is USED for: ranking",
            "    payments against a limited budget, where a constant offset cancels",
            "    out. The forecasts are fit for their actual purpose and unfit for one",
            "    nobody puts them to. Correcting the level would improve the Brier",
            "    score and change no decision this system makes.",
            "",
        ]
    lines += [
        "  The efficiency column is the honest one. Recovering more money by",
        "  contacting people more times is not obviously a win: every contact costs",
        "  goodwill and a real per-message fee, so INR/contact is what decides",
        "  whether sequencing is worth switching on.",
        "",
        "  Outcomes are drawn from each payment's TRUE recovery probability, which",
        "  the model never sees. Drawing them from the model's own forecast would",
        "  make the calibration score perfect by construction.",
        "",
        "  The fatigue figure is an ASSUMPTION, not a measurement, and it drives",
        "  this result more than anything else here. Run --sweep to see the range.",
    ]
    return "\n".join(lines)


def render_sweep(results: list[Backtest]) -> str:
    lines = [
        "SENSITIVITY TO THE CONTACT-FATIGUE ASSUMPTION",
        "",
        f"  {'fatigue':>8} {'single-shot':>14} {'sequenced':>14} {'uplift':>14} {'uplift %':>9}",
    ]
    for r in results:
        lines.append(
            f"  {r.fatigue:>8.2f} {r.single.recovered!s:>14} "
            f"{r.sequenced.recovered!s:>14} {r.uplift!s:>14} {r.uplift_pct:>8.1f}%"
        )
    lines += [
        "",
        "  Reported as a range rather than a point estimate because the honest",
        "  answer is 'it depends on how much customers resent being chased', and",
        "  this repo has no data on that. A reader can find their own row.",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure what sequencing earns.")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--seed", type=int, default=20260826)
    parser.add_argument("--fatigue", type=float, default=DEFAULT_FATIGUE)
    parser.add_argument(
        "--sweep", action="store_true", help="sweep the fatigue assumption instead"
    )
    args = parser.parse_args()

    if args.sweep:
        results = [
            run(count=args.count, seed=args.seed, fatigue=f)
            for f in (0.3, 0.45, 0.65, 0.8, 1.0)
        ]
        print(render_sweep(results))
        return 0

    print(render(run(count=args.count, seed=args.seed, fatigue=args.fatigue)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
