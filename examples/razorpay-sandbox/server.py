"""A local MCP server mimicking Razorpay's tool surface, for offline runs.

Why this exists, given the official `razorpay/razorpay-mcp-server` is right
there and MIT-licensed: the demo must not depend on the network, on
credentials, or on a Docker daemon. `examples/crm-mock/server.py` set exactly
this precedent -- the saga executor's acceptance tests run against a real stdio
MCP server rather than a mock, so the transport, the contract resolution, and
the ledger are all genuinely exercised while the upstream stays deterministic.

What this is:
  - a real MCP server (FastMCP, stdio), spoken to over real MCP by real client
    code -- not a Python fake injected in place of the transport
  - tool names, argument names and response shapes matching the official
    Razorpay MCP server and the underlying REST API, so the contract pack in
    `packs/razorpay/` is valid against both. `tests/razorpay/test_razorpay_pack.py`
    pins that correspondence.
  - deterministic: a fixed-seed cohort (`cohort.py`), a fixed clock

What this is NOT:
  - a Razorpay emulator. It implements only the tools the recovery workflow
    uses, and only the fields those tools' contracts reference.
  - the thing that proves Razorpay integration works. That is `--live` against
    the real server (Phase 6). This proves the *control plane* works, which is
    a different and larger claim.

Deliberate behaviours that make the demo honest:
  - `create_payment_link` is idempotent on `reference_id`, because the real API
    is. Calling it twice with the same reference returns the first link rather
    than creating a second, which is what makes the duplicate-execution
    scenario a real test rather than a staged one.
  - a created link starts `created`, and `simulate_payment` moves it to `paid`.
    Nothing marks itself paid, so "recovered" is always a fact from outside our
    own request.
  - `fetch_settlement_recon_details` returns entries only for payments that
    actually settled, so the settlement-verification tests have a source that
    can legitimately disagree with our ledger.
"""

from __future__ import annotations

import hashlib
import random
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

sys.path.insert(0, str(Path(__file__).parent))
from cohort import DEFAULT_SEED, generate_cohort

mcp = FastMCP("razorpay-sandbox")

_NOW = 1788000000

# ---------------------------------------------------------------- sandbox state

_payments: dict[str, dict[str, Any]] = {}
_payment_links: dict[str, dict[str, Any]] = {}
_links_by_reference: dict[str, str] = {}
_refunds: dict[str, dict[str, Any]] = {}
_settlements: dict[str, dict[str, Any]] = {}
_recon_entries: list[dict[str, Any]] = []
_rng = random.Random(DEFAULT_SEED)


def _reset(count: int = 200) -> None:
    """(Re)seed the sandbox. Called at import and by `sandbox_reset`."""
    global _rng
    _rng = random.Random(DEFAULT_SEED)
    _payments.clear()
    _payment_links.clear()
    _links_by_reference.clear()
    _refunds.clear()
    _settlements.clear()
    _recon_entries.clear()
    for payment in generate_cohort(count, seed=DEFAULT_SEED):
        _payments[payment["id"]] = payment


def _suffix(prefix: str, material: str) -> str:
    """Deterministic id from `material`, so a replayed call yields the same id."""
    digest = hashlib.sha256(f"{prefix}:{material}".encode()).hexdigest()
    return f"{prefix}_{digest[:14]}"


