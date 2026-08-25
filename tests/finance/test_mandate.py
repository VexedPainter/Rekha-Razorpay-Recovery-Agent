"""`belay/finance/mandate.py` -- the merchant's grant of authority.

The most important test in this file is
`test_a_refund_is_refused_even_though_the_tool_exists`: it is the mechanism
that makes the prompt-injection demo meaningful. The agent *can* form the
intent to issue a refund, and the tool *is* in the contract catalog. The
mandate is what stops it.
"""

from __future__ import annotations

import pytest
import yaml
from belay.errors import BelayError
from belay.finance.mandate import (
    MerchantMandate,
    check_mandate,
    load_mandate,
)
from belay.finance.money import Money


def _mandate(**overrides: object) -> MerchantMandate:
    base: dict[str, object] = {
        "merchant_id": "acme_retail",
        "purpose": "Recover recoverable failed checkout payments.",
        "currency": "INR",
        "allowed_actions": ["create_payment_link", "create_payment_link_upi", "send_payment_link"],
        "forbidden_actions": ["create_refund"],
        "allowed_methods": ["upi", "card", "netbanking"],
        "max_per_action": Money.from_major("5000.00", "INR"),
        "max_cumulative": Money.from_major("50000.00", "INR"),
        "window": "1d",
        "approval_threshold": Money.from_major("2500.00", "INR"),
    }
    base.update(overrides)
    return MerchantMandate.model_validate(base)


# ------------------------------------------------------------------- the point


def test_a_permitted_action_within_limits_passes() -> None:
    assert (
        check_mandate(
            _mandate(),
            "create_payment_link",
            amount=Money.from_major("2400.00", "INR"),
            method="upi",
        )
        is None
    )


def test_a_refund_is_refused_even_though_the_tool_exists() -> None:
    """The prompt-injection defence, stated as a unit test.

    `create_refund` is a real, contracted, working tool. An injected
    instruction can make the AI propose it. The mandate is the layer that
    refuses, and it names the clause that did so.
    """
    violation = check_mandate(
        _mandate(), "create_refund", amount=Money.from_major("50000.00", "INR")
    )
    assert violation is not None
    assert violation.field == "forbidden_actions"
    assert "create_refund" in violation.reason


def test_an_action_absent_from_the_allow_list_is_refused() -> None:
    """Default-deny: not being forbidden is not the same as being permitted."""
    violation = check_mandate(_mandate(), "create_instant_settlement")
    assert violation is not None
    assert violation.field == "allowed_actions"


def test_an_explicit_forbid_outranks_the_allow_list_message() -> None:
    """Most-specific refusal wins, so the reported reason is the informative one."""
    mandate = _mandate(allowed_actions=[], forbidden_actions=["create_refund"])
    violation = check_mandate(mandate, "create_refund")
    assert violation is not None
    assert violation.field == "forbidden_actions"


def test_an_empty_allow_list_permits_anything_not_forbidden() -> None:
    """An empty `allowed_actions` is 'unrestricted', matching IntentContract's
    `allowed_scope` semantics -- a mandate that lists nothing is not a mandate
    that permits nothing, or every field would have to be filled in to do
    anything at all."""
    mandate = _mandate(allowed_actions=[], forbidden_actions=[])
    assert check_mandate(mandate, "anything_at_all") is None


# ------------------------------------------------------------------- amount


def test_over_the_per_action_ceiling_is_refused() -> None:
    violation = check_mandate(
        _mandate(), "create_payment_link", amount=Money.from_major("5000.01", "INR")
    )
    assert violation is not None
    assert violation.field == "max_per_action"
    assert "5,000.00" in violation.reason


def test_exactly_at_the_per_action_ceiling_is_permitted() -> None:
    """The ceiling binds strictly above itself. A merchant who authorizes
    Rs 5,000 per action must be able to spend Rs 5,000."""
    assert (
        check_mandate(
            _mandate(), "create_payment_link", amount=Money.from_major("5000.00", "INR")
        )
        is None
    )


def test_a_foreign_currency_is_refused() -> None:
    """Closes the cap-evasion route `PolicyEngine` cannot close on its own.

    Per-currency caps in the policy engine cannot sum across currencies without
    a conversion rate, and a pure function has none. So the currency is pinned
    here, at the outer boundary.
    """
    violation = check_mandate(
        _mandate(), "create_payment_link", amount=Money.from_major("100.00", "USD")
    )
    assert violation is not None
    assert violation.field == "currency"


def test_a_negative_amount_is_refused() -> None:
    violation = check_mandate(
        _mandate(), "create_payment_link", amount=Money(minor_units=-1, currency="INR")
    )
    assert violation is not None
    assert violation.field == "max_per_action"


