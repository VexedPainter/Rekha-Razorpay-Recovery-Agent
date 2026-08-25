"""`Money`: monetary amounts as integer minor units. Never floats.

Why this module exists at all, when `float` would "work":

1. **Razorpay denominates in minor units.** Every amount in the Payments API
   is an integer number of paise. Representing Rs 2,400.00 as `2400.0` and
   converting at the boundary invites exactly one class of bug -- an amount
   that is off by a factor of 100 -- in the one place a bug is unrecoverable.
   Storing `240000` and formatting for display inverts that risk.

2. **Floats do not compare the way limits need to.** A policy limit is a
   comparison (`spend > cap`), and `0.1 + 0.2 > 0.3` is `True` in binary
   floating point. A cumulative limit sums many amounts before comparing, so
   the error accumulates in the direction of authorizing more than the
   merchant permitted. Integers make the comparison exact by construction
   rather than by tolerance.

3. **Evidence must be reproducible.** Ledger events are hashed
   (`rekha/canonical.py`), and a float's shortest-repr can differ across
   platforms and Python versions. An integer serializes one way, forever, so
   a signed evidence bundle verifies on a machine that is not ours.

The type is deliberately awkward to misuse: there is no `__float__`, no
`__int__`, and no arithmetic with bare numbers. Mixing currencies raises
rather than coercing. Converting from human-authored decimal strings goes
through `decimal.Decimal`, never `float`, so `"2400.10"` cannot arrive as
`240009.99999`.

`Money` is a Pydantic model rather than a plain dataclass so it drops into the
existing strict contract and policy documents (`rekha/contracts/model.py`,
`rekha/policy/model.py`) and inherits their validation and JSON
serialization, rather than needing a parallel parsing path.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from functools import total_ordering
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from rekha.errors import RekhaError

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

#: Minor units per major unit, by ISO-4217 code. Most currencies use 2
#: decimal places; the exceptions that matter are listed. Anything absent
#: defaults to 2 -- correct for INR (paise), which is this project's domain.
#: Deliberately a small table rather than a full ISO-4217 dependency: an
#: unlisted currency getting the wrong exponent is a display bug, whereas the
#: comparison and summation this module exists for stay exact regardless.
_MINOR_UNIT_EXPONENT: dict[str, int] = {
    "BHD": 3,
    "CLP": 0,
    "IQD": 3,
    "ISK": 0,
    "JOD": 3,
    "JPY": 0,
    "KRW": 0,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
    "VND": 0,
}

_DEFAULT_EXPONENT = 2


def minor_unit_exponent(currency: str) -> int:
    """Decimal places `currency` subdivides into (2 for INR, 0 for JPY)."""
    return _MINOR_UNIT_EXPONENT.get(currency.upper(), _DEFAULT_EXPONENT)


@total_ordering
class Money(BaseModel):
    """An exact monetary amount: integer `minor_units` plus an ISO-4217 `currency`.

    Construct canonically::

        Money(minor_units=240000, currency="INR")     # Rs 2,400.00

    or from a human-authored decimal string, which is what policy documents
    and merchant mandates are written in::

        Money.model_validate({"major": "2400.00", "currency": "INR"})
        Money.from_major("2400.00", "INR")

    Both forms are accepted by Pydantic validation, so a YAML mandate can say
    `max_per_action: {major: "5000.00", currency: INR}` and stay readable
    while the in-memory value remains an exact integer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    minor_units: int
    currency: str

    # ---- construction -----------------------------------------------------

    @model_validator(mode="before")
    @classmethod
    def _accept_major_units(cls, data: Any) -> Any:
        """Allow `{"major": "2400.00", "currency": "INR"}` as an input shape.

        Parsed via `Decimal`, never `float`. A `major` value carrying more
        decimal places than the currency subdivides into is an error, not a
        silent rounding -- `{"major": "10.005", "currency": "INR"}` is
        ambiguous about whether the merchant meant 1000 or 1001 paise, and
        guessing on a monetary limit is not acceptable.
        """
        if not isinstance(data, dict) or "major" not in data:
            return data
        if "minor_units" in data:
            raise RekhaError(
                "money_invalid",
                {"reason": "give either `major` or `minor_units`, never both", "value": data},
            )
        raw = dict(data)
        major = raw.pop("major")
        currency = raw.get("currency")
        if not isinstance(currency, str):
            raise RekhaError(
                "money_invalid", {"reason": "`currency` is required alongside `major`"}
            )
        raw["minor_units"] = _major_to_minor(major, currency)
        return raw

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, value: str) -> str:
        upper = value.strip().upper()
        if not _CURRENCY_RE.match(upper):
            raise RekhaError(
                "money_invalid",
                {"reason": "currency must be a 3-letter ISO-4217 code", "currency": value},
            )
        return upper

    @classmethod
    def from_major(cls, major: str | int | Decimal, currency: str) -> Money:
        """Build from a major-unit decimal *string* (or int/Decimal). No floats.

        `float` is rejected deliberately: `Money.from_major(0.1, "INR")` looks
        harmless and is not, so the type refuses rather than rounding.
        """
        return cls(minor_units=_major_to_minor(major, currency), currency=currency.upper())

    @classmethod
    def zero(cls, currency: str) -> Money:
        return cls(minor_units=0, currency=currency.upper())

    # ---- arithmetic -------------------------------------------------------

    def _guard(self, other: Money, op: str) -> None:
        if not isinstance(other, Money):  # pragma: no cover - defensive
            raise TypeError(f"cannot {op} Money and {type(other).__name__}")
        if self.currency != other.currency:
            raise RekhaError(
                "currency_mismatch",
                {
                    "reason": f"cannot {op} {self.currency} and {other.currency}",
                    "left": self.currency,
                    "right": other.currency,
                },
            )

    def __add__(self, other: Money) -> Money:
        self._guard(other, "add")
        return Money(minor_units=self.minor_units + other.minor_units, currency=self.currency)

    def __sub__(self, other: Money) -> Money:
        self._guard(other, "subtract")
        return Money(minor_units=self.minor_units - other.minor_units, currency=self.currency)

    def __mul__(self, count: int) -> Money:
        """Scale by a whole number of times (e.g. 40 payment links of Rs 4,000).

        Integer only: multiplying money by a fraction needs an explicit
        rounding policy, and this module will not pick one silently.
        """
        if not isinstance(count, int) or isinstance(count, bool):
            raise TypeError("Money can only be multiplied by an int")
        return Money(minor_units=self.minor_units * count, currency=self.currency)

    __rmul__ = __mul__

    # ---- comparison -------------------------------------------------------

    def __lt__(self, other: Money) -> bool:
        self._guard(other, "compare")
        return self.minor_units < other.minor_units

    def __eq__(self, other: object) -> bool:
        """Equality across currencies is `False`, never an error.

        Ordering (`<`, `>=`) raises on a currency mismatch because there is no
        correct answer, but `==` has one: two amounts in different currencies
        are not the same amount. Raising here would make `Money` unusable in a
        set, a dict key, or an `assertEqual`.
        """
        if not isinstance(other, Money):
            return NotImplemented
        return (self.minor_units, self.currency) == (other.minor_units, other.currency)

    def __hash__(self) -> int:
        return hash((self.minor_units, self.currency))

    def __bool__(self) -> bool:
        return self.minor_units != 0

    @property
    def is_negative(self) -> bool:
        return self.minor_units < 0

    # ---- presentation -----------------------------------------------------

    def as_major(self) -> Decimal:
        """Exact major-unit value, as a `Decimal`. For display and reports only."""
        exponent = minor_unit_exponent(self.currency)
        return Decimal(self.minor_units).scaleb(-exponent)

    def __str__(self) -> str:
        exponent = minor_unit_exponent(self.currency)
        return f"{self.currency} {self.as_major():,.{exponent}f}"

    def __repr__(self) -> str:
        return f"Money({self.minor_units}, {self.currency!r})"