def _collection(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Razorpay's list envelope: `{entity, count, items}`."""
    return {"entity": "collection", "count": len(items), "items": items}


def _public(payment: dict[str, Any]) -> dict[str, Any]:
    """A payment as Razorpay would return it, with ground truth removed.

    The cohort carries a `_truth` key holding the real cause class and recovery
    probability, used only by the evaluation harness. Stripping it here -- at the
    single boundary every read tool goes through -- means it cannot reach the agent
    even by accident, however the payment is fetched.

    If it leaked, every accuracy number in the project would be circular: the model
    would be reading the answer instead of inferring it.
    """
    return {key: value for key, value in payment.items() if key != "_truth"}


# ------------------------------------------------------------------- read tools


@mcp.tool(name="fetch_all_payments", annotations=ToolAnnotations(readOnlyHint=True))
def fetch_all_payments(
    status: str | None = None, count: int = 100, skip: int = 0
) -> dict[str, Any]:
    """List payments, newest first. `status` filters (e.g. "failed")."""
    items = sorted(_payments.values(), key=lambda p: p["created_at"], reverse=True)
    if status:
        items = [p for p in items if p["status"] == status]
    return _collection([_public(p) for p in items[skip : skip + count]])


@mcp.tool(name="fetch_payment", annotations=ToolAnnotations(readOnlyHint=True))
def fetch_payment(payment_id: str) -> dict[str, Any]:
    """Fetch one payment by id, including its full error detail."""
    payment = _payments.get(payment_id)
    if payment is None:
        return {"error": {"code": "BAD_REQUEST_ERROR", "description": "payment not found"}}
    return _public(payment)


@mcp.tool(name="fetch_order_payments", annotations=ToolAnnotations(readOnlyHint=True))
def fetch_order_payments(order_id: str) -> dict[str, Any]:
    """Every payment attempt against one order -- the customer's retry history."""
    return _collection(
        [_public(p) for p in _payments.values() if p.get("order_id") == order_id]
    )


@mcp.tool(name="fetch_tokens", annotations=ToolAnnotations(readOnlyHint=True))
def fetch_tokens(customer_id: str = "", contact: str = "") -> dict[str, Any]:
    """Saved payment instruments. Informs whether an alternate method exists.

    Deterministic per contact: the same customer always has the same saved
    methods, so a strategy decision is reproducible.
    """
    if not contact:
        return _collection([])
    seed = int(hashlib.sha256(contact.encode()).hexdigest()[:8], 16)
    local = random.Random(seed)
    if local.random() < 0.45:
        return _collection([])
    return _collection(
        [
            {
                "id": _suffix("token", contact),
                "entity": "token",
                "method": local.choice(["card", "upi"]),
                "used_at": _NOW - local.randrange(86400, 86400 * 90),
                "expired_at": _NOW + 86400 * 365,
            }
        ]
    )


@mcp.tool(name="fetch_payment_link", annotations=ToolAnnotations(readOnlyHint=True))
def fetch_payment_link(payment_link_id: str) -> dict[str, Any]:
    """Fetch a payment link, including whether it has been paid."""
    link = _payment_links.get(payment_link_id)
    if link is None:
        return {"error": {"code": "BAD_REQUEST_ERROR", "description": "link not found"}}
    return dict(link)


@mcp.tool(name="fetch_all_payment_links", annotations=ToolAnnotations(readOnlyHint=True))
def fetch_all_payment_links() -> dict[str, Any]:
    return _collection([dict(link) for link in _payment_links.values()])


@mcp.tool(name="fetch_all_settlements", annotations=ToolAnnotations(readOnlyHint=True))
def fetch_all_settlements(count: int = 10, skip: int = 0) -> dict[str, Any]:
    """Settlements: money actually moved to the merchant's bank account."""
    items = sorted(_settlements.values(), key=lambda s: s["created_at"], reverse=True)
    return _collection([_public(p) for p in items[skip : skip + count]])


@mcp.tool(name="fetch_settlement_recon_details", annotations=ToolAnnotations(readOnlyHint=True))
def fetch_settlement_recon_details(year: int = 2026, month: int = 8) -> dict[str, Any]:
    """Itemised reconciliation: per-transaction settled amount, fee and tax.

    This is leg 3 of `belay/settlement/verify.py` -- the only source that is not
    derived from our own request. Returns entries only for transactions that
    genuinely settled.
    """
    return _collection([dict(entry) for entry in _recon_entries])


# ------------------------------------------------------------------ write tools


def _create_link(
    *,
    amount: int,
    currency: str,
    description: str,
    reference_id: str,
    customer: dict[str, Any] | None,
    upi_only: bool,
) -> dict[str, Any]:
    """Shared by both link-creation tools. Idempotent on `reference_id`.

    The real API rejects a duplicate `reference_id`; returning the existing link
    is the behaviour that makes Belay's idempotency guarantee observable end to
    end, rather than something only the ledger knows about.
    """
    if reference_id and reference_id in _links_by_reference:
        return dict(_payment_links[_links_by_reference[reference_id]])

    link_id = _suffix("plink", reference_id or f"{amount}:{description}")
    link = {
        "id": link_id,
        "entity": "payment_link",
        "amount": int(amount),
        "amount_paid": 0,
        "currency": currency,
        "status": "created",
        "description": description,
        "reference_id": reference_id,
        "short_url": f"https://rzp.io/i/{link_id[-10:]}",
        "customer": customer or {},
        "notify": {"sms": True, "email": True},
        "reminder_enable": True,
        "upi_link": upi_only,
        "accept_partial": False,
        "created_at": _NOW,
        "expire_by": _NOW + 86400 * 3,
        "payments": [],
        "notes": {},
    }
    _payment_links[link_id] = link
    if reference_id:
        _links_by_reference[reference_id] = link_id
    return dict(link)


@mcp.tool(name="create_payment_link")
def create_payment_link(
    amount: int,
    currency: str = "INR",
    description: str = "",
    reference_id: str = "",
    customer: dict[str, Any] | None = None,
    method: str = "",
) -> dict[str, Any]:
    """Create a standard payment link. `amount` is in the currency's minor units.

    `method` is accepted so the mandate's `allowed_methods` can be enforced
    against a declared instrument; the real standard link lets the customer
    choose, so it is advisory here and not echoed into the link.
    """
    return _create_link(
        amount=amount,
        currency=currency,
        description=description,
        reference_id=reference_id,
        customer=customer,
        upi_only=False,
    )


@mcp.tool(name="create_payment_link_upi")
def create_payment_link_upi(
    amount: int,
    currency: str = "INR",
    description: str = "",
    reference_id: str = "",
    customer: dict[str, Any] | None = None,
    method: str = "upi",
) -> dict[str, Any]:
    """Create a UPI-only payment link."""
    return _create_link(
        amount=amount,
        currency=currency,
        description=description,
        reference_id=reference_id,
        customer=customer,
        upi_only=True,
    )


@mcp.tool(name="send_payment_link")
def send_payment_link(payment_link_id: str, medium: str = "sms") -> dict[str, Any]:
    """Send an existing link to the customer over SMS or email."""
    link = _payment_links.get(payment_link_id)
    if link is None:
        return {"error": {"code": "BAD_REQUEST_ERROR", "description": "link not found"}}
    sent = link.setdefault("_sent", [])
    sent.append(medium)
    return {"id": payment_link_id, "medium": medium, "status": "sent"}


@mcp.tool(name="cancel_payment_link")
def cancel_payment_link(payment_link_id: str) -> dict[str, Any]:
    """Cancel an unpaid link. This is the `undo` for link creation.

    A paid link cannot be cancelled -- which is precisely why the contract
    declares link creation `conditional` rather than `reversible`, and why the
    condition is evaluated at commit time.
    """
    link = _payment_links.get(payment_link_id)
    if link is None:
        return {"error": {"code": "BAD_REQUEST_ERROR", "description": "link not found"}}
    if link["status"] == "paid":
        return {
            "error": {
                "code": "BAD_REQUEST_ERROR",
                "description": "a paid payment link cannot be cancelled",
            }
        }
    link["status"] = "cancelled"
    return dict(link)


@mcp.tool(name="create_refund")
def create_refund(
    payment_id: str, amount: int | None = None, speed: str = "normal"
) -> dict[str, Any]:
    """Refund a captured payment. Irreversible -- a refund cannot be un-refunded.

    Present in the sandbox on purpose. The prompt-injection scenario is only
    meaningful if this tool genuinely exists and genuinely works: what stops it
    is the merchant's mandate, not the absence of the capability.
    """
    refund_id = _suffix("rfnd", f"{payment_id}:{amount}")
    refund = {
        "id": refund_id,
        "entity": "refund",
        "amount": amount,
        "currency": "INR",
        "payment_id": payment_id,
        "status": "processed",
        "speed_processed": speed,
        "created_at": _NOW,
    }
    _refunds[refund_id] = refund
    return dict(refund)


# --------------------------------------------------- sandbox-only test controls
# Named `sandbox_*` so they can never be confused with a real Razorpay tool,
# and deliberately absent from packs/razorpay/contracts.yaml -- which means the
# governed proxy refuses them with `contract_missing` (spec §4.6 default-deny).
# The demo drives them out-of-band, as the outside world, never as the agent.


@mcp.tool(name="sandbox_simulate_payment")
def sandbox_simulate_payment(payment_link_id: str) -> dict[str, Any]:
    """The customer pays a link. Produces a captured payment and a webhook body.

    Separate from link creation so "recovered" is always an event from outside
    our own request, never an assumption baked into the action that requested it.
    """
    link = _payment_links.get(payment_link_id)
    if link is None:
        return {"error": {"code": "BAD_REQUEST_ERROR", "description": "link not found"}}
    if link["status"] == "paid":
        return {"already_paid": True, "payment_link": dict(link)}

    payment_id = _suffix("pay", f"paid:{payment_link_id}")
    amount = int(link["amount"])
    fee = max(int(amount * 0.0236), 100)  # ~2% + GST, the real order of magnitude
    tax = max(int(fee * 0.18), 18)

    payment = {
        "id": payment_id,
        "entity": "payment",
        "amount": amount,
        "currency": link["currency"],
        "status": "captured",
        "order_id": None,
        "method": "upi" if link.get("upi_link") else "card",
        "captured": True,
        "description": link.get("description", ""),
        "email": (link.get("customer") or {}).get("email"),
        "contact": (link.get("customer") or {}).get("contact"),
        "notes": {"payment_link_id": payment_link_id},
        "fee": fee,
        "tax": tax,
        "error_code": None,
        "error_description": None,
        "created_at": _NOW + 600,
    }
    _payments[payment_id] = payment
    link["status"] = "paid"
    link["amount_paid"] = amount
    link["payments"] = [{"payment_id": payment_id, "amount": amount, "status": "captured"}]

    return {
        "payment": dict(payment),
        "payment_link": dict(link),
        "webhook": {
            "entity": "event",
            "event": "payment_link.paid",
            "contains": ["payment_link", "payment"],
            "created_at": _NOW + 600,
            "payload": {
                "payment_link": {"entity": dict(link)},
                "payment": {"entity": dict(payment)},
            },
        },
    }


@mcp.tool(name="sandbox_settle")
def sandbox_settle(payment_ids: list[str] | None = None) -> dict[str, Any]:
    """Settle captured payments into a settlement plus itemised recon entries.

    Accepts explicit `payment_ids` so a test can settle an amount that does NOT
    match what was authorized, or settle a payment the ledger never authorized
    at all. Both are required to prove the settlement verifier detects a
    mismatch rather than merely agreeing with itself.
    """
    candidates = (
        [_payments[p] for p in payment_ids if p in _payments]
        if payment_ids
        else [p for p in _payments.values() if p["status"] == "captured"]
    )
    if not candidates:
        return {"settled": 0, "settlement": None}

    gross = sum(int(p["amount"]) for p in candidates)
    fees = sum(int(p.get("fee") or 0) for p in candidates)
    tax = sum(int(p.get("tax") or 0) for p in candidates)
    settlement_id = _suffix("setl", ",".join(sorted(p["id"] for p in candidates)))
    settlement = {
        "id": settlement_id,
        "entity": "settlement",
        "amount": gross - fees - tax,
        "status": "processed",
        "fees": fees,
        "tax": tax,
        "utr": settlement_id[-12:],
        "created_at": _NOW + 86400,
    }
    _settlements[settlement_id] = settlement

    for payment in candidates:
        fee = int(payment.get("fee") or 0)
        payment_tax = int(payment.get("tax") or 0)
        _recon_entries.append(
            {
                "entity_id": payment["id"],
                "type": "payment",
                "debit": 0,
                "credit": int(payment["amount"]) - fee - payment_tax,
                "amount": int(payment["amount"]),
                "currency": payment["currency"],
                "fee": fee,
                "tax": payment_tax,
                "on_hold": False,
                "settled": True,
                "settled_at": _NOW + 86400,
                "settlement_id": settlement_id,
                "method": payment.get("method"),
                "description": payment.get("description", ""),
                "created_at": payment["created_at"],
            }
        )

    return {"settled": len(candidates), "settlement": settlement}


@mcp.tool(name="sandbox_inject_unauthorized_settlement")
def sandbox_inject_unauthorized_settlement(
    amount: int = 5000000, currency: str = "INR"
) -> dict[str, Any]:
    """Money that moved with no authorizing entry in our ledger.

    The scenario settlement verification exists for. Detecting this is the only
    check in the system that does not trust our own records, so it needs a way
    to be genuinely provoked.
    """
    entity_id = _suffix("pay", f"unauthorized:{amount}:{len(_recon_entries)}")
    settlement_id = _suffix("setl", f"unauthorized:{entity_id}")
    _settlements[settlement_id] = {
        "id": settlement_id,
        "entity": "settlement",
        "amount": amount,
        "status": "processed",
        "fees": 0,
        "tax": 0,
        "utr": settlement_id[-12:],
        "created_at": _NOW + 86400,
    }
    entry = {
        "entity_id": entity_id,
        "type": "payment",
        "debit": 0,
        "credit": amount,
        "amount": amount,
        "currency": currency,
        "fee": 0,
        "tax": 0,
        "on_hold": False,
        "settled": True,
        "settled_at": _NOW + 86400,
        "settlement_id": settlement_id,
        "method": "card",
        "description": "unreconciled movement",
        "created_at": _NOW + 3600,
    }
    _recon_entries.append(entry)
    return {"injected": entry}


@mcp.tool(name="sandbox_reset")
def sandbox_reset(count: int = 200) -> dict[str, Any]:
    """Reseed the sandbox to a known state, so a demo run is reproducible."""
    _reset(count)
    return {
        "reset": True,
        "failed_payments": sum(1 for p in _payments.values() if p["status"] == "failed"),
        "revenue_at_risk_paise": sum(
            int(p["amount"]) for p in _payments.values() if p["status"] == "failed"
        ),
    }


_reset()


if __name__ == "__main__":
    mcp.run()
