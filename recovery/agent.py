"""The recovery agent loop: inspect, diagnose, prioritise, request.

What this module can and cannot do is the whole point of the architecture, so it
is worth stating precisely.

**Can:** read failed payments, ask a model to diagnose them, rank the results,
and *request* recovery actions.

**Cannot:** execute anything itself. Every action goes out through a
`GovernedCaller`, which in the real system is an MCP client session against
`rekha run` -- the same governed surface any other agent faces. This package
holds no Razorpay credentials, opens no database, and (enforced by
`tests/test_layer_boundaries.py`) cannot import the modules that authorize,
execute, or record.

`GovernedCaller` is a callable rather than a concrete MCP client for two reasons:
it keeps `recovery/` free of any dependency on `rekha.proxy`, and it lets a test
drive the loop against a real `Lifecycle` without spawning subprocesses. The
production path and the test path go through the same function signature.

The loop treats all three governed outcomes as first-class, because conflating
them is a real bug class this project already hit once: a `pending_approval`
response is not a success, and counting it as one is how a caller comes to
believe money moved when it did not.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from rekha.errors import RekhaError
from rekha.finance.money import Money
from rekha.finance.money import total as sum_money

from recovery.diagnose import DEFAULT_BATCH_SIZE, DiagnosisReport, diagnose_batch
from recovery.prioritize import prioritize
from recovery.proposal import PaymentSnapshot, RecoveryPlan, RecoveryProposal
from recovery.providers import LLMProvider

#: One governed tool call. Raises `RekhaError` when the control plane refuses.
#:
#: Returns whatever the upstream returned on success, or a
#: `{"status": "pending_approval", ...}` mapping when the action was parked for a
#: human -- deliberately the same shape `rekha/proxy/lifecycle.py` produces, so
#: nothing has to be translated between the agent and the proxy.
#:
#: A plain callable alias rather than a `Protocol`, because a `Protocol.__call__`
#: matches on parameter *names* as well as types, which would force every caller
#: to spell its arguments the same way for no benefit.
type GovernedCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]


class Outcome(StrEnum):
    """What became of one requested recovery."""

    EXECUTED = "executed"
    PENDING_APPROVAL = "pending_approval"
    REFUSED = "refused"


@dataclass(frozen=True)
class AttemptResult:
    """One recovery attempt and what the control plane did with it."""

    proposal: RecoveryProposal
    outcome: Outcome
    #: Present when executed: the created payment link.
    payment_link_id: str | None = None
    short_url: str | None = None
    #: Present when parked: the approval a human must resolve.
    approval_id: str | None = None
    #: Present when refused: the code and reason, so the report can name the layer
    #: that refused rather than saying "failed".
    refusal_code: str | None = None
    refusal_reason: str | None = None
    refusal_field: str | None = None


@dataclass
class RecoveryRun:
    """Everything one batch run did. The source of the batch metrics.

    Reported per outcome rather than as a single success count, because the track
    bar asks for *measured* money recovered and each of these means something
    different to that number: `executed` created a link (money may yet arrive),
    `pending_approval` is waiting on a human, `refused` was stopped by a limit.
    """

    currency: str = "INR"
    revenue_at_risk: Money = field(default_factory=lambda: Money.zero("INR"))
    considered: int = 0
    diagnosis: DiagnosisReport | None = None
    plan: RecoveryPlan | None = None
    attempts: list[AttemptResult] = field(default_factory=list)

    @property
    def executed(self) -> list[AttemptResult]:
        return [a for a in self.attempts if a.outcome is Outcome.EXECUTED]

    @property
    def pending(self) -> list[AttemptResult]:
        return [a for a in self.attempts if a.outcome is Outcome.PENDING_APPROVAL]

    @property
    def refused(self) -> list[AttemptResult]:
        return [a for a in self.attempts if a.outcome is Outcome.REFUSED]

    @property
    def requested_value(self) -> Money:
        """Total value of links actually created. NOT recovered -- requested.

        Money is recovered when a customer pays, which is a webhook fact, not
        something this run can know. Naming it `requested_value` keeps that
        distinction visible at the call site.
        """
        return sum_money(
            [a.proposal.amount for a in self.executed], currency=self.currency
        )

    @property
    def pending_value(self) -> Money:
        return sum_money(
            [a.proposal.amount for a in self.pending], currency=self.currency
        )

    def refusals_by_layer(self) -> dict[str, int]:
        """Refusal counts by error code -- which control actually fired.

        A run that reports "12 refused" says nothing useful. A run that reports
        `cumulative_limit_exceeded: 11, mandate_violation: 1` shows the control
        plane working, and shows *which part*.
        """
        counts: dict[str, int] = {}
        for attempt in self.refused:
            code = attempt.refusal_code or "unknown"
            counts[code] = counts.get(code, 0) + 1
        return counts


def _unwrap(raw: object) -> dict[str, Any]:
    """Pull a plain dict out of an MCP `CallToolResult`, or pass a dict through.

    Mirrors `rekha/executor/saga.py::_as_dict`. The agent sees whatever the
    `GovernedCaller` hands back, which is a `CallToolResult` over real MCP and a
    plain dict in a direct-`Lifecycle` test.
    """
    structured = getattr(raw, "structuredContent", None)
    if isinstance(structured, dict):
        nested = structured.get("result", structured)
        return dict(nested) if isinstance(nested, dict) else {}
    if isinstance(raw, dict):
        return raw
    if hasattr(raw, "model_dump"):
        dumped = raw.model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _reference_id(proposal: RecoveryProposal) -> str:
    """The idempotency handle for this recovery.

    Derived from the payment being recovered, so a retried run cannot create a
    second link for the same failure. The contract's `idempotency_key` is
    `$args.reference_id`, so this value is what the control plane deduplicates on
    -- which makes the choice of a *stable* derivation load-bearing rather than
    cosmetic.
    """
    return f"recover-{proposal.payment_id}"


async def fetch_failed_payments(
    call: GovernedCaller, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Read the merchant's failed payments through the governed proxy."""
    raw = await call("fetch_all_payments", {"status": "failed", "count": limit})
    body = _unwrap(raw)
    items = body.get("items")
    return [dict(item) for item in items] if isinstance(items, list) else []


