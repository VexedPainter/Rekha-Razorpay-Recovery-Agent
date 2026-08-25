"""Three-way settlement verification.

This is the only check in the system that does not trust our own records, so the
tests are about what it refuses to conclude as much as what it detects:

- a missing SETTLED leg must report `unverifiable`, never `matched` -- absence of
  evidence is not agreement
- normal fee and GST deduction must NOT be a mismatch, or the verifier produces a
  false positive on literally every correct payment and its real findings get
  ignored
- organic merchant traffic must not be judged at all, or every ordinary sale gets
  flagged as unauthorized

And the finding the module exists for: money settled against a recovery reference
this control plane never approved.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from rekha.finance.money import Money
from rekha.ledger.store import LedgerStore
from rekha.razorpay.webhooks import ingest, sign_payload
from rekha.settlement.verify import (
    EmptySettlementSource,
    FixtureSettlementSource,
    MismatchReason,
    SettlementResult,
    verify,
)

SECRET = "whsec_test"
INR = "INR"


def _ledger_with_recovery(
    *,
    reference: str = "recover-pay_1",
    link_id: str = "plink_abc",
    authorized: int = 240000,
    paid: int | None = None,
    payment_id: str = "pay_paid1",
) -> LedgerStore:
    """A ledger holding a governed recovery, and optionally the webhook that followed."""
    ledger = LedgerStore()
    ledger.append("s", "session_started", {"merchant_id": "acme_retail"}, initiated_by="agent")
    ledger.append(
        "s",
        "result_recorded",
        {
            "tool": "create_payment_link_upi",
            "result": {
                "id": link_id,
                "status": "created",
                "amount": authorized,
                "currency": INR,
                "reference_id": reference,
            },
        },
        step_seq=1,
    )
    if paid is not None:
        body = json.dumps(
            {
                "entity": "event",
                "event": "payment_link.paid",
                "created_at": 1788000600,
                "payload": {
                    "payment_link": {
                        "entity": {
                            "id": link_id,
                            "status": "paid",
                            "amount": authorized,
                            "amount_paid": paid,
                            "currency": INR,
                            "reference_id": reference,
                        }
                    },
                    "payment": {
                        "entity": {
                            "id": payment_id,
                            "status": "captured",
                            "amount": paid,
                            "currency": INR,
                        }
                    },
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id="evt_1")
    return ledger


def _recon(
    *,
    entity_id: str = "pay_paid1",
    amount: int = 240000,
    fee: int = 5664,
    tax: int = 1019,
    reference: str | None = None,
) -> dict[str, Any]:
    """A settlement reconciliation entry, in Razorpay's shape."""
    entry: dict[str, Any] = {
        "entity_id": entity_id,
        "type": "payment",
        "debit": 0,
        "credit": amount - fee - tax,
        "amount": amount,
        "currency": INR,
        "fee": fee,
        "tax": tax,
        "on_hold": False,
        "settled": True,
        "settlement_id": "setl_1",
    }
    if reference:
        entry["reference_id"] = reference
    return entry


# --------------------------------------------------------------- what it refuses


def test_no_settlement_source_is_unverifiable_not_matched() -> None:
    """The verdict that stops a missing leg reading as a clean bill of health."""
    report = verify(_ledger_with_recovery(paid=240000).read("s"), EmptySettlementSource())

    assert report.verdict is SettlementResult.UNVERIFIABLE
    assert len(report.unverifiable) == 1
    assert report.matched == []
    assert "no settlement source" in report.unverifiable[0].detail


def test_an_authorized_payment_with_no_settlement_yet_is_pending() -> None:
    """Not-yet-settled is not settled correctly. Settlement runs on a banking
    cycle, so this is the normal state shortly after a payment."""
    report = verify(_ledger_with_recovery(paid=240000).read("s"), FixtureSettlementSource([]))

    assert report.verdict is SettlementResult.PENDING
    assert len(report.pending) == 1
    assert report.matched == []


