"""Belay error model (spec §11).

Defines the 17 error codes normative for 0.1, each with its `retryable`
flag. Full BelayError exception class and error-construction helpers
arrive with the components that raise them (E1+); this module fixes the
canonical registry early so every later entrega imports from one place.

The financial control plane (ADR 0028) needs codes spec §11 does not define.
Rather than editing the normative list -- which would quietly break the claim
that §11 has exactly 17 codes -- they live in a separate, explicitly additive
registry below, and `ERROR_CODES` is the union. `SPEC_ERROR_CODES` remains
exactly the normative 17, and `tests/test_imports.py` still pins that.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

# code -> retryable (spec §11). Normative and closed: do not add here.
SPEC_ERROR_CODES: Final[Mapping[str, bool]] = {
    "contract_missing": False,
    "contract_invalid": False,
    "expression_invalid": False,
    "capture_failed": False,
    "plan_expired": True,
    "plan_mismatch": False,
    "policy_denied": False,
    "approval_required": True,
    "approval_rejected": False,
    "approval_expired": False,
    "idempotency_conflict": False,
    "step_indeterminate": False,
    "compensation_failed": False,
    "verification_failed": False,
    "session_fenced": False,
    "ledger_integrity_error": False,
    "unsafe_passthrough_disabled": False,
}

#: Additive codes for the financial control plane (ADR 0028). Not part of
#: spec §11. All are non-retryable: every one of them means "this action is
#: not permitted as stated", and retrying an identical unauthorized action
#: cannot change that answer. A caller must change the action, or a human must
#: change the mandate -- so marking any of these retryable would invite an
#: agent to spin against a limit instead of surfacing it.
FINANCE_ERROR_CODES: Final[Mapping[str, bool]] = {
    # `belay/finance/money.py`: a malformed amount (a float, a bad decimal
    # string, more precision than the currency subdivides into).
    "money_invalid": False,
    # Arithmetic or ordering across two different currencies.
    "currency_mismatch": False,
    # `belay/finance/mandate.py`: the merchant's grant of authority does not
    # cover this action -- wrong action, over the per-action ceiling, or a
    # payment method the mandate excludes.
    "mandate_violation": False,
    # `belay/policy/cumulative.py`: this action would push windowed spend past
    # the mandate's aggregate ceiling. Distinct from `policy_denied` so the
    # agent (and the demo) can tell "never allowed" from "budget exhausted".
    "cumulative_limit_exceeded": False,
    # Too many actions in the window, irrespective of amount.
    "velocity_limit_exceeded": False,
    # `belay/settlement/verify.py`: money moved that does not reconcile
    # against what was authorized.
    "settlement_mismatch": False,
    # `belay/razorpay/webhooks.py`: HMAC verification failed, so the payload
    # is not evidence of anything.
    "webhook_signature_invalid": False,
}

#: Every code `BelayError` will accept: the normative 17 plus the financial
#: extensions. Kept as one lookup so call sites need not know which registry a
#: code came from.
ERROR_CODES: Final[Mapping[str, bool]] = {**SPEC_ERROR_CODES, **FINANCE_ERROR_CODES}


class BelayError(Exception):
    """Structured error per spec §11: `{"code", "detail", "retryable"}`.

    Raised at the boundaries components use to signal one of the 17
    normative error codes. `code` must be a key of `ERROR_CODES`; `retryable`
    defaults to the registry's value for that code but may be overridden.
    """

    def __init__(
        self,
        code: str,
        detail: Mapping[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown Belay error code: {code!r}")
        self.code = code
        self.detail: dict[str, Any] = dict(detail) if detail else {}
        self.retryable = ERROR_CODES[code] if retryable is None else retryable
        super().__init__(f"{code}: {self.detail}")

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "retryable": self.retryable}
