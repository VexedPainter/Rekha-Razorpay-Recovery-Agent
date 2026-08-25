"""`belay recover` -- run the AI recovery agent against a governed Razorpay surface.

Lives in `belay/cli/` rather than in `recovery/` because of what it has to do:
read the mandate, read the ledger to work out how much budget is left, and
construct the governed lifecycle. All three are things the AI layer is forbidden
from doing, so the wiring belongs on this side of the boundary. The agent is
*handed* its budget; it cannot look it up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from belay.finance.money import Money


def _describe_razorpay_action(
    tool: str, args: dict[str, Any]
) -> tuple[Money | None, str | None]:
    """Read `(amount, method)` out of a Razorpay tool call's arguments.

    This is the `Lifecycle.action_describer` for Razorpay: the integration-specific
    knowledge that Razorpay spells the amount `args["amount"]` in minor units and
    the method `args["method"]`. Deliberately not a default inside
    `belay/proxy/`, which must stay domain-agnostic -- and deliberately not a
    guess at field names, because reading the wrong key would silently enforce no
    ceiling at all.
    """
    raw = args.get("amount")
    amount = (
        Money(minor_units=raw, currency=str(args.get("currency") or "INR"))
        if isinstance(raw, int) and not isinstance(raw, bool)
        else None
    )
    method = args.get("method")
    return amount, (str(method) if method else None)


def register(app: typer.Typer) -> None:
    """Attach the `recover` command to the CLI app."""

    @app.command()
    def recover(
        mandate: str = typer.Option(
            "examples/mandates/merchant.yaml",
            "--mandate",
            help="Merchant mandate YAML: the grant of authority this run operates under.",
        ),
        contracts: str = typer.Option(
            "packs/razorpay/contracts.yaml",
            "--contracts",
            help="Contract pack. Defines the agent's entire action space; anything "
            "undeclared is refused before it is planned.",
        ),
        policy: str = typer.Option(
            "packs/razorpay/policy.yaml",
            "--policy",
            help="Operator policy: the blast-radius backstop, independent of the mandate.",
        ),
        db: str = typer.Option(
            "recovery.db", "--db", help="Ledger path. Reused across runs so the "
            "rolling cumulative window is real rather than reset each time."
        ),
        limit: int = typer.Option(200, "--limit", help="Failed payments to consider."),
        provider: str = typer.Option(
            "", "--provider", help="LLM provider: gemini, groq, anthropic, or replay "
            "(recorded fixtures, offline). Default: the first one with a key present, "
            "free providers first, falling back to replay."
        ),
        live: bool = typer.Option(
            False, "--live", help="Use the real Razorpay MCP server in TEST MODE "
            "instead of the offline sandbox. Requires credentials."
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Diagnose and prioritise, but request nothing."
        ),
        initiated_by: str = typer.Option(
            "recovery-agent", "--initiated-by", help="Identity recorded in evidence."
        ),
    ) -> None:
        """Diagnose failed payments with AI and recover them under a merchant mandate."""
        import anyio
        from recovery.agent import run_recovery
        from recovery.providers import resolve_provider

        from belay.contracts.loader import load_contract_set
        from belay.finance.mandate import load_mandate
        from belay.ledger.store import LedgerStore
        from belay.policy.cumulative import CumulativeTracker
        from belay.policy.model import load_policy
        from belay.proxy.lifecycle import Lifecycle
        from belay.proxy.upstream import connect_stdio

        mandate_obj = load_mandate(mandate)
        contract_set = load_contract_set([Path(contracts)])
        policy_doc = load_policy(policy)
        llm = resolve_provider(provider or None)

        ledger = LedgerStore(f"sqlite:///{db}")
        session_id = f"s_recover_{Path(db).stem}_{len(ledger.read_all())}"

        # The budget the agent is allowed to spend. Computed HERE, from the ledger,
        # because the agent cannot read it -- and handed over as a plain amount.
        tracker = CumulativeTracker(ledger)
        remaining: Money | None = None
        if mandate_obj.max_cumulative is not None:
            already = tracker.spent_by_merchant(
                mandate_obj.merchant_id,
                currency=mandate_obj.currency,
                now=ledger._clock.now(),
                window=mandate_obj.window_delta,
            )
            remaining = mandate_obj.max_cumulative - already
            if remaining.is_negative:
                remaining = Money.zero(mandate_obj.currency)

        command, args_list = _upstream_command(live=live)

        typer.echo(f"merchant   : {mandate_obj.merchant_id}")
        typer.echo(f"mandate    : {mandate_obj.hash()[:16]}...")
        typer.echo(f"per action : {mandate_obj.max_per_action}")
        typer.echo(
            f"budget     : {remaining} remaining of {mandate_obj.max_cumulative}"
            f" per {mandate_obj.window}"
        )
        typer.echo(f"model      : {llm.name}/{llm.model}")
        typer.echo(f"upstream   : {'RAZORPAY TEST MODE' if live else 'offline sandbox'}")
        typer.echo("")

        async def _main() -> None:
            import os

            async with connect_stdio(command, args_list, env=dict(os.environ)) as upstream:
                lifecycle = Lifecycle(
                    contract_set=contract_set,
                    unsafe_passthrough_tools=frozenset(),
                    ledger=ledger,
                    session_id=session_id,
                    policy=policy_doc,
                    mandate=mandate_obj,
                    action_describer=_describe_razorpay_action,
                )
                lifecycle.start_session(initiated_by, mandate_obj.merchant_id)

                async def call(tool: str, call_args: dict[str, Any]) -> Any:
                    return await lifecycle.govern_and_execute(
                        tool,
                        call_args,
                        read_only_hint=tool.startswith("fetch_"),
                        executor=upstream.call_tool,
                    )

                run = await run_recovery(
                    call,
                    llm,
                    # `now_epoch` deliberately omitted: the agent derives it from
                    # the newest payment in the batch. Anchoring payment age to
                    # the data rather than the wall clock makes a run reproducible
                    # and keeps recorded fixtures from going stale within the hour.
                    currency=mandate_obj.currency,
                    limit=limit,
                    remaining_budget=remaining,
                    max_per_action=mandate_obj.max_per_action,
                    max_actions=mandate_obj.max_actions_per_window,
                    execute=not dry_run,
                )
                # Record every forecast the AI made, selected or not, BEFORE
                # reporting. Scoring only the actions taken would be biased: the
                # agent chose those because it was confident, so it would be
                # measuring its confidence on the easy cases.
                _record_forecasts(ledger, session_id, run)
                _report(run, session_id=session_id, db=db)

        anyio.run(_main)


def _upstream_command(*, live: bool) -> tuple[str, list[str]]:
    """The upstream MCP server to wrap.

    Offline by default. `--live` uses Razorpay's REMOTE MCP server via
    `npx mcp-remote`, which needs only Node -- no Docker. The merchant token is
    base64(key_id:key_secret), assembled here from the environment so the secret
    never appears in a command line that a process list could show.
    """
    import base64
    import sys

    if not live:
        return sys.executable, ["examples/razorpay-sandbox/server.py"]

    from recovery.providers import load_env

    env = load_env()
    key_id = env.get("RAZORPAY_KEY_ID", "")
    key_secret = env.get("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        raise typer.BadParameter(
            "--live needs RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET in .env"
        )
    if not key_id.startswith("rzp_test_"):
        raise typer.BadParameter(
            f"refusing to run against a non-test key ({key_id[:12]}...). "
            f"This project is test-mode only."
        )
    token = base64.b64encode(f"{key_id}:{key_secret}".encode()).decode()
    return "npx", [
        "-y",
        "mcp-remote",
        "https://mcp.razorpay.com/mcp",
        "--header",
        f"Authorization: Basic {token}",
    ]


def _record_forecasts(ledger: object, session_id: str, run: Any) -> None:
    """Write every AI forecast from this run into the ledger.

    Done here rather than inside `recovery/` because the AI layer is forbidden from
    importing `belay.ledger` -- an agent able to write its own evidence could
    rewrite its own track record. The control plane records what it was handed.
    """
    from belay.razorpay.forecast import record_proposal

    plan = run.plan
    if plan is None:
        return

    selected_ids = {p.payment_id for p in plan.selected}
    everything = [*plan.selected, *plan.declined_for_budget, *plan.not_worth_pursuing]
    for proposal in everything:
        record_proposal(
            ledger,  # type: ignore[arg-type]
            session_id,
            payment_id=proposal.payment_id,
            reference_id=f"recover-{proposal.payment_id}",
            cause_class=proposal.cause_class.value,
            strategy=proposal.strategy.value,
            amount=proposal.amount,
            expected_recovery=proposal.expected_recovery,
            confidence=proposal.confidence.value,
            diagnosis=proposal.diagnosis,
            reasoning=proposal.reasoning,
            prompt_version=proposal.prompt_version,
            provider=proposal.provider,
            model=proposal.model,
            selected=proposal.payment_id in selected_ids,
        )


def _report(run: Any, *, session_id: str, db: str) -> None:
    """Print the batch result the track bar asks to see."""
    diagnosis = run.diagnosis
    plan = run.plan

    typer.echo(f"revenue at risk    : {run.revenue_at_risk}  ({run.considered} failed payments)")
    if diagnosis is not None:
        typer.echo(
            f"diagnosed          : {len(diagnosis.proposals)} in {diagnosis.calls} "
            f"model call(s)  [{diagnosis.prompt_version}]"
        )
        if diagnosis.rejected:
            typer.echo(f"  rejected entries : {len(diagnosis.rejected)}")
            for payment_id, why in diagnosis.rejected[:3]:
                typer.echo(f"      {payment_id}: {why}")
        if diagnosis.omitted:
            typer.echo(f"  omitted by model : {len(diagnosis.omitted)} (defaulted to do_nothing)")
            # Surface the reason, not just the count. An omission caused by a
            # missing fixture and one caused by a model declining to answer look
            # identical in a count, and mean completely different things.
            reasons = {
                p.reasoning
                for p in diagnosis.proposals
                if p.payment_id in set(diagnosis.omitted)
            }
            for reason in sorted(reasons)[:2]:
                typer.echo(f"      why: {reason[:160]}")

    if plan is not None:
        typer.echo(f"selected           : {len(plan.selected)}")
        typer.echo(f"  not worth chasing: {len(plan.not_worth_pursuing)}")
        typer.echo(f"  declined (budget): {len(plan.declined_for_budget)}")
        typer.echo(f"expected recovery  : {plan.total_expected_recovery}")

    if run.attempts:
        typer.echo("")
        typer.echo(f"executed           : {len(run.executed)}  ({run.requested_value} requested)")
        typer.echo(f"awaiting approval  : {len(run.pending)}  ({run.pending_value})")
        typer.echo(f"refused            : {len(run.refused)}")
        for code, count in sorted(run.refusals_by_layer().items()):
            typer.echo(f"  {code}: {count}")

        for attempt in run.executed[:5]:
            typer.echo(
                f"  {attempt.proposal.payment_id} -> {attempt.short_url or attempt.payment_link_id}"
            )

    typer.echo("")
    typer.echo(f"session            : {session_id}")
    typer.echo(f"verify with        : belay verify {db}")
