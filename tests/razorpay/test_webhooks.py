"""Webhook ingestion: verification, deduplication, and correlation.

A webhook is the first fact in this system not derived from our own request, which
is what makes it usable as evidence. So the tests concentrate on the properties
that make it *trustworthy* evidence: a forged signature is refused, a retry
changes nothing, and an unverified payload never reaches the ledger.

The correlation tests matter for a different reason. "Measured money recovered" is
a claim about two independent records agreeing, and it must be computed from
evidence rather than asserted at write time -- so `correlate_recoveries` is a pure
fold, and these tests pin what it does when the two records disagree.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from belay.errors import BelayError
from belay.finance.money import Money
from belay.ledger.store import LedgerStore
from belay.ledger.verify import verify_chain, verify_coherence
from belay.razorpay.webhooks import (
    WEBHOOK_RECEIVED,
    correlate_recoveries,
    ingest,
    ingested_event_ids,
    parse_fact,
    sign_payload,
    verify_signature,
)

SECRET = "whsec_test_secret"
INR = "INR"


def _link_paid_body(
    link_id: str = "plink_abc",
    *,
    amount: int = 240000,
    paid: int | None = None,
    reference: str = "recover-pay_1",
    event: str = "payment_link.paid",
    status: str = "paid",
) -> str:
    return json.dumps(
        {
            "entity": "event",
            "event": event,
            "created_at": 1788000600,
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": link_id,
                        "status": status,
                        "amount": amount,
                        "amount_paid": amount if paid is None else paid,
                        "currency": INR,
                        "reference_id": reference,
                    }
                },
                "payment": {
                    "entity": {
                        "id": f"pay_for_{link_id}",
                        "status": "captured",
                        "amount": amount if paid is None else paid,
                        "currency": INR,
                    }
                },
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )


# ------------------------------------------------------------------ signatures


def test_a_valid_signature_verifies() -> None:
    body = _link_paid_body()
    assert verify_signature(body, sign_payload(body, SECRET), SECRET)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b + " ",                       # trailing whitespace
        lambda b: b.replace("240000", "999999"),  # amount tampered
        lambda b: b.replace("plink_abc", "plink_xyz"),  # different link
        lambda b: b[:-1],                        # truncated
    ],
)
def test_any_change_to_the_body_invalidates_the_signature(mutate: Any) -> None:
    """HMAC is over exact bytes -- which is why the raw body is stored verbatim
    rather than re-serialized from a parsed object."""
    body = _link_paid_body()
    signature = sign_payload(body, SECRET)
    assert not verify_signature(mutate(body), signature, SECRET)


def test_the_wrong_secret_does_not_verify() -> None:
    body = _link_paid_body()
    assert not verify_signature(body, sign_payload(body, SECRET), "different-secret")


@pytest.mark.parametrize("signature", ["", "   ", "not-hex", "0" * 64])
def test_a_malformed_signature_is_simply_invalid(signature: str) -> None:
    """Never raises: a garbage signature is not an error condition, it is a
    failed verification."""
    assert not verify_signature(_link_paid_body(), signature, SECRET)


def test_an_empty_secret_never_verifies() -> None:
    """A missing secret must fail closed, not accept everything."""
    body = _link_paid_body()
    assert not verify_signature(body, sign_payload(body, ""), "")


def test_comparison_is_constant_time() -> None:
    """Pinned because `==` here is a timing oracle: it returns faster on an early
    mismatch, which leaks how much of a forged signature was correct and makes it
    guessable one character at a time."""
    import inspect

    from belay.razorpay import webhooks

    source = inspect.getsource(webhooks.verify_signature)
    assert "compare_digest" in source


# ---------------------------------------------------------------------- parsing


def test_a_paid_link_parses_into_a_financial_fact() -> None:
    fact = parse_fact(_link_paid_body(), event_id="evt_1")
    assert fact.event == "payment_link.paid"
    assert fact.event_id == "evt_1"
    assert fact.payment_link_id == "plink_abc"
    assert fact.reference_id == "recover-pay_1"
    assert fact.amount == Money(minor_units=240000, currency=INR)
    assert fact.amount_paid == Money(minor_units=240000, currency=INR)
    assert fact.is_payment


def test_a_partial_payment_records_less_than_the_authorized_amount() -> None:
    fact = parse_fact(
        _link_paid_body(amount=240000, paid=100000, event="payment_link.partially_paid"),
        event_id="evt_partial",
    )
    assert fact.amount == Money(minor_units=240000, currency=INR)
    assert fact.amount_paid == Money(minor_units=100000, currency=INR)
    assert fact.is_payment


def test_an_expiry_is_not_a_payment() -> None:
    fact = parse_fact(
        _link_paid_body(event="payment_link.expired", status="expired", paid=0),
        event_id="evt_exp",
    )
    assert not fact.is_payment
    assert fact.status == "expired"


def test_a_missing_event_id_falls_back_to_a_body_digest() -> None:
    """Dedupe must work even for a delivery that arrived without the header."""
    body = _link_paid_body()
    first = parse_fact(body)
    second = parse_fact(body)
    assert first.event_id == second.event_id
    assert first.event_id != parse_fact(_link_paid_body(link_id="plink_other")).event_id


@pytest.mark.parametrize("bad", ["not json", "[]", '"a string"', "{}", '{"event": ""}'])
def test_a_body_without_an_event_is_refused(bad: str) -> None:
    with pytest.raises(BelayError) as excinfo:
        parse_fact(bad)
    assert excinfo.value.code == "webhook_signature_invalid"


# -------------------------------------------------------------------- ingestion


def test_an_unverified_webhook_appends_nothing() -> None:
    """Recording an unverified claim would put a fact in the evidence chain that
    nothing vouches for."""
    ledger = LedgerStore()
    with pytest.raises(BelayError) as excinfo:
        ingest(ledger, "s", _link_paid_body(), "forged", SECRET, event_id="evt_1")
    assert excinfo.value.code == "webhook_signature_invalid"
    assert ledger.read("s") == []


def test_a_verified_webhook_is_recorded() -> None:
    ledger = LedgerStore()
    body = _link_paid_body()
    result = ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id="evt_1")

    assert result.accepted
    assert not result.duplicate
    events = [e for e in ledger.read("s") if e.type == WEBHOOK_RECEIVED]
    assert len(events) == 1
    assert events[0].payload["event"] == "payment_link.paid"
    assert events[0].payload["amount_paid"] == {"minor_units": 240000, "currency": INR}


def test_a_retried_delivery_changes_nothing() -> None:
    """Razorpay retries. A duplicate is expected traffic, not an error -- and it
    must not be able to make one recovery look like two."""
    ledger = LedgerStore()
    body = _link_paid_body()
    signature = sign_payload(body, SECRET)

    first = ingest(ledger, "s", body, signature, SECRET, event_id="evt_1")
    second = ingest(ledger, "s", body, signature, SECRET, event_id="evt_1")
    third = ingest(ledger, "s", body, signature, SECRET, event_id="evt_1")

    assert first.accepted
    assert second.duplicate and not second.accepted
    assert third.duplicate
    assert len([e for e in ledger.read("s") if e.type == WEBHOOK_RECEIVED]) == 1


def test_deduplication_survives_a_restart() -> None:
    """Read from the ledger, not memory: a retry after a crash must not
    double-count. Same reasoning as the executor's durable idempotency."""
    first_ledger = LedgerStore()
    body = _link_paid_body()
    signature = sign_payload(body, SECRET)
    ingest(first_ledger, "s", body, signature, SECRET, event_id="evt_1")

    # A fresh store over the same engine, as a restarted process would see.
    restarted = LedgerStore(engine=first_ledger.engine)
    result = ingest(restarted, "s", body, signature, SECRET, event_id="evt_1")
    assert result.duplicate


