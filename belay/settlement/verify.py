"""Three-way settlement verification: did the money that moved match what we authorized?

Everything else in this system verifies that we *controlled* what we *intended*.
This is the only check that asks whether reality agrees, and it is the only one
that does not trust our own records.

Three legs, three sources, and the third is what makes it verification rather
than bookkeeping:

    AUTHORIZED   our hash-chained ledger -- `spend` effects that passed the
                 mandate and policy and reached `step_committed`
    REPORTED     Razorpay webhooks -- what Razorpay says happened
    SETTLED      Razorpay's settlement reconciliation -- money that actually
                 moved to the merchant's bank account

Comparing AUTHORIZED against REPORTED alone would be self-referential: a webhook
arrives because we created a payment link, so both legs descend from our own
request. SETTLED is independent of it.

The join is not direct and that matters. Our ledger knows payment *link* ids
(`plink_...`); settlement reconciliation is itemised by *payment* id (`pay_...`).
The webhook is the only record carrying both, so the three-way join genuinely
requires all three legs -- there is no shortcut that reads two of them.

**Scope, stated precisely.** A merchant's settlement report contains every payment
they took, not only agent-driven recoveries. Flagging "settled with no authorizing
ledger entry" across the whole report would mark every organic sale as
unauthorized, which would be alarming, wrong, and would destroy the credibility of
every other verdict. So verification is scoped to entries that claim to be ours,
by the `recover-{payment_id}` reference this system controls. Everything else is
counted as `unrelated` and explicitly not judged.

Verdicts mirror the honest four-value taxonomy `belay/rewind/service.py` uses for
Verified Rewind, for the same reason it exists there: absence of evidence must
never be reported as agreement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from belay.finance.money import Money
from belay.finance.money import total as sum_money
from belay.ledger.model import Event
from belay.razorpay.webhooks import WEBHOOK_RECEIVED

#: Reference ids this system issues. The scope boundary: an entry claiming this
#: prefix is claiming to be one of ours, and is therefore ours to verify.
RECOVERY_REFERENCE_PREFIX = "recover-"


class SettlementResult(StrEnum):
    """What can honestly be said about one payment's settlement."""

    #: All three legs agree, within declared fee tolerance.
    MATCHED = "matched"
    #: The legs disagree in a way that needs a human. See `MismatchReason`.
    MISMATCHED = "mismatched"
    #: Authorized and reported, but settlement data has not arrived yet.
    #: Never `matched`: not-yet-settled is not the same as settled correctly.
    PENDING = "pending"
    #: No settlement source at all, so nothing can be concluded. Never `matched`:
    #: this is the verdict that keeps a missing leg from reading as success.
    UNVERIFIABLE = "unverifiable"


class MismatchReason(StrEnum):
    """Why three legs disagree. Every mismatch names one."""

    #: Settled gross differs from what was authorized.
    AMOUNT_MISMATCH = "amount_mismatch"
    #: Money settled against a recovery reference this system never authorized.
    #: The case the whole module exists for -- the only one detectable from
    #: outside our own records.
    UNAUTHORIZED_PAYMENT = "unauthorized_payment"
    #: More than one settlement for a single authorization.
    DUPLICATE_CAPTURE = "duplicate_capture"
    #: Reported and settled disagree, but neither matches the authorization
    #: either -- so the discrepancy is not attributable to fees.
    REPORTED_SETTLED_DIVERGENCE = "reported_settled_divergence"


class SettlementSource(Protocol):
    """Where the SETTLED leg comes from.

    A protocol with two implementations, and the reason is worth recording: a
    brand-new Razorpay test-mode account returns HTTP 200 with zero settlements
    (verified by `scripts/check_razorpay.py`), because settlement is a real banking
    event on a real cycle. So the live source is correct and currently empty, and
    the fixture source is what makes the verifier exercisable. Which one ran is
    reported, never implied.
    """

    name: str

    def recon_entries(self) -> list[dict[str, Any]]:
        """Itemised settlement reconciliation entries."""
        ...


