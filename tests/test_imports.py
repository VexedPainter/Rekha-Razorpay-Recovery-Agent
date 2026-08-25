"""Trivial import test per package (plan.md E0 (c)).

Each rekha submodule must import cleanly with no side effects and no
missing dependencies. Real behavior tests arrive with each entrega.
"""

import importlib

import pytest

MODULES = [
    "rekha",
    "rekha.canonical",
    "rekha.errors",
    "rekha.contracts",
    "rekha.contracts.model",
    "rekha.contracts.loader",
    "rekha.contracts.expressions",
    "rekha.ledger",
    "rekha.ledger.model",
    "rekha.ledger.store",
    "rekha.ledger.verify",
    "rekha.ledger.redact",
    "rekha.planner",
    "rekha.planner.model",
    "rekha.planner.planner",
    "rekha.policy",
    "rekha.policy.model",
    "rekha.policy.engine",
    "rekha.approvals",
    "rekha.approvals.queue",
    "rekha.executor",
    "rekha.executor.saga",
    "rekha.executor.idempotency",
    "rekha.executor.recovery",
    "rekha.rewind",
    "rekha.rewind.service",
    "rekha.proxy",
    "rekha.proxy.server",
    "rekha.proxy.upstream",
    "rekha.proxy.lifecycle",
    "rekha.cli",
    "rekha.cli.main",
    "rekha.db",
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
    from rekha.errors import SPEC_ERROR_CODES

    assert len(SPEC_ERROR_CODES) == 17


def test_error_codes_is_the_union_of_spec_and_finance_registries() -> None:
    """`ERROR_CODES` is what `RekhaError` validates against: both registries."""
    from rekha.errors import ERROR_CODES, FINANCE_ERROR_CODES, SPEC_ERROR_CODES

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
    from rekha.errors import FINANCE_ERROR_CODES

    assert not any(FINANCE_ERROR_CODES.values())