async def attempt_recovery(
    call: GovernedCaller, proposal: RecoveryProposal
) -> AttemptResult:
    """Request one recovery. Interprets all three governed outcomes honestly."""
    args: dict[str, Any] = {
        "amount": proposal.amount.minor_units,
        "currency": proposal.amount.currency,
        "description": f"Recovery for {proposal.payment_id}",
        "reference_id": _reference_id(proposal),
        "method": "upi" if proposal.tool.endswith("_upi") else "card",
    }
    try:
        raw = await call(proposal.tool, args)
    except RekhaError as exc:
        return AttemptResult(
            proposal=proposal,
            outcome=Outcome.REFUSED,
            refusal_code=exc.code,
            refusal_reason=str(exc.detail.get("reason") or exc.detail.get("reasons") or ""),
            refusal_field=exc.detail.get("field"),
        )

    body = _unwrap(raw)
    if body.get("status") == "pending_approval":
        return AttemptResult(
            proposal=proposal,
            outcome=Outcome.PENDING_APPROVAL,
            approval_id=str(body.get("approval_id", "")),
        )
    return AttemptResult(
        proposal=proposal,
        outcome=Outcome.EXECUTED,
        payment_link_id=str(body.get("id", "")) or None,
        short_url=str(body.get("short_url", "")) or None,
    )