def test_two_different_events_are_both_recorded() -> None:
    ledger = LedgerStore()
    for index in range(2):
        body = _link_paid_body(link_id=f"plink_{index}")
        ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id=f"evt_{index}")
    assert len(ingested_event_ids(ledger, "s")) == 2


def test_an_unknown_event_is_recorded_but_flagged() -> None:
    """Unknown is not invalid. It verified, so it is genuine evidence, and
    dropping a signed fact would lose it."""
    ledger = LedgerStore()
    body = json.dumps({"event": "subscription.charged", "payload": {}})
    result = ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id="evt_u")

    assert result.accepted
    assert result.unknown_event
    events = [e for e in ledger.read("s") if e.type == WEBHOOK_RECEIVED]
    assert events[0].payload["unknown_event"] is True


def test_the_chain_still_verifies_after_ingestion() -> None:
    ledger = LedgerStore()
    for index in range(3):
        body = _link_paid_body(link_id=f"plink_{index}")
        ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id=f"evt_{index}")

    events = ledger.read("s")
    assert verify_chain(events).ok
    assert verify_coherence(events).ok


# ------------------------------------------------------------------ correlation


def _executed_link(
    ledger: LedgerStore, link_id: str, amount: int, reference: str = "recover-pay_1"
) -> None:
    """Record the evidence a governed link creation leaves behind."""
    ledger.append(
        "s",
        "result_recorded",
        {
            "tool": "create_payment_link_upi",
            "result": {
                "id": link_id,
                "status": "created",
                "amount": amount,
                "currency": INR,
                "reference_id": reference,
            },
        },
        step_seq=1,
    )


