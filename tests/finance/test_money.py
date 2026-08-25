"""`belay/finance/money.py` -- exact monetary amounts.

The tests that matter most here are the ones proving `Money` *refuses* things:
a float, excess precision, a cross-currency sum. Money that silently accepts
bad input is worse than no type at all, because it launders the error into the
one place it cannot be recovered from.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from belay.errors import BelayError
from belay.finance.money import Money, minor_unit_exponent, total
from hypothesis import given
from hypothesis import strategies as st

# ---------------------------------------------------------------- construction


def test_canonical_construction_is_integer_minor_units() -> None:
    amount = Money(minor_units=240000, currency="INR")
    assert amount.minor_units == 240000
    assert amount.currency == "INR"
    assert amount.as_major() == Decimal("2400.00")


def test_from_major_parses_a_decimal_string_exactly() -> None:
    assert Money.from_major("2400.00", "INR").minor_units == 240000
    assert Money.from_major("2400.10", "INR").minor_units == 240010
    assert Money.from_major("0.01", "INR").minor_units == 1
    assert Money.from_major(2400, "INR").minor_units == 240000
    assert Money.from_major(Decimal("19.99"), "INR").minor_units == 1999


def test_the_classic_float_rounding_bug_cannot_happen() -> None:
    """`2400.10` as a float is 2400.099999...; via Decimal it is exactly 240010."""
    assert Money.from_major("2400.10", "INR").minor_units == 240010
    assert Money.from_major("0.07", "INR").minor_units == 7
    assert Money.from_major("1234567.89", "INR").minor_units == 123456789


def test_yaml_shape_with_major_units_validates() -> None:
    """A mandate written by a human says `{major: "5000.00", currency: INR}`."""
    amount = Money.model_validate({"major": "5000.00", "currency": "INR"})
    assert amount == Money(minor_units=500000, currency="INR")


def test_yaml_shape_with_minor_units_validates() -> None:
    amount = Money.model_validate({"minor_units": 500000, "currency": "INR"})
    assert amount.minor_units == 500000


def test_currency_is_normalized_to_upper_case() -> None:
    assert Money(minor_units=1, currency="inr").currency == "INR"
    assert Money.model_validate({"major": "1.00", "currency": " inr "}).currency == "INR"


def test_zero_is_typed() -> None:
    """A cumulative-spend query over an empty window must return INR 0, not None."""
    zero = Money.zero("INR")
    assert zero.minor_units == 0
    assert zero.currency == "INR"
    assert not zero


# ------------------------------------------------------------------- rejection


def test_a_float_is_refused_outright() -> None:
    with pytest.raises(BelayError) as excinfo:
        Money.from_major(0.1, "INR")
    assert excinfo.value.code == "money_invalid"
    assert "float" in excinfo.value.detail["reason"]


def test_excess_precision_is_refused_not_rounded() -> None:
    """`10.005` INR is ambiguous between 1000 and 1001 paise. Refuse, never guess."""
    with pytest.raises(BelayError) as excinfo:
        Money.from_major("10.005", "INR")
    assert excinfo.value.code == "money_invalid"
    assert "precision" in excinfo.value.detail["reason"]


def test_a_non_numeric_amount_is_refused() -> None:
    with pytest.raises(BelayError) as excinfo:
        Money.from_major("not-a-number", "INR")
    assert excinfo.value.code == "money_invalid"


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf"])
def test_non_finite_amounts_are_refused(bad: str) -> None:
    with pytest.raises(BelayError):
        Money.from_major(bad, "INR")


@pytest.mark.parametrize("bad", ["RUPEE", "IN", "", "1NR", "in r"])
def test_a_malformed_currency_is_refused(bad: str) -> None:
    with pytest.raises(BelayError) as excinfo:
        Money(minor_units=1, currency=bad)
    assert excinfo.value.code == "money_invalid"


def test_giving_both_major_and_minor_units_is_refused() -> None:
    """Two sources of truth for one amount is a bug in the caller, not an input."""
    with pytest.raises(BelayError) as excinfo:
        Money.model_validate({"major": "1.00", "minor_units": 100, "currency": "INR"})
    assert excinfo.value.code == "money_invalid"


def test_major_without_currency_is_refused() -> None:
    with pytest.raises(BelayError):
        Money.model_validate({"major": "1.00"})


def test_money_has_no_float_or_int_coercion() -> None:
    """The type is deliberately awkward to misuse: no silent numeric coercion."""
    amount = Money(minor_units=100, currency="INR")
    assert not hasattr(amount, "__float__")
    assert not hasattr(amount, "__int__")
    with pytest.raises(TypeError):
        float(amount)  # type: ignore[arg-type]


def test_unknown_fields_are_forbidden() -> None:
    with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
        Money.model_validate({"minor_units": 1, "currency": "INR", "note": "x"})


def test_money_is_immutable() -> None:
    amount = Money(minor_units=100, currency="INR")
    with pytest.raises(Exception):  # noqa: B017 - pydantic frozen error
        amount.minor_units = 200  # type: ignore[misc]


# ------------------------------------------------------------------ arithmetic


def test_addition_and_subtraction_are_exact() -> None:
    a = Money.from_major("0.10", "INR")
    b = Money.from_major("0.20", "INR")
    assert (a + b) == Money.from_major("0.30", "INR")
    assert (b - a) == Money.from_major("0.10", "INR")


def test_the_float_comparison_bug_that_motivates_this_type() -> None:
    """In floats, 0.1 + 0.2 > 0.3. In Money it is exactly equal.

    This is the whole reason limits are compared in integers: a cumulative cap
    sums many amounts before comparing, and float error accumulates toward
    authorizing more than the merchant permitted.
    """
    assert 0.1 + 0.2 > 0.3  # the bug this type exists to prevent
    tenth = Money.from_major("0.10", "INR")
    fifth = Money.from_major("0.20", "INR")
    three_tenths = Money.from_major("0.30", "INR")
    assert (tenth + fifth) == three_tenths
    assert not (tenth + fifth) > three_tenths


def test_multiplication_by_a_count() -> None:
    """40 payment links of Rs 4,000 -- the cumulative fan-out scenario."""
    each = Money.from_major("4000.00", "INR")
    assert (each * 40) == Money.from_major("160000.00", "INR")
    assert (40 * each) == Money.from_major("160000.00", "INR")


def test_multiplication_by_a_non_integer_is_refused() -> None:
    each = Money.from_major("4000.00", "INR")
    with pytest.raises(TypeError):
        each * 1.5  # type: ignore[operator]
    with pytest.raises(TypeError):
        each * True  # bool is an int subclass; still not a count


def test_cross_currency_arithmetic_raises() -> None:
    inr = Money(minor_units=100, currency="INR")
    usd = Money(minor_units=100, currency="USD")
    for op in (lambda: inr + usd, lambda: inr - usd, lambda: inr < usd):
        with pytest.raises(BelayError) as excinfo:
            op()
        assert excinfo.value.code == "currency_mismatch"


def test_cross_currency_equality_is_false_not_an_error() -> None:
    """Ordering has no correct cross-currency answer; equality does.

    Raising here would make Money unusable as a dict key or in an assertion.
    """
    assert Money(minor_units=100, currency="INR") != Money(minor_units=100, currency="USD")
    assert Money(minor_units=100, currency="INR") != "INR 1.00"


def test_money_is_hashable_and_usable_in_a_set() -> None:
    a = Money(minor_units=100, currency="INR")
    b = Money(minor_units=100, currency="INR")
    assert len({a, b}) == 1


def test_ordering_is_total() -> None:
    small = Money.from_major("100.00", "INR")
    large = Money.from_major("5000.00", "INR")
    assert small < large
    assert small <= large
    assert large > small
    assert large >= small
    assert small != large


def test_negative_amounts_are_representable_and_flagged() -> None:
    """A refund leg or a reversal is legitimately negative; callers must see it."""
    negative = Money(minor_units=-100, currency="INR")
    assert negative.is_negative
    assert not Money.zero("INR").is_negative


def test_total_sums_a_list_and_types_the_empty_case() -> None:
    amounts = [Money.from_major("100.00", "INR"), Money.from_major("250.50", "INR")]
    assert total(amounts, currency="INR") == Money.from_major("350.50", "INR")
    assert total([], currency="INR") == Money.zero("INR")


def test_total_raises_on_a_mixed_currency_list() -> None:
    with pytest.raises(BelayError) as excinfo:
        total(
            [Money(minor_units=1, currency="INR"), Money(minor_units=1, currency="USD")],
            currency="INR",
        )
    assert excinfo.value.code == "currency_mismatch"


# ----------------------------------------------------------------- presentation


def test_minor_unit_exponents() -> None:
    assert minor_unit_exponent("INR") == 2
    assert minor_unit_exponent("inr") == 2
    assert minor_unit_exponent("JPY") == 0
    assert minor_unit_exponent("KWD") == 3
    assert minor_unit_exponent("ZZZ") == 2  # unlisted defaults to 2


def test_zero_decimal_currency_round_trips() -> None:
    """JPY has no minor unit: 2400 yen is 2400, not 240000."""
    yen = Money.from_major("2400", "JPY")
    assert yen.minor_units == 2400
    assert yen.as_major() == Decimal("2400")


def test_str_is_human_readable() -> None:
    assert str(Money.from_major("240000.00", "INR")) == "INR 240,000.00"
    assert str(Money.from_major("2400", "JPY")) == "JPY 2,400"


def test_repr_shows_minor_units() -> None:
    assert repr(Money(minor_units=100, currency="INR")) == "Money(100, 'INR')"


# -------------------------------------------------------------------- property


@given(
    st.integers(min_value=-10**12, max_value=10**12),
    st.integers(min_value=-10**12, max_value=10**12),
)
def test_addition_never_loses_a_paisa(left: int, right: int) -> None:
    """Property: integer addition is exact, so the sum is the sum of the parts."""
    a = Money(minor_units=left, currency="INR")
    b = Money(minor_units=right, currency="INR")
    assert (a + b).minor_units == left + right


@given(st.integers(min_value=0, max_value=10**10))
def test_major_minor_round_trip_is_lossless(minor: int) -> None:
    """Property: minor -> major -> minor returns the original integer."""
    original = Money(minor_units=minor, currency="INR")
    assert Money.from_major(original.as_major(), "INR") == original


@given(st.lists(st.integers(min_value=0, max_value=10**9), max_size=50))
def test_total_equals_the_integer_sum(minors: list[int]) -> None:
    """Property: summing many amounts accumulates no error, unlike floats."""
    amounts = [Money(minor_units=m, currency="INR") for m in minors]
    assert total(amounts, currency="INR").minor_units == sum(minors)
