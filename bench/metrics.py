"""Metrics, folded from the hash-chained ledger.

Every number here is recomputed from evidence rather than accumulated in a
counter, which means the measurements are exactly as verifiable as the actions they
measure. A reviewer who distrusts the report can recompute it from the same ledger,
or check the chain first and then recompute.

The distinction the whole module is built around: **requested is not recovered.**
A created payment link is something we did. Recovered money is what a customer
paid, taken from a webhook. Reports that blur those two are how a recovery rate
gets quietly inflated, so they are named and presented separately here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rekha.finance.money import Money
from rekha.finance.money import total as sum_money
from rekha.ledger.model import Event
from rekha.ledger.store import LedgerStore
from rekha.ledger.verify import verify_chain, verify_coherence
from rekha.razorpay.webhooks import WEBHOOK_RECEIVED, correlate_recoveries


@dataclass
class Metrics:
    """One session, measured."""

    session_id: str
    currency: str = "INR"

    # Revenue
    revenue_at_risk: Money = field(default_factory=lambda: Money.zero("INR"))
    payments_considered: int = 0
    links_created: int = 0
    amount_requested: Money = field(default_factory=lambda: Money.zero("INR"))
    amount_recovered: Money = field(default_factory=lambda: Money.zero("INR"))
    links_recovered: int = 0

    # Escalation
    paused_for_approval: int = 0
    approvals_granted: int = 0

    # Safety
    refusals_by_code: dict[str, int] = field(default_factory=dict)
    upstream_calls: int = 0
    steps_committed: int = 0

    # Verification
    webhooks_accepted: int = 0

    # Engineering
    total_events: int = 0
    chain_ok: bool = False
    coherence_ok: bool = False

    @property
    def recovery_rate(self) -> float:
        """Recovered as a fraction of revenue at risk. The headline number."""
        if not self.revenue_at_risk:
            return 0.0
        return self.amount_recovered.minor_units / self.revenue_at_risk.minor_units

    @property
    def conversion_rate(self) -> float:
        """Recovered as a fraction of what was actually requested.

        A more honest measure of the *agent's* effectiveness than recovery rate,
        which is dominated by how much of the cohort the mandate allowed it to
        pursue at all.
        """
        if not self.amount_requested:
            return 0.0
        return self.amount_recovered.minor_units / self.amount_requested.minor_units


def measure(db: str, session: str | None = None) -> Metrics:
    """Fold one session's ledger into metrics."""
    ledger = LedgerStore(f"sqlite:///{db}")
    target = session or _latest_session(ledger)
    events = ledger.read(target) if target else []
    return measure_events(target or "", events)


def measure_events(session_id: str, events: list[Event]) -> Metrics:
    """The pure fold. No I/O, so a metric can be recomputed from an exported bundle."""
    metrics = Metrics(session_id=session_id)
    metrics.total_events = len(events)
    if not events:
        return metrics

    for event in events:
        if event.type == "result_recorded":
            result = event.payload.get("result")
            if isinstance(result, dict):
                # The cohort read: how much revenue was at risk.
                items = result.get("items")
                if isinstance(items, list) and items:
                    amounts = [
                        Money(
                            minor_units=item["amount"],
                            currency=str(item.get("currency") or metrics.currency),
                        )
                        for item in items
                        if isinstance(item, dict)
                        and isinstance(item.get("amount"), int)
                        and not isinstance(item.get("amount"), bool)
                    ]
                    if amounts:
                        metrics.payments_considered = len(amounts)
                        metrics.revenue_at_risk = sum_money(
                            amounts, currency=metrics.currency
                        )
        elif event.type == "approval_requested":
            metrics.paused_for_approval += 1
        elif event.type == "approval_resolved":
            if event.payload.get("state") == "approved":
                metrics.approvals_granted += 1
        elif event.type == "tool_called":
            metrics.upstream_calls += 1
        elif event.type == "step_committed":
            metrics.steps_committed += 1
        elif event.type == WEBHOOK_RECEIVED:
            metrics.webhooks_accepted += 1
        elif event.type == "step_failed":
            error = event.payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if isinstance(code, str):
                metrics.refusals_by_code[code] = metrics.refusals_by_code.get(code, 0) + 1

    # Requested vs recovered, from the same correlation the verifier uses.
    recoveries = correlate_recoveries(events, currency=metrics.currency)
    metrics.links_created = len(recoveries)
    metrics.amount_requested = sum_money(
        [r.authorized for r in recoveries], currency=metrics.currency
    )
    metrics.amount_recovered = sum_money(
        [r.paid for r in recoveries], currency=metrics.currency
    )
    metrics.links_recovered = sum(1 for r in recoveries if r.is_recovered)

    metrics.chain_ok = verify_chain(events).ok
    metrics.coherence_ok = verify_coherence(events).ok
    return metrics


def render(metrics: Metrics) -> str:
    """A report a human reads. Requested and recovered are never blurred."""
    lines = [
        "MEASURED BATCH",
        f"  session                  : {metrics.session_id}",
        "",
        "REVENUE",
        f"  failed payments in batch : {metrics.payments_considered}",
        f"  revenue at risk          : {metrics.revenue_at_risk}",
        f"  recovery links created   : {metrics.links_created}",
        f"  amount requested         : {metrics.amount_requested}",
        f"  amount RECOVERED         : {metrics.amount_recovered}   "
        f"({metrics.links_recovered} link(s) paid)",
        f"  recovery rate            : {metrics.recovery_rate:.1%} of revenue at risk",
        f"  conversion rate          : {metrics.conversion_rate:.1%} of what was requested",
        "",
        "  Requested is not recovered. The recovered figure comes from webhooks --",
        "  what customers actually paid -- not from what the agent attempted.",
        "",
        "ESCALATION",
        f"  paused for a human       : {metrics.paused_for_approval}",
        f"  approvals granted        : {metrics.approvals_granted}",
        "",
        "SAFETY",
        f"  actions refused          : {sum(metrics.refusals_by_code.values())}",
    ]
    for code, count in sorted(metrics.refusals_by_code.items()):
        lines.append(f"    {code}: {count}")
    lines += [
        f"  upstream calls made      : {metrics.upstream_calls}",
        f"  steps committed          : {metrics.steps_committed}",
        "",
        "VERIFICATION",
        f"  webhooks accepted        : {metrics.webhooks_accepted}",
        "",
        "EVIDENCE",
        f"  ledger events            : {metrics.total_events}",
        f"  hash chain               : {'OK' if metrics.chain_ok else 'BROKEN'}",
        f"  step coherence           : {'OK' if metrics.coherence_ok else 'BROKEN'}",
        "",
        "  Every number above is a fold over the hash-chained ledger, so the",
        "  measurements are as verifiable as the actions they measure.",
    ]
    return "\n".join(lines)


def _latest_session(ledger: LedgerStore) -> str:
    sessions = [e.session_id for e in ledger.read_by_types(["session_started"])]
    return sessions[-1] if sessions else ""