def derive_now_epoch(payments: list[dict[str, Any]]) -> int:
    """"Now", derived from the observations rather than the wall clock.

    Payment age is a real signal -- a failure from an hour ago is far more
    recoverable than one from three weeks ago -- but taking it from
    `time.time()` makes every request unique, which has two bad consequences:
    a recorded fixture goes stale within the hour, and two runs over the same
    data produce different prompts and so different answers.

    Anchoring to the newest payment in the batch plus an hour fixes both. It is
    also more defensible on its own terms: the agent is reasoning about a
    snapshot, and ages should be relative to when that snapshot was taken, not to
    when someone happened to run the analysis.

    Falls back to the real clock only when there is nothing to anchor to.
    """
    timestamps = [
        int(payment["created_at"])
        for payment in payments
        if isinstance(payment.get("created_at"), int)
    ]
    if not timestamps:
        import time

        return int(time.time())
    return max(timestamps) + 3600


async def run_recovery(
    call: GovernedCaller,
    provider: LLMProvider,
    *,
    now_epoch: int | None = None,
    currency: str = "INR",
    limit: int = 200,
    batch_size: int = DEFAULT_BATCH_SIZE,
    remaining_budget: Money | None = None,
    max_per_action: Money | None = None,
    max_actions: int | None = None,
    min_expected_recovery: Money | None = None,
    execute: bool = True,
) -> RecoveryRun:
    """One full pass: read, diagnose, prioritise, request.

    `remaining_budget` / `max_per_action` / `max_actions` are passed in, not
    looked up. Reading them requires the ledger and the mandate, and this package
    is forbidden both -- so the caller (which has them) tells the agent what it
    has to work with. That is the boundary doing its job rather than being
    described.

    `now_epoch` defaults to `derive_now_epoch(payments)` -- see there for why
    anchoring to the data rather than the clock matters for reproducibility.

    `execute=False` stops after prioritisation, for inspecting what the agent
    *would* do without asking for anything.
    """
    payments = await fetch_failed_payments(call, limit=limit)
    anchor = now_epoch if now_epoch is not None else derive_now_epoch(payments)
    snapshots = [
        PaymentSnapshot.from_razorpay(payment, now_epoch=anchor) for payment in payments
    ]

    run = RecoveryRun(
        currency=currency,
        revenue_at_risk=sum_money(
            [s.amount for s in snapshots if s.amount.currency == currency],
            currency=currency,
        ),
        considered=len(snapshots),
    )
    if not snapshots:
        return run

    run.diagnosis = diagnose_batch(snapshots, provider, batch_size=batch_size)
    run.plan = prioritize(
        run.diagnosis.proposals,
        currency=currency,
        remaining_budget=remaining_budget,
        max_per_action=max_per_action,
        max_actions=max_actions,
        min_expected_recovery=min_expected_recovery,
    )

    if not execute:
        return run

    for proposal in run.plan.selected:
        run.attempts.append(await attempt_recovery(call, proposal))
    return run


def lifecycle_caller(lifecycle: Any, *, read_only: frozenset[str] = frozenset()) -> GovernedCaller:
    """Adapt a `rekha.proxy.lifecycle.Lifecycle` into a `GovernedCaller`.

    Lives here rather than in `rekha/` so the agent's one seam to the control
    plane is visible in the AI package -- but takes `lifecycle` as `Any`
    deliberately: typing it would require importing `rekha.proxy`, and keeping
    even that import out means the boundary test has nothing to argue with.

    Used by tests and by the demo. The production path is an MCP client session,
    which needs no adapter because it is already this shape.
    """

    async def call(tool: str, args: dict[str, Any]) -> Any:
        return await lifecycle.govern_and_execute(
            tool,
            args,
            read_only_hint=tool in read_only or tool.startswith("fetch_"),
            executor=lifecycle_executor(lifecycle),
        )

    return call


def lifecycle_executor(lifecycle: Any) -> Any:
    """The upstream executor a `Lifecycle` needs. Set by the caller before use."""
    executor = getattr(lifecycle, "_recovery_executor", None)
    if executor is None:
        raise RuntimeError(
            "set lifecycle._recovery_executor to the upstream callable before "
            "using lifecycle_caller()"
        )
    return executor