def test_a_paid_link_is_correlated_as_recovered() -> None:
    ledger = LedgerStore()
    _executed_link(ledger, "plink_abc", 240000)
    body = _link_paid_body("plink_abc", amount=240000)
    ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id="evt_1")

    recoveries = correlate_recoveries(ledger.read("s"))
    assert len(recoveries) == 1
    assert recoveries[0].authorized == Money(minor_units=240000, currency=INR)
    assert recoveries[0].paid == Money(minor_units=240000, currency=INR)
    assert recoveries[0].is_recovered
    assert recoveries[0].is_fully_recovered


def test_an_unpaid_link_is_correlated_as_awaiting() -> None:
    """A created link is not recovered money. Money is recovered when a customer
    pays, which is a webhook fact -- so this must read zero."""
    ledger = LedgerStore()
    _executed_link(ledger, "plink_abc", 240000)

    recoveries = correlate_recoveries(ledger.read("s"))
    assert recoveries[0].paid == Money.zero(INR)
    assert not recoveries[0].is_recovered


def test_a_partial_payment_is_recovered_but_not_fully() -> None:
    ledger = LedgerStore()
    _executed_link(ledger, "plink_abc", 240000)
    body = _link_paid_body(
        "plink_abc", amount=240000, paid=100000, event="payment_link.partially_paid"
    )
    ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id="evt_1")

    recovery = correlate_recoveries(ledger.read("s"))[0]
    assert recovery.is_recovered
    assert not recovery.is_fully_recovered
    assert recovery.paid == Money(minor_units=100000, currency=INR)


def test_a_partial_then_full_payment_is_not_double_counted() -> None:
    """`payment_link.paid` restates the cumulative total, so summing a partial and
    the final total would report more recovered than the link was even worth."""
    ledger = LedgerStore()
    _executed_link(ledger, "plink_abc", 240000)

    partial = _link_paid_body(
        "plink_abc", amount=240000, paid=100000, event="payment_link.partially_paid"
    )
    ingest(ledger, "s", partial, sign_payload(partial, SECRET), SECRET, event_id="evt_p")
    full = _link_paid_body("plink_abc", amount=240000, paid=240000)
    ingest(ledger, "s", full, sign_payload(full, SECRET), SECRET, event_id="evt_f")

    recovery = correlate_recoveries(ledger.read("s"))[0]
    assert recovery.paid == Money(minor_units=240000, currency=INR), (
        f"expected the cumulative total, got {recovery.paid} -- partial and full "
        f"were summed"
    )


def test_a_webhook_for_a_link_we_never_created_is_not_a_recovery() -> None:
    """Ingestion must accept it -- that is exactly the case settlement
    verification exists to catch -- but it correlates to nothing of ours."""
    ledger = LedgerStore()
    _executed_link(ledger, "plink_ours", 100000)
    body = _link_paid_body("plink_not_ours", amount=5000000)
    result = ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id="evt_x")

    assert result.accepted
    recoveries = correlate_recoveries(ledger.read("s"))
    assert [r.payment_link_id for r in recoveries] == ["plink_ours"]
    assert recoveries[0].paid == Money.zero(INR)


def test_correlation_is_a_pure_deterministic_fold() -> None:
    """"Measured money recovered" is a claim about two independent records
    agreeing, so it must be recomputable from evidence rather than asserted."""
    ledger = LedgerStore()
    _executed_link(ledger, "plink_abc", 240000)
    body = _link_paid_body("plink_abc")
    ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id="evt_1")

    events = ledger.read("s")
    assert correlate_recoveries(events) == correlate_recoveries(events)


def test_an_expiry_does_not_count_as_payment() -> None:
    ledger = LedgerStore()
    _executed_link(ledger, "plink_abc", 240000)
    body = _link_paid_body("plink_abc", event="payment_link.expired", status="expired", paid=0)
    ingest(ledger, "s", body, sign_payload(body, SECRET), SECRET, event_id="evt_e")

    recovery = correlate_recoveries(ledger.read("s"))[0]
    assert not recovery.is_recovered
    assert "payment_link.expired" in recovery.events
