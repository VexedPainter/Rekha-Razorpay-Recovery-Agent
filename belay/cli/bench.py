"""`belay bench` -- run the adversarial suite and print the metrics table.

Every metric is a fold over the hash-chained ledger or over scenario outcomes, so
the measurements are as verifiable as the actions they measure.
"""

from __future__ import annotations

import typer


def register(app: typer.Typer) -> None:
    """Attach the `bench` command group."""
    bench_app = typer.Typer(
        help="Adversarial scenarios and measured metrics.", no_args_is_help=True
    )
    app.add_typer(bench_app, name="bench")

    @bench_app.command("run")
    def run(
        strict: bool = typer.Option(
            True,
            "--strict/--no-strict",
            help="Exit non-zero if any scenario fails. On by default: a suite that "
            "reports a breach and exits 0 cannot gate anything.",
        ),
    ) -> None:
        """Run every adversarial scenario and report which layer caught each one."""
        import anyio
        from bench.attacks import run_all

        report = anyio.run(run_all)

        typer.echo("ADVERSARIAL SCENARIOS")
        typer.echo("")
        for result in report.results:
            if result.expected_blocked:
                mark = "BLOCKED " if result.blocked else "*ESCAPED*"
            else:
                mark = "ALLOWED " if not result.blocked else "*FALSE +*"
            typer.echo(f"  [{mark}] {result.name}")
            typer.echo(f"             attempt : {result.attempted}")
            typer.echo(f"             caught by: {result.layer}")
            typer.echo(f"             evidence : {result.evidence}")
            if result.detail:
                typer.echo(f"             note    : {result.detail}")
            typer.echo("")

        adversarial = report.adversarial
        blocked = sum(1 for r in adversarial if r.blocked)
        typer.echo("SAFETY METRICS")
        typer.echo(f"  unauthorized actions attempted : {len(adversarial)}")
        typer.echo(f"  blocked                        : {blocked}")
        typer.echo(f"  block rate                     : {report.block_rate:.0%}")
        fp = report.false_positive_rate
        typer.echo(
            f"  false-positive rate            : "
            f"{'n/a (no benign cohort)' if fp != fp else f'{fp:.0%}'}"
        )
        typer.echo("")
        typer.echo(
            "  A block rate without a false-positive rate is uninterpretable:"
        )
        typer.echo(
            "  a system that refuses everything scores 100%. The benign cohort is"
        )
        typer.echo("  what makes the number above mean something.")
        typer.echo("")
        typer.echo(f"  scenarios passed               : {report.passed}/{len(report.results)}")

        if report.failed:
            typer.echo("")
            for failure in report.failed:
                typer.echo(f"  FAILED: {failure.name} -- {failure.detail or failure.layer}")
            if strict:
                raise typer.Exit(code=1)

    @bench_app.command("metrics")
    def metrics(
        db: str = typer.Option("recovery.db", "--db", help="Ledger to measure."),
        session: str = typer.Option("", "--session", help="Session id. Default: latest."),
    ) -> None:
        """Revenue, safety and engineering metrics, folded from one ledger."""
        from bench.metrics import measure, render

        typer.echo(render(measure(db, session or None)))
