"""`MerchantMandate`: the merchant's written grant of authority to the agent.

This is the outermost boundary of the control plane. It is checked *before*
contract resolution, planning, or policy evaluation, because those layers
answer "is this action safe?" while the mandate answers a prior question:
"did the merchant authorize this agent to do this kind of thing at all?"

Deliberately narrower than the marketing version of the idea. `IntentContract`
(which this replaces, ADR 0028) already argued the point in its own docstring
and it transfers verbatim: nothing on the authorization path may be decided by
prose an LLM has to interpret. So a mandate carries only mechanically
checkable constraints:

- `allowed_actions` / `forbidden_actions` -- exact tool names. An action absent
  from `allowed_actions` is refused. This is what stops a prompt-injected
  agent issuing a refund: `create_refund` exists in the contract catalog and
  is simply not in the merchant's grant.
- `max_per_action` -- a `Money` ceiling on any single action.
- `max_cumulative` + `window` -- an aggregate ceiling over a rolling window.
  Enforced by `belay/policy/cumulative.py`; declared here because it is the
  merchant's number, not the operator's.
- `allowed_methods` -- payment instruments the agent may use.
- `currency` -- the single currency this mandate authorizes. Pinned here
  because `PolicyEngine`'s per-currency caps cannot sum across currencies
  without a conversion rate, and a policy engine that stays a pure function
  has none. Fixing the currency at the boundary closes that as an evasion
  route.
- `approval_threshold` -- above this value a human must approve.

`purpose` is free text and is deliberately *not* enforced. It exists so the
ledger records what the merchant thought they were authorizing, and so a human
reviewing an approval has context. Pretending to machine-check "only chase
genuinely recoverable payments" would be worse than not checking it.

The mandate's canonical hash is pinned into `session_started` and covered by
the Ed25519 evidence signature, exactly as `IntentContract`'s was. So which
authority governed a session is a signed fact, not an assertion made
afterwards by whichever file happened to be on disk at report time.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from belay.canonical import canonical_hash
from belay.errors import BelayError
from belay.finance.money import Money
from belay.policy.quota import parse_window


class MandateViolation(BaseModel):
    """Why an action falls outside the mandate. Names the field, not just 'denied'.

    `field` is the mandate clause that refused the action, so the agent's
    self-explanation and the demo can both say *which* limit applied rather
    than reporting an opaque refusal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: str
    field: str
    detail: dict[str, Any] = Field(default_factory=dict)


class MerchantMandate(BaseModel):
    """One merchant's grant of authority, loaded from YAML and hash-pinned."""

    model_config = ConfigDict(extra="forbid")

    merchant_id: str
    #: Free text, recorded in evidence, never enforced. See module docstring.
    purpose: str = ""
    currency: str = "INR"

    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    allowed_methods: list[str] = Field(default_factory=list)

    max_per_action: Money | None = None
    max_cumulative: Money | None = None
    window: str = "1d"
    max_actions_per_window: int | None = None
    approval_threshold: Money | None = None

    @model_validator(mode="after")
    def _check_coherent(self) -> MerchantMandate:
        """Reject a mandate that cannot mean what it says.

        Loudly, at load time, rather than at the moment money is about to
        move. A mandate is a small hand-written document; every one of these
        mistakes is plausible and every one of them would otherwise fail open
        or produce a confusing refusal much later.
        """
        parse_window(self.window)  # raises ValueError on a malformed window

        for name, amount in (
            ("max_per_action", self.max_per_action),
            ("max_cumulative", self.max_cumulative),
            ("approval_threshold", self.approval_threshold),
        ):
            if amount is None:
                continue
            if amount.currency != self.currency:
                raise BelayError(
                    "mandate_violation",
                    {
                        "reason": f"{name} is in {amount.currency} but the mandate's "
                        f"currency is {self.currency}",
                        "field": name,
                    },
                )
            if amount.is_negative:
                raise BelayError(
                    "mandate_violation",
                    {"reason": f"{name} must not be negative", "field": name},
                )

        if (
            self.max_per_action is not None
            and self.max_cumulative is not None
            and self.max_per_action > self.max_cumulative
        ):
            raise BelayError(
                "mandate_violation",
                {
                    "reason": "max_per_action exceeds max_cumulative, so the aggregate "
                    "ceiling could never bind -- one of the two is wrong",
                    "field": "max_per_action",
                },
            )

        if self.max_actions_per_window is not None and self.max_actions_per_window < 0:
            raise BelayError(
                "mandate_violation",
                {"reason": "max_actions_per_window must not be negative", "field": "window"},
            )

        overlap = sorted(set(self.allowed_actions) & set(self.forbidden_actions))
        if overlap:
            raise BelayError(
                "mandate_violation",
                {
                    "reason": f"action(s) both allowed and forbidden: {overlap}; a mandate "
                    "must be unambiguous about what it grants",
                    "field": "allowed_actions",
                },
            )
        return self

    # ---- derived properties ----------------------------------------------

    @property
    def window_delta(self) -> timedelta:
        """`window` as a `timedelta` (`"1d"`, `"12h"`, `"30m"`)."""
        return parse_window(self.window)

    def hash(self) -> str:
        """Canonical hash, pinned into `session_started` and signed with the evidence.

        Same mechanism as `ContractSet.set_hash`, so there is one way to
        fingerprint a governing document, not two.
        """
        return canonical_hash(self.model_dump(mode="json"))

    def requires_approval(self, amount: Money | None) -> bool:
        """Whether `amount` sits above the threshold at which a human must decide.

        No threshold configured means no mandate-driven approval requirement.
        An action with no amount at all (a read, a status check) never triggers
        one. Policy may still pause the action for its own reasons -- this is
        the mandate's opinion, not the only one.
        """
        if self.approval_threshold is None or amount is None:
            return False
        return amount > self.approval_threshold


