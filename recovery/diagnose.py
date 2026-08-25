"""Batch failure diagnosis: failed payments in, `RecoveryProposal`s out.

One LLM call handles a batch of payments rather than one call per payment. Three
reasons, in order of how much they matter:

1. **A free tier has to be enough.** 200 payments at 25 per call is 8 requests.
   At one per payment it is 200, which rate-limits on every free provider and
   would quietly make a paid key a requirement.
2. **Cross-payment context improves the answer.** Seeing a batch lets the model
   notice that eleven failures share a bank and are probably one outage, which is
   invisible when each payment is judged alone.
3. **Latency.** 8 sequential calls is a demo; 200 is a coffee break.

The model's output is trusted neither structurally nor numerically:

- structurally, every entry is validated against `RecoveryProposal`, and a
  malformed entry is dropped with a recorded reason rather than patched
- numerically, `clamped()` bounds `expected_recovery` at the original amount,
  because a model claiming to recover more than was lost is wrong in a way no
  prompt reliably prevents
- a payment the model omits gets a conservative `do_nothing`, never a guess. An
  absent answer is not permission.

Prompts are files (`recovery/prompts/`), hashed into every proposal, so a decision
is traceable to the exact instruction that produced it and a prompt change shows
up as a change in evidence rather than silent drift in behaviour.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from belay.canonical import canonical_bytes, sha256_hex
from belay.finance.money import Money
from pydantic import ValidationError

from recovery.proposal import (
    CauseClass,
    Confidence,
    PaymentSnapshot,
    RecoveryProposal,
    RecoveryStep,
    Strategy,
)
from recovery.providers import LLMProvider, ProviderError

PROMPT_DIR = Path(__file__).parent / "prompts"
DEFAULT_PROMPT = "diagnose_v2"

#: How many payments per request. Chosen empirically against the real Gemini free
#: tier: 15 completes in ~24s, 25 reliably drops the connection mid-generation
#: (25 proposals of prose is a lot of output tokens). 10 leaves margin and still
#: makes a 200-payment cohort 20 calls, which is comfortably inside a free tier --
#: versus 200 calls at one per payment, which rate-limits.
DEFAULT_BATCH_SIZE = 10


@lru_cache(maxsize=8)
def load_prompt(name: str = DEFAULT_PROMPT) -> tuple[str, str]:
    """`(text, version)` for a prompt file, where version includes a content hash.

    The hash is what makes provenance real: `diagnose_v1@a1b2c3d4` identifies the
    exact bytes that produced a decision, so editing a prompt cannot silently
    change behaviour while the recorded version stays the same.
    """
    path = PROMPT_DIR / f"{name}.md"
    text = path.read_text(encoding="utf-8")
    digest = sha256_hex(text.encode("utf-8"))[:8]
    return text, f"{name}@{digest}"


#: The response schema handed to the provider. Written out rather than generated
#: from the Pydantic model because the wire shape is deliberately flatter than the
#: domain model: amounts cross as plain integer paise (a model handles a number
#: far more reliably than a nested `{minor_units, currency}` object), and `Money`
#: is reconstructed on arrival where the currency is known from the payment.
RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "payment_id": {"type": "string"},
                    "cause_class": {
                        "type": "string",
                        "enum": [c.value for c in CauseClass],
                    },
                    "strategy": {"type": "string", "enum": [s.value for s in Strategy]},
                    "amount_paise": {"type": "integer"},
                    "expected_recovery_paise": {"type": "integer"},
                    "confidence": {
                        "type": "string",
                        "enum": [c.value for c in Confidence],
                    },
                    "diagnosis": {"type": "string"},
                    "reasoning": {"type": "string"},
                    # The follow-up plan. Optional in the schema rather than
                    # required: an empty sequence is a legitimate answer (one
                    # well-chosen action, or `do_nothing`), and requiring the field
                    # would push the model towards inventing steps to fill it.
                    "follow_up": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "strategy": {
                                    "type": "string",
                                    "enum": [s.value for s in Strategy],
                                },
                                "wait_hours": {"type": "integer"},
                                "rationale": {"type": "string"},
                            },
                            "required": ["strategy", "wait_hours", "rationale"],
                        },
                    },
                },
                "required": [
                    "payment_id",
                    "cause_class",
                    "strategy",
                    "amount_paise",
                    "expected_recovery_paise",
                    "confidence",
                    "diagnosis",
                    "reasoning",
                ],
            },
        }
    },
    "required": ["proposals"],
}


@dataclass
class DiagnosisReport:
    """Proposals, plus an honest account of what went wrong producing them.

    `rejected` and `omitted` exist because a batch that silently returns fewer
    proposals than it was asked for looks like a model exercising judgement, when
    it may instead be a model failing. Distinguishing the two is the difference
    between measuring a system and flattering it.
    """

    proposals: list[RecoveryProposal] = field(default_factory=list)
    #: `(payment_id, why)` for entries the model returned that failed validation.
    rejected: list[tuple[str, str]] = field(default_factory=list)
    #: Payment ids the model was asked about and did not answer for. These get a
    #: conservative `do_nothing` proposal, counted here so the gap is visible.
    omitted: list[str] = field(default_factory=list)
    prompt_version: str = ""
    provider: str = ""
    model: str = ""
    calls: int = 0

    @property
    def actionable(self) -> list[RecoveryProposal]:
        return [p for p in self.proposals if p.is_actionable]


def _snapshot_for_prompt(snapshot: PaymentSnapshot) -> dict[str, object]:
    """The payment as the model sees it. Amounts flatten to integer paise."""
    return {
        "payment_id": snapshot.payment_id,
        "amount_paise": snapshot.amount.minor_units,
        "currency": snapshot.amount.currency,
        "method": snapshot.method,
        "error_code": snapshot.error_code,
        "error_description": snapshot.error_description,
        "error_source": snapshot.error_source,
        "error_step": snapshot.error_step,
        "error_reason": snapshot.error_reason,
        "age_hours": snapshot.age_hours,
        "has_saved_method": snapshot.has_saved_method,
        "contact_present": snapshot.contact_present,
        "notes": snapshot.notes,
    }


def _do_nothing(
    snapshot: PaymentSnapshot, *, reason: str, provenance: tuple[str, str, str]
) -> RecoveryProposal:
    """The conservative default for a payment the model did not usefully answer.

    Not an error and not a guess: proposing nothing is always safe, and it keeps
    the batch's arithmetic complete so `revenue at risk` and `recovery rate` are
    computed over every payment rather than only the ones that went well.
    """
    prompt_version, provider, model = provenance
    return RecoveryProposal(
        payment_id=snapshot.payment_id,
        cause_class=CauseClass.PERMANENTLY_DEAD,
        strategy=Strategy.DO_NOTHING,
        amount=snapshot.amount,
        expected_recovery=Money.zero(snapshot.amount.currency),
        confidence=Confidence.LOW,
        diagnosis="No usable diagnosis was produced for this payment.",
        reasoning=reason,
        prompt_version=prompt_version,
        provider=provider,
        model=model,
    )


#: Longest follow-up plan accepted. Three contacts is the merchant ceiling, and
#: `wait` steps consume none, so six leaves room for interleaved waits while
#: bounding what one model response can write into the ledger. A model returning
#: fifty steps is malfunctioning, not being thorough.
MAX_FOLLOW_UP_STEPS = 6


def _parse_follow_up(raw: object) -> tuple[tuple[RecoveryStep, ...], str | None]:
    """Parse a follow-up plan, truncating at the first thing that does not parse.

    TRUNCATE RATHER THAN REJECT OR REPAIR. The two obvious options are both worse:
    dropping the whole proposal because step three was malformed throws away a
    legitimate recovery over a detail that has not happened yet, and skipping the
    bad step silently changes the plan into one the model did not propose. Cutting
    the plan short keeps every step that was actually understood, invents nothing,
    and returns a reason so the truncation is recorded rather than inferred.

    Nothing dangerous can survive a short plan: the opening action is unaffected,
    and every later step is re-checked by `belay.razorpay.sequence` anyway.

    Note what is NOT validated here: two link-creating steps in a row. The prompt
    asks the model to avoid it, but the guarantee lives in the executor, which
    demands the live link be cancelled first. A structural invariant is worth more
    than prompt compliance, so this parser does not duplicate it.
    """
    if raw is None:
        return (), None
    if not isinstance(raw, list):
        return (), f"follow_up was {type(raw).__name__}, not a list"

    steps: list[RecoveryStep] = []
    for index, entry in enumerate(raw):
        if len(steps) >= MAX_FOLLOW_UP_STEPS:
            return tuple(steps), f"plan truncated at {MAX_FOLLOW_UP_STEPS} steps"
        if not isinstance(entry, dict):
            return tuple(steps), f"step {index} was {type(entry).__name__}, not an object"
        try:
            steps.append(
                RecoveryStep(
                    strategy=Strategy(str(entry["strategy"])),
                    wait_hours=int(entry.get("wait_hours") or 0),
                    rationale=str(entry.get("rationale") or "").strip(),
                )
            )
        except (KeyError, ValueError, TypeError, ValidationError) as exc:
            return tuple(steps), f"step {index} rejected: {type(exc).__name__}"
    return tuple(steps), None


def _parse_entry(
    entry: object,
    snapshots: dict[str, PaymentSnapshot],
    provenance: tuple[str, str, str],
) -> tuple[RecoveryProposal | None, tuple[str, str] | None]:
    """Validate one model entry into a clamped proposal, or explain the rejection."""
    prompt_version, provider, model = provenance
    if not isinstance(entry, dict):
        return None, ("<unknown>", f"entry was {type(entry).__name__}, not an object")

    payment_id = str(entry.get("payment_id", ""))
    snapshot = snapshots.get(payment_id)
    if snapshot is None:
        # A payment id we never asked about. Dropped rather than acted on: the
        # model does not get to widen the batch it was given.
        return None, (payment_id or "<missing>", "payment_id was not in the request")

    currency = snapshot.amount.currency
    try:
        amount_paise = int(entry["amount_paise"])
        expected_paise = int(entry["expected_recovery_paise"])
        if amount_paise <= 0:
            # A recovery for zero or a negative amount is not a recovery. Rejected
            # rather than clamped: clamping to zero would produce a worthless
            # payment link, and clamping up to the original would be deciding on
            # the model's behalf. This module's rule is to reject malformed output
            # wholesale, never to patch it into something plausible.
            return None, (payment_id, f"amount_paise was {amount_paise}, must be positive")
        if expected_paise < 0:
            return None, (
                payment_id,
                f"expected_recovery_paise was {expected_paise}, must not be negative",
            )
        proposal = RecoveryProposal(
            payment_id=payment_id,
            cause_class=CauseClass(str(entry["cause_class"])),
            strategy=Strategy(str(entry["strategy"])),
            amount=Money(minor_units=amount_paise, currency=currency),
            expected_recovery=Money(minor_units=expected_paise, currency=currency),
            confidence=Confidence(str(entry["confidence"])),
            diagnosis=str(entry["diagnosis"]).strip(),
            reasoning=str(entry["reasoning"]).strip(),
            follow_up=_parse_follow_up(entry.get("follow_up"))[0],
            prompt_version=prompt_version,
            provider=provider,
            model=model,
        )
    except (KeyError, ValueError, TypeError, ValidationError) as exc:
        return None, (payment_id, f"{type(exc).__name__}: {exc}")

    return proposal.clamped(snapshot.amount), None


def diagnose_batch(
    snapshots: list[PaymentSnapshot],
    provider: LLMProvider,
    *,
    prompt: str = DEFAULT_PROMPT,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> DiagnosisReport:
    """Diagnose `snapshots` and propose a recovery for each.

    Every input payment gets exactly one proposal in the report -- a real one if
    the model produced a valid entry, a conservative `do_nothing` otherwise. That
    invariant matters because the batch metrics the track bar asks for
    ("measured money recovered across a batch") are only meaningful if the
    denominator is the whole batch.
    """
    system, prompt_version = load_prompt(prompt)
    provenance = (prompt_version, provider.name, provider.model)
    report = DiagnosisReport(
        prompt_version=prompt_version, provider=provider.name, model=provider.model
    )
    if not snapshots:
        return report

    by_id = {s.payment_id: s for s in snapshots}
    answered: set[str] = set()

    for start in range(0, len(snapshots), batch_size):
        chunk = snapshots[start : start + batch_size]
        user = json.dumps(
            {"payments": [_snapshot_for_prompt(s) for s in chunk]},
            indent=2,
            ensure_ascii=False,
        )
        report.calls += 1
        try:
            body = provider.complete_json(
                system=system, user=user, schema=RESPONSE_SCHEMA
            )
        except ProviderError as exc:
            # A failed call is not a reason to act unadvised. Every payment in the
            # chunk falls back to `do_nothing`, and the failure is recorded.
            for snapshot in chunk:
                report.omitted.append(snapshot.payment_id)
                report.proposals.append(
                    _do_nothing(
                        snapshot,
                        reason=f"diagnosis call failed: {exc}",
                        provenance=provenance,
                    )
                )
                answered.add(snapshot.payment_id)
            continue

        entries = body.get("proposals")
        if not isinstance(entries, list):
            entries = []
        for entry in entries:
            proposal, rejection = _parse_entry(entry, by_id, provenance)
            if proposal is not None:
                if proposal.payment_id in answered:
                    # A duplicate answer for one payment. Keep the first; a model
                    # does not get two votes.
                    report.rejected.append((proposal.payment_id, "duplicate entry"))
                    continue
                report.proposals.append(proposal)
                answered.add(proposal.payment_id)
            elif rejection is not None:
                report.rejected.append(rejection)

    for snapshot in snapshots:
        if snapshot.payment_id not in answered:
            report.omitted.append(snapshot.payment_id)
            report.proposals.append(
                _do_nothing(
                    snapshot,
                    reason="the model returned no entry for this payment",
                    provenance=provenance,
                )
            )

    # Preserve the caller's order, so a report is comparable run to run.
    order = {s.payment_id: i for i, s in enumerate(snapshots)}
    report.proposals.sort(key=lambda p: order.get(p.payment_id, 1 << 30))
    return report


def request_digest(snapshots: list[PaymentSnapshot], prompt: str = DEFAULT_PROMPT) -> str:
    """Stable digest of a diagnosis request. Used to name recorded fixtures."""
    _, version = load_prompt(prompt)
    return sha256_hex(
        canonical_bytes(
            {
                "prompt": version,
                "payments": [_snapshot_for_prompt(s) for s in snapshots],
            }
        )
    )[:20]