def test_normal_fees_are_not_a_mismatch() -> None:
    """Razorpay deducts a fee and GST from every settlement, so net is below gross
    on every CORRECT transaction. Reporting that would be a false positive on
    literally every payment, and a verifier that cries wolf on normal traffic gets
    its real findings ignored."""
    report = verify(
        _ledger_with_recovery(paid=240000).read("s"),
        FixtureSettlementSource([_recon(amount=240000, fee=5664, tax=1019)]),
    )

    assert report.verdict is SettlementResult.MATCHED
    case = report.matched[0]
    assert case.reason is None
    assert case.fee_variance_only
    assert case.settled_gross == Money(minor_units=240000, currency=INR)
    assert case.net_credit == Money(minor_units=240000 - 5664 - 1019, currency=INR)
    assert report.fees_total == Money(minor_units=5664 + 1019, currency=INR)


def test_organic_merchant_traffic_is_counted_but_never_judged() -> None:
    """A merchant's settlement report contains every payment they took. Flagging
    entries that are not ours would mark every ordinary sale as unauthorized --
    alarming, wrong, and fatal to the credibility of every other verdict."""
    report = verify(
        _ledger_with_recovery(paid=240000).read("s"),
        FixtureSettlementSource(
            [
                _recon(amount=240000),
                {
                    "entity_id": "pay_organic_1",
                    "amount": 999999,
                    "currency": INR,
                    "type": "payment",
                },
                {
                    "entity_id": "pay_organic_2",
                    "amount": 500000,
                    "currency": INR,
                    "type": "payment",
                },
            ]
        ),
    )

    assert report.unrelated == 2
    assert report.verdict is SettlementResult.MATCHED
    assert len(report.cases) == 1


# ------------------------------------------------------------------ what it finds


def test_money_settled_against_an_unauthorized_reference_is_detected() -> None:
    """The finding this module exists for.

    Detectable ONLY from a record we do not author: our own ledger has no entry,
    our own webhooks have no event, and yet money moved. Comparing authorized
    against reported would never surface it, because both descend from our request.
    """
    report = verify(
        _ledger_with_recovery(paid=240000).read("s"),
        FixtureSettlementSource(
            [
                _recon(amount=240000),
                _recon(
                    entity_id="pay_ghost",
                    amount=5000000,
                    fee=0,
                    tax=0,
                    reference="recover-pay_NEVER_AUTHORIZED",
                ),
            ]
        ),
    )

    assert report.verdict is SettlementResult.MISMATCHED
    assert report.mismatches_by_reason() == {"unauthorized_payment": 1}
    case = report.mismatched[0]
    assert case.reason is MismatchReason.UNAUTHORIZED_PAYMENT
    assert case.authorized is None
    assert case.settled_gross == Money(minor_units=5000000, currency=INR)
    assert "never authorized" in case.detail


def test_a_settled_amount_differing_from_the_authorization_is_detected() -> None:
    report = verify(
        _ledger_with_recovery(authorized=240000, paid=240000).read("s"),
        FixtureSettlementSource([_recon(amount=390000)]),
    )

    assert report.verdict is SettlementResult.MISMATCHED
    case = report.mismatched[0]
    assert case.reason is MismatchReason.AMOUNT_MISMATCH
    assert "authorized INR 2,400.00" in case.detail
    assert "INR 3,900.00 settled" in case.detail


def test_one_authorization_settled_twice_is_detected() -> None:
    report = verify(
        _ledger_with_recovery(paid=240000).read("s"),
        FixtureSettlementSource([_recon(amount=240000), _recon(amount=240000)]),
    )

    assert report.verdict is SettlementResult.MISMATCHED
    case = report.mismatched[0]
    assert case.reason is MismatchReason.DUPLICATE_CAPTURE
    assert case.settled_gross == Money(minor_units=480000, currency=INR)
    assert "2 settlement entries" in case.detail