@dataclass(frozen=True)
class SettlementCase:
    """One payment, judged across all three legs."""

    reference_id: str
    result: SettlementResult
    reason: MismatchReason | None = None
    payment_link_id: str | None = None
    payment_id: str | None = None
    authorized: Money | None = None
    reported: Money | None = None
    settled_gross: Money | None = None
    fee: Money | None = None
    tax: Money | None = None
    net_credit: Money | None = None
    detail: str = ""

    @property
    def fee_variance_only(self) -> bool:
        """Whether the only difference between gross and net is fees and tax.

        This is deliberately NOT a mismatch. Razorpay deducts a fee and GST from
        every settlement, so net < gross on every correct transaction. Reporting
        that as a discrepancy would produce a false positive on literally every
        payment -- and a verifier that cries wolf on normal traffic is worse than
        no verifier, because its real findings get ignored.
        """
        if self.settled_gross is None or self.net_credit is None:
            return False
        deductions = sum_money(
            [m for m in (self.fee, self.tax) if m is not None],
            currency=self.settled_gross.currency,
        )
        return self.net_credit + deductions == self.settled_gross


@dataclass
class SettlementReport:
    """The verification outcome for a session, plus what it declined to judge."""

    source: str
    cases: list[SettlementCase] = field(default_factory=list)
    #: Settlement entries that are not ours -- organic merchant traffic. Counted,
    #: never judged. Reported so the number is visible rather than silently
    #: dropped, since "we checked 5 of 500 entries" is material context.
    unrelated: int = 0
    currency: str = "INR"

    def by_result(self, result: SettlementResult) -> list[SettlementCase]:
        return [case for case in self.cases if case.result is result]

    @property
    def matched(self) -> list[SettlementCase]:
        return self.by_result(SettlementResult.MATCHED)

    @property
    def mismatched(self) -> list[SettlementCase]:
        return self.by_result(SettlementResult.MISMATCHED)

    @property
    def pending(self) -> list[SettlementCase]:
        return self.by_result(SettlementResult.PENDING)

    @property
    def unverifiable(self) -> list[SettlementCase]:
        return self.by_result(SettlementResult.UNVERIFIABLE)

    @property
    def verdict(self) -> SettlementResult:
        """One verdict for the session, taking the worst case present.

        Most-severe-wins, matching how `PolicyEngine` combines its dimensions:
        one mismatch makes the session mismatched, however many cases matched.
        A single unreconciled rupee is the finding, not the average.
        """
        for result in (
            SettlementResult.MISMATCHED,
            SettlementResult.UNVERIFIABLE,
            SettlementResult.PENDING,
        ):
            if self.by_result(result):
                return result
        return SettlementResult.MATCHED

    @property
    def settled_total(self) -> Money:
        return sum_money(
            [c.settled_gross for c in self.cases if c.settled_gross is not None],
            currency=self.currency,
        )

    @property
    def authorized_total(self) -> Money:
        return sum_money(
            [c.authorized for c in self.cases if c.authorized is not None],
            currency=self.currency,
        )

    @property
    def fees_total(self) -> Money:
        return sum_money(
            [m for c in self.cases for m in (c.fee, c.tax) if m is not None],
            currency=self.currency,
        )

    def mismatches_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for case in self.mismatched:
            key = case.reason.value if case.reason else "unspecified"
            counts[key] = counts.get(key, 0) + 1
        return counts


class FixtureSettlementSource:
    """Settlement entries from a list supplied by the caller.

    Not a stub for something unwritten -- it is the only usable source while a
    test-mode account has no settlements. The verifier cannot tell the difference,
    which is the point.
    """

    name = "fixture"

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    def recon_entries(self) -> list[dict[str, Any]]:
        return list(self._entries)


class EmptySettlementSource:
    """No settlement data. Everything authorized becomes `unverifiable`.

    Exists so "we have no settlement data" is a first-class, reportable state
    rather than an empty report that reads like a clean bill of health.
    """

    name = "none"

    def recon_entries(self) -> list[dict[str, Any]]:
        return []


def _money(entity: dict[str, Any], key: str, currency: str) -> Money | None:
    raw = entity.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return Money(minor_units=raw, currency=str(entity.get("currency") or currency))