def _major_to_minor(major: str | int | Decimal | Any, currency: str) -> int:
    """Convert a major-unit value to exact integer minor units.

    Rejects `float` outright and rejects more precision than the currency
    subdivides into, rather than rounding a monetary limit behind the
    caller's back.
    """
    if isinstance(major, float):
        raise RekhaError(
            "money_invalid",
            {
                "reason": "refusing to build Money from a float -- pass a decimal "
                "string like '2400.00' so the value is exact",
                "value": repr(major),
            },
        )
    try:
        amount = Decimal(major) if not isinstance(major, Decimal) else major
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RekhaError(
            "money_invalid",
            {"reason": "not a valid decimal amount", "value": repr(major)},
        ) from exc

    if not amount.is_finite():
        raise RekhaError("money_invalid", {"reason": "amount must be finite", "value": str(major)})

    exponent = minor_unit_exponent(currency)
    shifted = amount.scaleb(exponent)
    if shifted != shifted.to_integral_value():
        raise RekhaError(
            "money_invalid",
            {
                "reason": f"{currency} subdivides into {exponent} decimal places; "
                f"{major!r} has more precision than that, and rounding a "
                f"monetary amount silently is not acceptable",
                "value": str(major),
            },
        )
    return int(shifted)


def total(amounts: list[Money], *, currency: str) -> Money:
    """Sum `amounts`, returning `Money.zero(currency)` for an empty list.

    `currency` is required rather than inferred from the first element so that
    summing an empty list still yields a typed zero -- a cumulative-spend
    query over a window with no activity must return `INR 0.00`, not `None`
    and not an untyped `0`.
    """
    result = Money.zero(currency)
    for amount in amounts:
        result = result + amount
    return result