def test_a_webhook_disagreeing_with_both_other_legs_is_detected() -> None:
    """Gross matches the authorization but the webhook says something else, so the
    discrepancy is not attributable to fees and needs a human."""
    report = verify(
        _ledger_with_recovery(authorized=240000, paid=100000).read("s"),
        FixtureSettlementSource([_recon(amount=240000)]),
    )

    assert report.verdict is SettlementResult.MISMATCHED
    assert report.mismatched[0].reason is MismatchReason.REPORTED_SETTLED_DIVERGENCE


# ------------------------------------------------------------------- properties


def test_one_mismatch_makes_the_session_mismatched() -> None:
    """Most-severe-wins, like `PolicyEngine`. A single unreconciled rupee is the
    finding, not the average."""
    ledger = _ledger_with_recovery(paid=240000)
    report = verify(
        ledger.read("s"),
        FixtureSettlementSource(
            [
                _recon(amount=240000),
                _recon(entity_id="pay_x", amount=1, reference="recover-pay_GHOST"),
            ]
        ),
    )
    assert len(report.matched) == 1
    assert len(report.mismatched) == 1
    assert report.verdict is SettlementResult.MISMATCHED


def test_verification_is_a_pure_deterministic_fold() -> None:
    """Purity is what lets a verdict be recomputed from evidence without our
    database, our credentials, or our word."""
    events = _ledger_with_recovery(paid=240000).read("s")
    source = FixtureSettlementSource([_recon(amount=240000)])
    first, second = verify(events, source), verify(events, source)

    assert first.verdict is second.verdict
    assert [c.result for c in first.cases] == [c.result for c in second.cases]
    assert first.settled_total == second.settled_total


def test_the_three_way_join_needs_the_webhook_to_bridge_ids() -> None:
    """Our ledger knows link ids; settlement is itemised by payment id. The webhook
    is the only record carrying both, so there is no shortcut that reads two legs.

    Without the webhook, an entry keyed only by payment id cannot be attributed --
    and it must then be treated as unrelated rather than guessed at.
    """
    without_webhook = _ledger_with_recovery(paid=None)
    report = verify(
        without_webhook.read("s"),
        FixtureSettlementSource([_recon(entity_id="pay_paid1", amount=240000)]),
    )
    assert report.unrelated == 1
    assert report.verdict is SettlementResult.PENDING

    with_webhook = _ledger_with_recovery(paid=240000)
    bridged = verify(
        with_webhook.read("s"),
        FixtureSettlementSource([_recon(entity_id="pay_paid1", amount=240000)]),
    )
    assert bridged.unrelated == 0
    assert bridged.verdict is SettlementResult.MATCHED


def test_totals_are_exact_integers() -> None:
    report = verify(
        _ledger_with_recovery(authorized=240010, paid=240010).read("s"),
        FixtureSettlementSource([_recon(amount=240010, fee=5664, tax=1019)]),
    )
    assert report.authorized_total == Money(minor_units=240010, currency=INR)
    assert report.settled_total == Money(minor_units=240010, currency=INR)


def test_an_empty_session_verifies_vacuously() -> None:
    ledger = LedgerStore()
    ledger.append("s", "session_started", {"merchant_id": "m"}, initiated_by="a")
    report = verify(ledger.read("s"), FixtureSettlementSource([]))
    assert report.cases == []
    assert report.verdict is SettlementResult.MATCHED


def test_the_source_name_is_always_reported() -> None:
    """Which leg ran must never be implied. A fixture-backed verification and a
    live one are different claims."""
    events = _ledger_with_recovery(paid=240000).read("s")
    assert verify(events, FixtureSettlementSource([])).source == "fixture"
    assert verify(events, EmptySettlementSource()).source == "none"


def test_the_live_source_refuses_a_non_test_key() -> None:
    from rekha.settlement.live import LiveSettlementSource

    with pytest.raises(ValueError, match="test-mode only"):
        LiveSettlementSource("rzp_live_realkey", "secret", year=2026, month=8)