def _authorizations(events: list[Event], currency: str) -> dict[str, dict[str, Any]]:
    """AUTHORIZED leg: recovery links this control plane created, by reference id.

    Read from `result_recorded`, which only exists for a step that passed the
    mandate, passed policy, and committed -- so presence here IS the authorization.
    """
    found: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type != "result_recorded":
            continue
        result = event.payload.get("result")
        if not isinstance(result, dict):
            continue
        link_id, reference = result.get("id"), result.get("reference_id")
        amount = result.get("amount")
        if not isinstance(link_id, str) or not link_id.startswith("plink_"):
            continue
        if not isinstance(reference, str) or not reference:
            continue
        if isinstance(amount, bool) or not isinstance(amount, int):
            continue
        found[reference] = {
            "payment_link_id": link_id,
            "authorized": Money(
                minor_units=amount, currency=str(result.get("currency") or currency)
            ),
        }
    return found


def _reported(events: list[Event], currency: str) -> dict[str, dict[str, Any]]:
    """REPORTED leg: what webhooks say, keyed by reference id.

    Also carries `payment_id`, which is the only bridge between our link ids and
    settlement's payment ids -- the reason all three legs are genuinely needed.
    """
    found: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type != WEBHOOK_RECEIVED or not event.payload.get("is_payment"):
            continue
        reference = event.payload.get("reference_id")
        if not isinstance(reference, str) or not reference:
            continue
        raw = event.payload.get("amount_paid") or event.payload.get("amount")
        paid = None
        if isinstance(raw, dict):
            minor, cur = raw.get("minor_units"), raw.get("currency")
            if not isinstance(minor, bool) and isinstance(minor, int):
                paid = Money(minor_units=minor, currency=str(cur or currency))
        existing = found.get(reference)
        # Highest reported amount wins: `payment_link.paid` restates the
        # cumulative total, so a partial followed by a full payment must not sum.
        if existing is None or (paid is not None and paid > existing["reported"]):
            found[reference] = {
                "reported": paid or Money.zero(currency),
                "payment_id": event.payload.get("payment_id"),
                "payment_link_id": event.payload.get("payment_link_id"),
            }
    return found


def verify(
    events: list[Event],
    source: SettlementSource,
    *,
    currency: str = "INR",
) -> SettlementReport:
    """Reconcile authorized, reported, and settled. Pure -- no I/O.

    Purity is what lets the verdict be replayed from evidence and signed into an
    evidence bundle: a reviewer can recompute it without our database, our
    credentials, or our word.
    """
    report = SettlementReport(source=source.name, currency=currency)
    authorized = _authorizations(events, currency)
    reported = _reported(events, currency)
    entries = source.recon_entries()

    # SETTLED leg, grouped by the reference each entry claims. Grouping rather than
    # mapping so more than one settlement per authorization is detectable at all.
    settled: dict[str, list[dict[str, Any]]] = {}
    payment_to_reference = {
        info.get("payment_id"): reference
        for reference, info in reported.items()
        if info.get("payment_id")
    }

    for entry in entries:
        reference = entry.get("reference_id")
        if not isinstance(reference, str) or not reference:
            # No reference of its own: bridge via the payment id the webhook
            # supplied. This is the three-way join.
            reference = payment_to_reference.get(entry.get("entity_id"))
        if not isinstance(reference, str) or not reference.startswith(
            RECOVERY_REFERENCE_PREFIX
        ):
            # Organic merchant traffic. Counted, never judged -- flagging it would
            # mark every ordinary sale as unauthorized.
            report.unrelated += 1
            continue
        settled.setdefault(reference, []).append(entry)

    for reference in sorted(set(authorized) | set(settled)):
        report.cases.append(
            _judge(
                reference,
                authorized.get(reference),
                reported.get(reference),
                settled.get(reference, []),
                currency=currency,
                have_source=not isinstance(source, EmptySettlementSource),
            )
        )

    return report


