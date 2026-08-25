"""Trivial import test per package (plan.md E0 (c)).

Each belay submodule must import cleanly with no side effects and no
missing dependencies. Real behavior tests arrive with each entrega.
"""

import importlib

import pytest

MODULES = [
    "belay",
    "belay.canonical",
    "belay.errors",
    "belay.contracts",
    "belay.contracts.model",
    "belay.contracts.loader",
    "belay.contracts.expressions",
    "belay.ledger",
    "belay.ledger.model",
    "belay.ledger.store",
    "belay.ledger.verify",
    "belay.ledger.redact",
    "belay.planner",
    "belay.planner.model",
    "belay.planner.planner",
    "belay.policy",
    "belay.policy.model",
    "belay.policy.engine",
    "belay.approvals",
    "belay.approvals.queue",
    "belay.executor",
    "belay.executor.saga",
    "belay.executor.idempotency",
    "belay.executor.recovery",
    "belay.rewind",
    "belay.rewind.service",
    "belay.proxy",
    "belay.proxy.server",
    "belay.proxy.upstream",
    "belay.proxy.lifecycle",
    "belay.cli",
    "belay.cli.main",
    "belay.db",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)


def test_error_codes_registry_has_seventeen_entries() -> None:
    """Spec §11's normative registry stays closed at exactly 17 codes.

    The financial control plane (ADR 0028) adds its own codes, but additively
    in `FINANCE_ERROR_CODES` -- so this assertion still means what it always
    meant, rather than being bumped every time a feature lands.
    """
    from belay.errors import SPEC_ERROR_CODES

    assert len(SPEC_ERROR_CODES) == 17


def test_error_codes_is_the_union_of_spec_and_finance_registries() -> None:
    """`ERROR_CODES` is what `BelayError` validates against: both registries."""
    from belay.errors import ERROR_CODES, FINANCE_ERROR_CODES, SPEC_ERROR_CODES

    assert ERROR_CODES == {**SPEC_ERROR_CODES, **FINANCE_ERROR_CODES}
    assert not set(SPEC_ERROR_CODES) & set(FINANCE_ERROR_CODES), (
        "a financial code must never shadow a normative spec code -- the union "
        "would silently take the finance registry's retryable flag"
    )


def test_every_financial_error_code_is_non_retryable() -> None:
    """An unauthorized action stays unauthorized however many times it is retried.

    Marking any of these retryable would invite an agent to spin against a
    limit instead of surfacing it to the merchant.
    """
    from belay.errors import FINANCE_ERROR_CODES

    assert not any(FINANCE_ERROR_CODES.values())
