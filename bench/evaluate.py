"""Diagnosis accuracy against ground truth, on a held-out set.

The agent classifies each failed payment into a cause class. This measures whether
it is right, using labels it never saw.

## Why this is not circular

The cohort is synthetic, so we know why each payment failed -- we decided when we
generated it. That label lives under a `_truth` key which the sandbox strips at its
read boundary, so the model receives only the error fields a real Razorpay payment
exposes. `tests/bench/` asserts the label cannot reach the prompt.

That is the whole methodological point of synthetic data for evaluation: real data
gives you realism without labels, synthetic data gives you labels without realism,
and for measuring a classifier the labels are what you need.

## Why a baseline is mandatory

"78% accurate" is meaningless alone. If 56 of 200 payments are `bank_transient`, a
model that answers `bank_transient` every time scores 28% -- so any number must be
read against that. Two baselines are reported:

- **majority class** -- always answer the most common label
- **keyword rule** -- a short table matching obvious phrases in the error text

The second is the honest bar. If a twelve-line rule table matches the model, the
model is not adding value and the project should say so rather than hide it.

## Macro rather than micro averaging

Per-class scores are averaged unweighted, so the four `permanently_dead` payments
count as much as the 56 `bank_transient` ones. Micro averaging would let good
performance on the common classes bury total failure on the rare ones -- and
`permanently_dead` is the class where being wrong costs the most, because chasing a
fraud-blocked payment wastes money and annoys a customer.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "examples" / "razorpay-sandbox") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "examples" / "razorpay-sandbox"))

from cohort import generate_cohort  # noqa: E402
from recovery.agent import derive_now_epoch  # noqa: E402
from recovery.diagnose import diagnose_batch  # noqa: E402
from recovery.proposal import PaymentSnapshot  # noqa: E402
from recovery.providers import LLMProvider, resolve_provider  # noqa: E402

#: A deliberately crude keyword table, used ONLY as a baseline to measure the model
#: against. If this matches the model's accuracy, the model is not earning its place.
_KEYWORD_RULES: tuple[tuple[str, str], ...] = (
    ("high risk", "permanently_dead"),
    ("blocked", "permanently_dead"),
    ("insufficient funds", "insufficient_funds"),
    ("expired", "method_unsupported"),
    ("exceeds the limit", "method_unsupported"),
    ("cancelled by the customer", "customer_recoverable"),
    ("expired before the customer", "customer_recoverable"),
    ("timeout", "bank_transient"),
    ("gateway", "bank_transient"),
    ("declined by the issuing bank", "bank_transient"),
    ("3d secure", "authentication_failed"),
    ("otp", "authentication_failed"),
)


def keyword_baseline(description: str) -> str:
    """First matching keyword wins. Falls back to the majority class."""
    lowered = (description or "").lower()
    for needle, label in _KEYWORD_RULES:
        if needle in lowered:
            return label
    return "bank_transient"


@dataclass
class ClassScore:
    label: str
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0

    @property
    def support(self) -> int:
        return self.true_positives + self.false_negatives

    @property
    def precision(self) -> float:
        predicted = self.true_positives + self.false_positives
        return self.true_positives / predicted if predicted else 0.0

    @property
    def recall(self) -> float:
        return self.true_positives / self.support if self.support else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass
class EvaluationReport:
    """Accuracy of one classifier over a held-out set."""

    name: str
    n: int = 0
    correct: int = 0
    classes: dict[str, ClassScore] = field(default_factory=dict)
    confusion: dict[tuple[str, str], int] = field(default_factory=dict)

    @property
    def accuracy(self) -> float:
        return self.correct / self.n if self.n else 0.0

    @property
    def macro_f1(self) -> float:
        """Unweighted mean F1. Rare classes count as much as common ones."""
        scores = [c.f1 for c in self.classes.values() if c.support]
        return sum(scores) / len(scores) if scores else 0.0

    @property
    def macro_precision(self) -> float:
        scores = [c.precision for c in self.classes.values() if c.support]
        return sum(scores) / len(scores) if scores else 0.0

    @property
    def macro_recall(self) -> float:
        scores = [c.recall for c in self.classes.values() if c.support]
        return sum(scores) / len(scores) if scores else 0.0

    def worst_confusions(self, limit: int = 4) -> list[tuple[str, str, int]]:
        """The most frequent mistakes, so the failure mode is visible not just the score."""
        wrong = [
            (truth, predicted, count)
            for (truth, predicted), count in self.confusion.items()
            if truth != predicted
        ]
        return sorted(wrong, key=lambda row: -row[2])[:limit]


def _tally(name: str, pairs: list[tuple[str, str]]) -> EvaluationReport:
    """Build a report from `(truth, prediction)` pairs."""
    report = EvaluationReport(name=name, n=len(pairs))
    labels = {truth for truth, _ in pairs} | {pred for _, pred in pairs}
    report.classes = {label: ClassScore(label=label) for label in labels}

    for truth, predicted in pairs:
        report.confusion[(truth, predicted)] = report.confusion.get((truth, predicted), 0) + 1
        if truth == predicted:
            report.correct += 1
            report.classes[truth].true_positives += 1
        else:
            report.classes[predicted].false_positives += 1
            report.classes[truth].false_negatives += 1
    return report


@dataclass
class Evaluation:
    """The model against both baselines, on the same held-out payments."""

    model: EvaluationReport
    majority: EvaluationReport
    keyword: EvaluationReport
    holdout_size: int
    train_size: int
    prompt_version: str = ""
    provider: str = ""

    @property
    def beats_majority(self) -> bool:
        return self.model.macro_f1 > self.majority.macro_f1

    @property
    def beats_keyword(self) -> bool:
        return self.model.macro_f1 > self.keyword.macro_f1


def evaluate(
    *,
    count: int = 200,
    holdout_fraction: float = 0.5,
    seed: int = 4242,
    provider: LLMProvider | None = None,
) -> Evaluation:
    """Diagnose a held-out slice of the cohort and score it against ground truth.

    The split uses its own seed, independent of the cohort's, so the held-out set is
    not correlated with generation order. `holdout_fraction` defaults to half: the
    training half exists to make the split real, though nothing is currently trained
    on it -- the prompt is hand-written. Reported honestly rather than implying a
    fitted model.
    """
    payments = generate_cohort(count)
    anchor = derive_now_epoch(payments)

    rng = random.Random(seed)
    indices = list(range(len(payments)))
    rng.shuffle(indices)
    split = int(len(indices) * (1 - holdout_fraction))
    holdout = [payments[i] for i in indices[split:]]

    # Diagnose the FULL cohort, then score only the held-out slice.
    #
    # Not a shortcut. Diagnosing the shuffled subset directly would batch payments
    # differently from every other run in the project, which changes the request and
    # therefore misses the recorded fixtures -- and silently scoring a fallback
    # default at 3% would look like a catastrophic model rather than a plumbing
    # mistake. It cost an hour to find that the first time.
    #
    # Methodologically equivalent: predictions are made without labels either way,
    # and nothing is trained, so applying the split at scoring time rather than
    # prediction time changes nothing about what the model knew.
    snapshots = [PaymentSnapshot.from_razorpay(p, now_epoch=anchor) for p in payments]
    llm = provider or resolve_provider("replay")
    report = diagnose_batch(snapshots, llm)

    predicted_by_id = {p.payment_id: p.cause_class.value for p in report.proposals}
    truth_by_id = {str(p["id"]): p["_truth"]["cause_class"] for p in holdout}

    model_pairs: list[tuple[str, str]] = []
    keyword_pairs: list[tuple[str, str]] = []
    majority_pairs: list[tuple[str, str]] = []

    # The majority class is taken from the TRAINING half, not the held-out half.
    # Reading it from the test set would be leakage -- a baseline that peeks is not
    # a baseline.
    train_truths = [payments[i]["_truth"]["cause_class"] for i in indices[:split]]
    majority_label = max(set(train_truths), key=train_truths.count) if train_truths else ""

    for payment in holdout:
        payment_id = str(payment["id"])
        truth = truth_by_id[payment_id]
        model_pairs.append((truth, predicted_by_id.get(payment_id, "permanently_dead")))
        keyword_pairs.append((truth, keyword_baseline(str(payment.get("error_description", "")))))
        majority_pairs.append((truth, majority_label))

    return Evaluation(
        model=_tally(f"{report.provider}/{report.model}", model_pairs),
        majority=_tally(f"majority class ({majority_label})", majority_pairs),
        keyword=_tally("keyword rule table", keyword_pairs),
        holdout_size=len(holdout),
        train_size=split,
        prompt_version=report.prompt_version,
        provider=f"{report.provider}/{report.model}",
    )


def render(evaluation: Evaluation) -> str:
    """A report a human reads, including whether the model earned its place."""
    model = evaluation.model
    lines = [
        "DIAGNOSIS ACCURACY  (held-out set, labels the model never saw)",
        f"  held-out payments        : {evaluation.holdout_size}"
        f"   (train split: {evaluation.train_size}, unused -- the prompt is hand-written)",
        f"  model                    : {evaluation.provider}",
        f"  prompt                   : {evaluation.prompt_version}",
        "",
        f"  {'classifier':<28} {'accuracy':>9} {'macro P':>9} {'macro R':>9} {'macro F1':>9}",
    ]
    for report in (evaluation.majority, evaluation.keyword, model):
        lines.append(
            f"  {report.name[:28]:<28} {report.accuracy:>8.1%} "
            f"{report.macro_precision:>8.1%} {report.macro_recall:>8.1%} "
            f"{report.macro_f1:>8.1%}"
        )
    lines.append("")

    if evaluation.beats_keyword:
        lines.append(
            "  The model beats a keyword rule table, so its judgement carries"
        )
        lines.append("  information the error text does not state literally.")
    else:
        lines.append(
            "  The model does NOT beat a keyword rule table on macro F1. Stated"
        )
        lines.append(
            "  plainly rather than hidden: on this cohort a rule table is competitive,"
        )
        lines.append(
            "  and the model's value is in strategy and expected-value reasoning"
        )
        lines.append("  rather than in classification alone.")
    lines.append("")

    lines.append("  PER CLASS")
    for label, score in sorted(model.classes.items(), key=lambda kv: -kv[1].support):
        if not score.support:
            continue
        lines.append(
            f"    {label:<24} n={score.support:<4} P {score.precision:>5.0%}  "
            f"R {score.recall:>5.0%}  F1 {score.f1:>5.0%}"
        )
    lines.append("")

    confusions = model.worst_confusions()
    if confusions:
        lines.append("  MOST COMMON MISTAKES  (true -> predicted)")
        for truth, predicted, count in confusions:
            lines.append(f"    {truth:<24} -> {predicted:<24} {count}x")
        lines.append("")

    lines += [
        "  Macro-averaged on purpose: the 4 permanently_dead payments count as much",
        "  as the 56 bank_transient ones. Micro averaging would let strong",
        "  performance on common classes bury total failure on the rare ones -- and",
        "  permanently_dead is where being wrong costs the most.",
        "",
        "  READ THIS SCORE WITH CARE. It is high partly because the cohort is",
        "  synthetic: each failure mode has one clean, distinctive error description,",
        "  so the classes are more separable than real Razorpay traffic would be.",
        "  Real data carries truncated messages, gateway-specific wording, and",
        "  genuinely ambiguous cases. The number that transfers is the MARGIN over the",
        f"  keyword baseline ({model.macro_f1 - evaluation.keyword.macro_f1:+.1%}), not the",
        "  absolute figure -- both classifiers see the same clean text, so the gap is",
        "  what measures the model's contribution.",
    ]
    return "\n".join(lines)