def _judge(
    reference: str,
    authorization: dict[str, Any] | None,
    report_leg: dict[str, Any] | None,
    entries: list[dict[str, Any]],
    *,
    currency: str,
    have_source: bool,
) -> SettlementCase:
    """Judge one reference across the three legs. The whole decision, in one place."""
    authorized_amount = authorization["authorized"] if authorization else None
    reported_amount = report_leg["reported"] if report_leg else None
    link_id = (authorization or {}).get("payment_link_id") or (report_leg or {}).get(
        "payment_link_id"
    )

    # Settled with no authorization. The finding this module exists to make: money
    # moved against a recovery reference this control plane never approved, and it
    # is detectable ONLY from a source we do not author.
    if entries and authorization is None:
        gross = _money(entries[0], "amount", currency)
        return SettlementCase(
            reference_id=reference,
            result=SettlementResult.MISMATCHED,
            reason=MismatchReason.UNAUTHORIZED_PAYMENT,
            payment_link_id=link_id,
            payment_id=entries[0].get("entity_id"),
            settled_gross=gross,
            fee=_money(entries[0], "fee", currency),
            tax=_money(entries[0], "tax", currency),
            net_credit=_money(entries[0], "credit", currency),
            reported=reported_amount,
            detail=(
                f"{gross} settled against reference {reference!r}, which this control "
                f"plane never authorized. Money moved without approval."
            ),
        )

    if not have_source:
        return SettlementCase(
            reference_id=reference,
            result=SettlementResult.UNVERIFIABLE,
            payment_link_id=link_id,
            authorized=authorized_amount,
            reported=reported_amount,
            detail="no settlement source available, so settlement cannot be checked",
        )

    if not entries:
        return SettlementCase(
            reference_id=reference,
            result=SettlementResult.PENDING,
            payment_link_id=link_id,
            authorized=authorized_amount,
            reported=reported_amount,
            detail=(
                "authorized, but no settlement entry yet -- settlement runs on a "
                "banking cycle, so this is expected shortly after a payment"
            ),
        )

    if len(entries) > 1:
        total = sum_money(
            [m for m in (_money(e, "amount", currency) for e in entries) if m is not None],
            currency=currency,
        )
        return SettlementCase(
            reference_id=reference,
            result=SettlementResult.MISMATCHED,
            reason=MismatchReason.DUPLICATE_CAPTURE,
            payment_link_id=link_id,
            authorized=authorized_amount,
            reported=reported_amount,
            settled_gross=total,
            detail=(
                f"{len(entries)} settlement entries for one authorization "
                f"({authorized_amount}); total settled {total}"
            ),
        )

    entry = entries[0]
    gross = _money(entry, "amount", currency)
    case = SettlementCase(
        reference_id=reference,
        result=SettlementResult.MATCHED,
        payment_link_id=link_id,
        payment_id=entry.get("entity_id"),
        authorized=authorized_amount,
        reported=reported_amount,
        settled_gross=gross,
        fee=_money(entry, "fee", currency),
        tax=_money(entry, "tax", currency),
        net_credit=_money(entry, "credit", currency),
    )

    if gross is None or authorized_amount is None:
        return _with(
            case,
            SettlementResult.PENDING,
            None,
            "settlement entry carries no comparable amount",
        )

    if gross != authorized_amount:
        return _with(
            case,
            SettlementResult.MISMATCHED,
            MismatchReason.AMOUNT_MISMATCH,
            f"authorized {authorized_amount} but {gross} settled "
            f"(difference {gross - authorized_amount})",
        )

    # Gross matches the authorization. If the webhook disagrees with BOTH, the
    # discrepancy is not attributable to fees and needs a human.
    if reported_amount is not None and reported_amount != gross:
        return _with(
            case,
            SettlementResult.MISMATCHED,
            MismatchReason.REPORTED_SETTLED_DIVERGENCE,
            f"webhook reported {reported_amount} but {gross} settled against an "
            f"authorization of {authorized_amount}",
        )

    detail = f"{gross} authorized, reported and settled"
    if case.fee_variance_only:
        # The expected case, not a finding. Net is below gross on every correct
        # Razorpay settlement because a fee and GST are deducted.
        detail += f"; net {case.net_credit} after {case.fee} fee and {case.tax} tax"
    return _with(case, SettlementResult.MATCHED, None, detail)


def _with(
    case: SettlementCase,
    result: SettlementResult,
    reason: MismatchReason | None,
    detail: str,
) -> SettlementCase:
    return SettlementCase(
        reference_id=case.reference_id,
        result=result,
        reason=reason,
        payment_link_id=case.payment_link_id,
        payment_id=case.payment_id,
        authorized=case.authorized,
        reported=case.reported,
        settled_gross=case.settled_gross,
        fee=case.fee,
        tax=case.tax,
        net_credit=case.net_credit,
        detail=detail,
    )