def test_an_action_with_no_amount_is_not_amount_checked() -> None:
    """A read or a status check has no amount and must not be refused for it."""
    mandate = _mandate(allowed_actions=["fetch_payment"], forbidden_actions=[])
    assert check_mandate(mandate, "fetch_payment") is None


# ------------------------------------------------------------------- method


def test_a_disallowed_payment_method_is_refused() -> None:
    violation = check_mandate(
        _mandate(),
        "create_payment_link",
        amount=Money.from_major("100.00", "INR"),
        method="paylater",
    )
    assert violation is not None
    assert violation.field == "allowed_methods"


def test_an_empty_method_list_permits_any_method() -> None:
    mandate = _mandate(allowed_methods=[])
    assert (
        check_mandate(
            mandate,
            "create_payment_link",
            amount=Money.from_major("100.00", "INR"),
            method="paylater",
        )
        is None
    )


# ------------------------------------------------------------- approval threshold


def test_requires_approval_above_the_threshold() -> None:
    mandate = _mandate()
    assert mandate.requires_approval(Money.from_major("2500.01", "INR"))
    assert not mandate.requires_approval(Money.from_major("2500.00", "INR"))
    assert not mandate.requires_approval(None)


def test_no_threshold_means_no_mandate_driven_approval() -> None:
    """Policy may still pause the action; this is the mandate's opinion only."""
    mandate = _mandate(approval_threshold=None)
    assert not mandate.requires_approval(Money.from_major("999999.00", "INR"))


# --------------------------------------------------------- incoherent mandates


def test_a_mandate_whose_currencies_disagree_is_refused_at_load_time() -> None:
    with pytest.raises(BelayError) as excinfo:
        _mandate(currency="INR", max_per_action=Money.from_major("50.00", "USD"))
    assert excinfo.value.code == "mandate_violation"
    assert excinfo.value.detail["field"] == "max_per_action"


def test_per_action_above_cumulative_is_refused() -> None:
    """If one action may exceed the daily budget, one of the two numbers is wrong."""
    with pytest.raises(BelayError) as excinfo:
        _mandate(
            max_per_action=Money.from_major("60000.00", "INR"),
            max_cumulative=Money.from_major("50000.00", "INR"),
        )
    assert excinfo.value.detail["field"] == "max_per_action"


def test_an_action_both_allowed_and_forbidden_is_refused() -> None:
    with pytest.raises(BelayError) as excinfo:
        _mandate(
            allowed_actions=["create_refund"],
            forbidden_actions=["create_refund"],
        )
    assert excinfo.value.detail["field"] == "allowed_actions"


def test_a_negative_limit_is_refused() -> None:
    with pytest.raises(BelayError):
        _mandate(max_per_action=Money(minor_units=-1, currency="INR"))


def test_a_malformed_window_is_refused_at_load_time() -> None:
    with pytest.raises(ValueError):
        _mandate(window="one day")


def test_unknown_mandate_fields_are_refused() -> None:
    """Strict, like every other authority document in the repo (spec §14)."""
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        MerchantMandate.model_validate({"merchant_id": "m", "max_spend": "lots"})


# ------------------------------------------------------------------- hashing


def test_the_hash_is_stable_and_content_addressed() -> None:
    assert _mandate().hash() == _mandate().hash()


def test_changing_any_limit_changes_the_hash() -> None:
    """So tampering with a mandate mid-session is detectable in signed evidence."""
    original = _mandate().hash()
    assert _mandate(max_per_action=Money.from_major("5000.01", "INR")).hash() != original
    assert _mandate(allowed_actions=["create_payment_link", "capture_payment"]).hash() != original
    assert _mandate(forbidden_actions=[]).hash() != original
    assert _mandate(purpose="something else entirely").hash() != original
    assert _mandate(window="7d").hash() != original


# --------------------------------------------------------------------- loading


def test_loads_from_yaml_with_human_authored_major_units(tmp_path) -> None:
    """The shape a merchant actually writes: rupees, not paise."""
    path = tmp_path / "mandate.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "merchant_id": "acme_retail",
                "purpose": "Recover failed checkout payments.",
                "currency": "INR",
                "allowed_actions": ["create_payment_link"],
                "forbidden_actions": ["create_refund"],
                "max_per_action": {"major": "5000.00", "currency": "INR"},
                "max_cumulative": {"major": "50000.00", "currency": "INR"},
                "window": "1d",
                "approval_threshold": {"major": "2500.00", "currency": "INR"},
            }
        ),
        encoding="utf-8",
    )
    mandate = load_mandate(path)
    assert mandate.merchant_id == "acme_retail"
    assert mandate.max_per_action == Money(minor_units=500000, currency="INR")
    assert mandate.max_cumulative == Money(minor_units=5000000, currency="INR")
    assert mandate.window_delta.total_seconds() == 86400
    assert check_mandate(mandate, "create_refund") is not None
