"""The adversarial suite and the metrics fold.

The suite itself is the interesting test subject: if a scenario stops detecting
what it claims to detect, the build must fail rather than the report quietly
changing. So this asserts each scenario names the RIGHT layer, not merely that
something was blocked.

Marked `slow`: the suite spawns a real MCP server subprocess.
"""

from __future__ import annotations

import pytest
from bench.metrics import measure_events, render
from rekha.finance.money import Money
from rekha.ledger.store import LedgerStore


@pytest.mark.slow
@pytest.mark.anyio
async def test_every_adversarial_scenario_is_blocked_by_the_right_layer() -> None:
    """Six attacks, each refused by a specific named control.

    A test asserting only "it was blocked" cannot distinguish a designed control
    from a coincidence, so the layer is part of the assertion.
    """
    from bench.attacks import run_all

    report = await run_all()
    by_name = {r.name: r for r in report.results}

    expected_layers = {
        "prompt injection": "MANDATE",
        "amount escalation": "PLAN BINDING",
        "approval reuse": "CAPABILITY LEASE",
        "duplicate execution": "IDEMPOTENCY",
        "cumulative fan-out": "CUMULATIVE CEILING",
        "settlement mismatch": "SETTLEMENT VERIFICATION",
    }

    for name, layer in expected_layers.items():
        result = by_name[name]
        assert result.blocked, f"{name} ESCAPED -- {result.layer}"
        assert layer in result.layer, (
            f"{name} was blocked by {result.layer!r}, expected {layer!r} -- a control "
            f"other than the intended one fired, so the scenario is no longer "
            f"testing what it claims"
        )
        assert result.evidence, f"{name} left no evidence"


@pytest.mark.slow
@pytest.mark.anyio
async def test_the_benign_cohort_is_not_blocked() -> None:
    """Without this, a block rate is uninterpretable: a system that refuses
    everything scores 100%."""
    from bench.attacks import run_all

    report = await run_all()
    benign = next(r for r in report.results if not r.expected_blocked)

    assert not benign.blocked, f"legitimate traffic was refused: {benign.detail}"
    assert report.false_positive_rate == 0.0
    assert report.block_rate == 1.0
    assert report.passed == len(report.results)


@pytest.mark.slow
@pytest.mark.anyio
async def test_the_report_distinguishes_adversarial_from_benign() -> None:
    """Mixing them would make both rates meaningless."""
    from bench.attacks import run_all

    report = await run_all()
    assert len(report.adversarial) == 6
    assert len(report.benign) == 1
    adversarial_names = {r.name for r in report.adversarial}
    benign_names = {r.name for r in report.benign}
    assert adversarial_names.isdisjoint(benign_names)
    assert len(adversarial_names | benign_names) == len(report.results)


# ------------------------------------------------------------------------ metrics


def _session_with_recovery(
    *, authorized: int = 240000, paid: int | None = None
) -> LedgerStore:
    from rekha.razorpay.webhooks import ingest, sign_payload

    ledger = LedgerStore()
    ledger.append("s", "session_started", {"merchant_id": "acme"}, initiated_by="agent")
    ledger.append(
        "s",
        "result_recorded",
        {
            "tool": "fetch_all_payments",
            "result": {
                "items": [
                    {"amount": 100000, "currency": "INR"},
                    {"amount": 200000, "currency": "INR"},
                ]
            },
        },
        step_seq=1,
    )
    ledger.append("s", "tool_called", {"tool": "create_payment_link_upi"}, step_seq=2)
    # A committed step needs its journal, result and registered compensation, or
    # `verify_coherence` rightly refuses to call the evidence coherent. Writing a
    # partial step here would make the fixture, not the code, the thing under test.
    ledger.append(
        "s", "step_journaled", {"tool": "create_payment_link_upi"}, step_seq=2
    )
    ledger.append(
        "s",
        "result_recorded",
        {
            "tool": "create_payment_link_upi",
            "result": {
                "id": "plink_m",
                "amount": authorized,
                "currency": "INR",
                "reference_id": "recover-pay_1",
            },
        },
        step_seq=2,
    )
    ledger.append(
        "s",
        "compensation_registered",
        {"reversible": True, "tool": "cancel_payment_link"},
        step_seq=2,
    )
    ledger.append("s", "step_committed", {"tool": "create_payment_link_upi"}, step_seq=2)

    if paid is not None:
        import json

        body = json.dumps(
            {
                "event": "payment_link.paid",
                "payload": {
                    "payment_link": {
                        "entity": {
                            "id": "plink_m",
                            "amount": authorized,
                            "amount_paid": paid,
                            "currency": "INR",
                            "reference_id": "recover-pay_1",
                        }
                    },
                    "payment": {"entity": {"id": "pay_m", "status": "captured"}},
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        ingest(ledger, "s", body, sign_payload(body, "sec"), "sec", event_id="evt_m")
    return ledger


def test_metrics_never_conflate_requested_with_recovered() -> None:
    """The distinction the whole module is built around. Blurring them is how a
    recovery rate gets quietly inflated."""
    unpaid = measure_events("s", _session_with_recovery(paid=None).read("s"))
    assert unpaid.amount_requested == Money(minor_units=240000, currency="INR")
    assert unpaid.amount_recovered == Money.zero("INR")
    assert unpaid.recovery_rate == 0.0
    assert unpaid.links_recovered == 0

    paid = measure_events("s", _session_with_recovery(paid=240000).read("s"))
    assert paid.amount_recovered == Money(minor_units=240000, currency="INR")
    assert paid.links_recovered == 1


def test_revenue_at_risk_comes_from_the_cohort_read() -> None:
    metrics = measure_events("s", _session_with_recovery(paid=240000).read("s"))
    assert metrics.payments_considered == 2
    assert metrics.revenue_at_risk == Money(minor_units=300000, currency="INR")


def test_conversion_rate_measures_the_agent_not_the_mandate() -> None:
    """Recovery rate is dominated by how much of the cohort the mandate allowed the
    agent to pursue at all; conversion rate is the agent's own effectiveness."""
    metrics = measure_events("s", _session_with_recovery(authorized=240000, paid=120000).read("s"))
    assert metrics.conversion_rate == pytest.approx(0.5)
    assert metrics.recovery_rate == pytest.approx(120000 / 300000)


def test_metrics_report_chain_integrity() -> None:
    metrics = measure_events("s", _session_with_recovery(paid=240000).read("s"))
    assert metrics.chain_ok
    assert metrics.coherence_ok
    assert metrics.total_events > 0


def test_metrics_are_a_deterministic_fold() -> None:
    events = _session_with_recovery(paid=240000).read("s")
    assert render(measure_events("s", events)) == render(measure_events("s", events))


def test_an_empty_session_measures_to_zero_not_an_error() -> None:
    metrics = measure_events("empty", [])
    assert metrics.revenue_at_risk == Money.zero("INR")
    assert metrics.recovery_rate == 0.0
    assert metrics.conversion_rate == 0.0
    assert "MEASURED BATCH" in render(metrics)


def test_refusals_are_counted_by_code() -> None:
    """A run that reports "3 refused" says nothing; the codes name the controls."""
    ledger = LedgerStore()
    ledger.append("s", "session_started", {"merchant_id": "m"}, initiated_by="a")
    for code in ("mandate_violation", "cumulative_limit_exceeded", "mandate_violation"):
        ledger.append("s", "step_failed", {"error": {"code": code}}, step_seq=1)

    metrics = measure_events("s", ledger.read("s"))
    assert metrics.refusals_by_code == {
        "mandate_violation": 2,
        "cumulative_limit_exceeded": 1,
    }
