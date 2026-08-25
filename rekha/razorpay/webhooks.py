"""Razorpay webhook ingestion: the first facts not derived from our own request.

Everything else in this system is something we did. A webhook is something that
*happened to us* -- a customer paid, a payment captured, a link expired -- and
that independence is what makes it usable as evidence. Settlement verification
(`rekha/settlement/`) needs a leg it did not author, and this is the first one.

Three responsibilities, and nothing else:

1. **Verify.** HMAC-SHA256 over the exact raw body, compared in constant time.
   An unverified payload is not evidence of anything and is refused outright.
2. **Deduplicate.** Razorpay retries delivery. A replayed webhook must not be
   able to make a recovery look like it happened twice.
3. **Record.** Append a normalized fact to the same hash-chained ledger the
   authorizations live in, so a later reconciliation reads one source.

Deliberately NOT done here:

- **Correlation.** Nothing in this module decides that a paid link corresponds to
  a recovery we requested. That is a fold over the ledger
  (`correlate_recoveries`), kept separate because ingestion must work even for a
  webhook about something we never did -- which is precisely the case settlement
  verification exists to catch.
- **An HTTP endpoint.** There is no server here. A public endpoint means a tunnel,
  a hostname, and a demo that fails when the network does. `rekha webhooks replay`
  feeds saved payloads through the real verifier instead: identical code path,
  identical evidence, no infrastructure.

On hand-rolling the HMAC rather than using the official `razorpay` SDK: the SDK's
`verify_webhook_signature` is five lines of `hmac` plus a constant-time compare,
and pulling in the SDK (and its `requests` tree) for that is a poor trade. The
real risk in hand-rolling is comparing with `==` instead of `hmac.compare_digest`,
which is a timing oracle -- so that is done explicitly below, and tested.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from typing import Any

from rekha.errors import RekhaError
from rekha.finance.money import Money
from rekha.ledger.model import Event
from rekha.ledger.store import LedgerStore

#: Ledger event type for a verified, deduplicated webhook. One type rather than
#: one per Razorpay event: the Razorpay event name rides in the payload, so
#: adding support for a new event never requires a new event type (and spec §9.1's
#: normative list stays untouched).
WEBHOOK_RECEIVED = "webhook_received"

#: Razorpay events this module understands. Anything else verifies and is
#: recorded, but contributes no financial fact -- unknown is not the same as
#: invalid, and silently dropping a signed event would lose evidence.
KNOWN_EVENTS: frozenset[str] = frozenset(
    {
        "payment_link.paid",
        "payment_link.partially_paid",
        "payment_link.expired",
        "payment_link.cancelled",
        "payment.captured",
        "payment.failed",
        "payment.authorized",
        "refund.processed",
    }
)

#: Events that mean money arrived.
PAYMENT_EVENTS: frozenset[str] = frozenset(
    {"payment_link.paid", "payment_link.partially_paid", "payment.captured"}
)


def sign_payload(body: str | bytes, secret: str) -> str:
    """HMAC-SHA256 of `body` under `secret`, hex-encoded.

    This is what Razorpay does to produce `X-Razorpay-Signature`. Provided here so
    the sandbox and the demo can stand in for Razorpay explicitly -- the verifier
    below neither knows nor cares who signed, so the verification path exercised
    offline is the same one a real webhook takes.
    """
    raw = body.encode("utf-8") if isinstance(body, str) else body
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def verify_signature(body: str | bytes, signature: str, secret: str) -> bool:
    """Whether `signature` is a valid HMAC-SHA256 of `body` under `secret`.

    Compared with `hmac.compare_digest`, not `==`. A byte-by-byte comparison
    returns faster on an early mismatch, which leaks how much of a forged
    signature was correct and makes it guessable one character at a time. Never
    raises: any malformed input is simply not a valid signature.
    """
    if not signature or not secret:
        return False
    try:
        expected = sign_payload(body, secret)
    except Exception:  # pragma: no cover - defensive
        return False
    return hmac.compare_digest(expected, signature.strip())


@dataclass(frozen=True)
class WebhookFact:
    """The normalized financial content of one webhook.

    Amounts are `Money` from the moment they enter the system, so a webhook's
    figure can be compared with an authorization's without a conversion step where
    a units mistake could hide.
    """

    event: str
    event_id: str
    #: The payment link this concerns, if any. The handle that ties a webhook back
    #: to a recovery we requested.
    payment_link_id: str | None = None
    payment_id: str | None = None
    reference_id: str | None = None
    amount: Money | None = None
    amount_paid: Money | None = None
    status: str | None = None
    created_at: int | None = None

    @property
    def is_payment(self) -> bool:
        return self.event in PAYMENT_EVENTS

    def to_payload(self) -> dict[str, Any]:
        """Ledger payload. `Money` serializes as `{minor_units, currency}`."""
        payload: dict[str, Any] = {
            "event": self.event,
            "event_id": self.event_id,
            "is_payment": self.is_payment,
        }
        for name, value in (
            ("payment_link_id", self.payment_link_id),
            ("payment_id", self.payment_id),
            ("reference_id", self.reference_id),
            ("status", self.status),
            ("created_at", self.created_at),
        ):
            if value is not None:
                payload[name] = value
        if self.amount is not None:
            payload["amount"] = self.amount.model_dump(mode="json")
        if self.amount_paid is not None:
            payload["amount_paid"] = self.amount_paid.model_dump(mode="json")
        return payload


def _money(entity: dict[str, Any], key: str = "amount") -> Money | None:
    raw = entity.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    currency = entity.get("currency")
    return Money(minor_units=raw, currency=str(currency) if currency else "INR")


def parse_fact(body: str | bytes, *, event_id: str = "") -> WebhookFact:
    """Extract the financial content of a webhook body.

    `event_id` normally comes from the `X-Razorpay-Event-Id` header. When absent,
    a digest of the body is used instead: dedupe must work even for a delivery
    that arrived without the header, and a body-derived id is stable for the same
    reason a hash is.
    """
    raw = body.encode("utf-8") if isinstance(body, str) else body
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RekhaError(
            "webhook_signature_invalid",
            {"reason": f"body is not JSON: {exc}", "detail": raw[:120].decode(errors="replace")},
        ) from exc
    if not isinstance(parsed, dict):
        raise RekhaError(
            "webhook_signature_invalid", {"reason": "webhook body is not a JSON object"}
        )

    event = str(parsed.get("event", "")).strip()
    if not event:
        raise RekhaError(
            "webhook_signature_invalid", {"reason": "webhook body has no `event` field"}
        )

    resolved_id = event_id.strip() or hashlib.sha256(raw).hexdigest()[:32]
    payload = parsed.get("payload")
    entities = payload if isinstance(payload, dict) else {}

    link = entities.get("payment_link", {})
    link_entity = link.get("entity", {}) if isinstance(link, dict) else {}
    payment = entities.get("payment", {})
    payment_entity = payment.get("entity", {}) if isinstance(payment, dict) else {}
    if not isinstance(link_entity, dict):
        link_entity = {}
    if not isinstance(payment_entity, dict):
        payment_entity = {}

    # A link event carries the authorized amount on the link; a bare payment event
    # carries it on the payment. Prefer the link, since that is what we authorized.
    amount = _money(link_entity) or _money(payment_entity)
    amount_paid = _money(link_entity, "amount_paid")
    if amount_paid is None and payment_entity.get("status") == "captured":
        # A captured payment with no link context: the amount paid IS the amount.
        amount_paid = _money(payment_entity)

    return WebhookFact(
        event=event,
        event_id=resolved_id,
        payment_link_id=(link_entity.get("id") or None),
        payment_id=(payment_entity.get("id") or None),
        reference_id=(link_entity.get("reference_id") or None),
        amount=amount,
        amount_paid=amount_paid,
        status=(link_entity.get("status") or payment_entity.get("status") or None),
        created_at=(
            parsed.get("created_at") if isinstance(parsed.get("created_at"), int) else None
        ),
    )


@dataclass
class IngestResult:
    """Outcome of ingesting one webhook. Explicit about every non-happy path."""

    accepted: bool
    fact: WebhookFact | None = None
    #: True when this exact event was already recorded. Not an error: Razorpay
    #: retries delivery, and a duplicate is expected traffic.
    duplicate: bool = False
    #: True when the event verified but is not one we model. Recorded anyway.
    unknown_event: bool = False
    reason: str = ""


def ingested_event_ids(ledger: LedgerStore, session_id: str | None = None) -> set[str]:
    """Event ids already recorded, for deduplication.

    Read from the ledger rather than kept in memory: dedupe must survive a
    process restart, or a retry after a crash would double-count a recovery. Same
    reasoning as the durable idempotency the executor uses for outbound calls.
    """
    events = ledger.read(session_id) if session_id else ledger.read_by_types([WEBHOOK_RECEIVED])
    return {
        str(event.payload.get("event_id"))
        for event in events
        if event.type == WEBHOOK_RECEIVED and event.payload.get("event_id")
    }


def ingest(
    ledger: LedgerStore,
    session_id: str,
    body: str | bytes,
    signature: str,
    secret: str,
    *,
    event_id: str = "",
) -> IngestResult:
    """Verify, deduplicate, and record one webhook.

    Order matters and is deliberate: verify first, so an unsigned payload never
    reaches the parser or the ledger; parse second; dedupe third, against the
    durable ledger. An unverified webhook is refused with
    `webhook_signature_invalid` and appends nothing -- recording an unverified
    claim would put a fact in the evidence chain that nothing vouches for.
    """
    if not verify_signature(body, signature, secret):
        raise RekhaError(
            "webhook_signature_invalid",
            {
                "reason": "HMAC-SHA256 signature does not match the payload; this "
                "webhook is not evidence of anything and was not recorded",
                "event_id": event_id or None,
            },
        )

    fact = parse_fact(body, event_id=event_id)

    if fact.event_id in ingested_event_ids(ledger):
        # Expected traffic, not an error. Razorpay retries, and the whole point of
        # deduplicating is that a retry changes nothing.
        return IngestResult(
            accepted=False,
            fact=fact,
            duplicate=True,
            reason=f"event {fact.event_id} was already ingested",
        )

    unknown = fact.event not in KNOWN_EVENTS
    payload = fact.to_payload()
    if unknown:
        # Recorded, but flagged. Unknown is not invalid: it verified, so it is
        # genuine evidence, and dropping it would lose a signed fact.
        payload["unknown_event"] = True
    ledger.append(session_id, WEBHOOK_RECEIVED, payload)

    return IngestResult(accepted=True, fact=fact, unknown_event=unknown)


# --------------------------------------------------------------- correlation


@dataclass(frozen=True)
class Recovery:
    """One recovery attempt matched against what a webhook says happened."""

    payment_link_id: str
    reference_id: str | None
    #: What the control plane authorized and executed.
    authorized: Money
    #: What webhooks say the customer actually paid. Zero until one arrives.
    paid: Money
    events: tuple[str, ...] = field(default=())

    @property
    def is_recovered(self) -> bool:
        return bool(self.paid) and not self.paid.is_negative

    @property
    def is_fully_recovered(self) -> bool:
        return self.is_recovered and self.paid >= self.authorized


def correlate_recoveries(events: list[Event], *, currency: str = "INR") -> list[Recovery]:
    """Match executed recovery actions against the webhooks that followed.

    A pure fold, deliberately separate from ingestion. Two reasons: a webhook must
    be recordable even when it concerns something we never did (the case
    settlement verification exists to catch), and correlation is a *claim* about
    two independent records agreeing -- which should be recomputable from evidence
    rather than asserted at write time.

    Links our side to Razorpay's by `payment_link_id`, which appears in the
    `result_recorded` event of the action that created it and in every webhook
    about it.
    """
    authorized: dict[str, Money] = {}
    reference_of: dict[str, str | None] = {}
    paid: dict[str, Money] = {}
    seen_events: dict[str, list[str]] = {}

    # Our side: link ids we actually created, with the amount we asked for.
    for event in events:
        if event.type != "result_recorded":
            continue
        result = event.payload.get("result")
        if not isinstance(result, dict):
            continue
        link_id = result.get("id")
        raw_amount = result.get("amount")
        if not isinstance(link_id, str) or not link_id.startswith("plink_"):
            continue
        if isinstance(raw_amount, int) and not isinstance(raw_amount, bool):
            authorized[link_id] = Money(
                minor_units=raw_amount, currency=str(result.get("currency") or currency)
            )
            reference_of[link_id] = result.get("reference_id") or None

    # Razorpay's side: what the webhooks report.
    for event in events:
        if event.type != WEBHOOK_RECEIVED:
            continue
        link_id = event.payload.get("payment_link_id")
        if not isinstance(link_id, str):
            continue
        seen_events.setdefault(link_id, []).append(str(event.payload.get("event")))
        if not event.payload.get("is_payment"):
            continue
        raw = event.payload.get("amount_paid") or event.payload.get("amount")
        if not isinstance(raw, dict):
            continue
        minor, cur = raw.get("minor_units"), raw.get("currency")
        if isinstance(minor, bool) or not isinstance(minor, int) or not isinstance(cur, str):
            continue
        amount = Money(minor_units=minor, currency=cur)
        # Highest reported paid amount wins rather than a sum: `payment_link.paid`
        # restates the cumulative total, so adding a partial payment and the final
        # total would double-count the partial.
        existing = paid.get(link_id)
        if existing is None or amount > existing:
            paid[link_id] = amount

    return [
        Recovery(
            payment_link_id=link_id,
            reference_id=reference_of.get(link_id),
            authorized=amount,
            paid=paid.get(link_id, Money.zero(amount.currency)),
            events=tuple(seen_events.get(link_id, ())),
        )
        for link_id, amount in sorted(authorized.items())
    ]