def check_mandate(
    mandate: MerchantMandate,
    tool: str,
    *,
    amount: Money | None = None,
    method: str | None = None,
) -> MandateViolation | None:
    """Check one action against `mandate`. `None` means permitted.

    Checked most-specific-refusal first, so the reported reason is the most
    informative one available: an explicitly forbidden action reports as
    forbidden rather than as merely absent from the allow-list.

    Cumulative and velocity limits are *not* checked here. They are a function
    of ledger history rather than of this action alone, so they belong to
    `belay/policy/cumulative.py`, which has the ledger. This function stays a
    pure function of its arguments.
    """
    if tool in mandate.forbidden_actions:
        return MandateViolation(
            reason=f"{tool!r} is explicitly forbidden by this mandate",
            field="forbidden_actions",
            detail={"tool": tool},
        )

    if mandate.allowed_actions and tool not in mandate.allowed_actions:
        return MandateViolation(
            reason=f"{tool!r} is not among the actions this mandate authorizes",
            field="allowed_actions",
            detail={"tool": tool, "allowed": sorted(mandate.allowed_actions)},
        )

    if amount is not None:
        if amount.currency != mandate.currency:
            return MandateViolation(
                reason=f"this mandate authorizes {mandate.currency} only, not "
                f"{amount.currency}",
                field="currency",
                detail={"mandate_currency": mandate.currency, "amount": str(amount)},
            )
        if amount.is_negative:
            return MandateViolation(
                reason="a negative amount is not a recovery action",
                field="max_per_action",
                detail={"amount": str(amount)},
            )
        if mandate.max_per_action is not None and amount > mandate.max_per_action:
            return MandateViolation(
                reason=f"{amount} exceeds the per-action ceiling of "
                f"{mandate.max_per_action}",
                field="max_per_action",
                detail={"amount": str(amount), "limit": str(mandate.max_per_action)},
            )

    if method is not None and mandate.allowed_methods and method not in mandate.allowed_methods:
        return MandateViolation(
            reason=f"payment method {method!r} is not permitted by this mandate",
            field="allowed_methods",
            detail={"method": method, "allowed": sorted(mandate.allowed_methods)},
        )

    return None


def load_mandate(path: str | Path) -> MerchantMandate:
    """Load a `MerchantMandate` from a YAML (or JSON) document.

    Operator-facing, so a malformed document raises the underlying
    `yaml.YAMLError` / `pydantic.ValidationError` directly -- mirroring
    `belay/policy/model.py::load_policy`. Those errors describe an operator's
    config mistake, not something an agent should ever see as a protocol error.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return MerchantMandate.model_validate(raw)
