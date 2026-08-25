"""`belay settle-verify` -- reconcile authorized, reported, and settled.

The answer to "did the money that actually moved match what we authorized?",
computed from three independently-sourced records.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer


def register(app: typer.Typer) -> None:
    """Attach the `settle-verify` command to the CLI app."""

    @app.command(name="settle-verify")
    def settle_verify(
        db: str = typer.Option("recovery.db", "--db", help="Ledger to verify."),
        session: str = typer.Option("", "--session", help="Session id. Default: latest."),
        recon: str = typer.Option(
            "",
            "--recon",
            help="JSON file of settlement reconciliation entries (the SETTLED leg). "
            "Omit to fetch from Razorpay, or pass --no-source to prove that a "
            "missing leg reports `unverifiable` rather than success.",
        ),
        live: bool = typer.Option(
            False, "--live", help="Fetch the SETTLED leg from the real Razorpay API."
        ),
        no_source: bool = typer.Option(
            False, "--no-source", help="Run with no settlement data at all."
        ),
        month: int = typer.Option(0, "--month", help="Month for --live. Default: now."),
        year: int = typer.Option(0, "--year", help="Year for --live. Default: now."),
    ) -> None:
        """Verify that settled money matches what the control plane authorized."""
        from belay.ledger.store import LedgerStore
        from belay.settlement.verify import (
            EmptySettlementSource,
            FixtureSettlementSource,
            SettlementResult,
            SettlementSource,
            verify,
        )

        ledger = LedgerStore(f"sqlite:///{db}")
        target = session or _latest_session(ledger)
        if not target:
            raise typer.BadParameter(f"{db} has no sessions -- run `belay recover` first")

        source: SettlementSource
        note = ""
        if no_source:
            source = EmptySettlementSource()
            note = "no settlement data supplied, on purpose"
        elif recon:
            entries = json.loads(Path(recon).read_text(encoding="utf-8"))
            if isinstance(entries, dict):
                entries = entries.get("items", [])
            source = FixtureSettlementSource(list(entries))
            note = f"{len(entries)} entries from {recon}"
        elif live:
            from datetime import UTC, datetime

            from recovery.providers import load_env

            from belay.settlement.live import LiveSettlementSource

            env = load_env()
            now = datetime.now(UTC)
            live_source = LiveSettlementSource(
                env.get("RAZORPAY_KEY_ID", ""),
                env.get("RAZORPAY_KEY_SECRET", ""),
                year=year or now.year,
                month=month or now.month,
            )
            fetched = live_source.recon_entries()
            note = f"{len(fetched)} entries from the Razorpay API"
            if live_source.last_error:
                note += f" ({live_source.last_error[:80]})"
            source = FixtureSettlementSource(fetched) if fetched else EmptySettlementSource()
        else:
            raise typer.BadParameter(
                "choose a SETTLED leg: --recon <file>, --live, or --no-source"
            )

        report = verify(ledger.read(target), source, currency="INR")

        typer.echo("THREE-WAY SETTLEMENT VERIFICATION")
        typer.echo("  AUTHORIZED  our hash-chained ledger")
        typer.echo("  REPORTED    Razorpay webhooks")
        typer.echo(f"  SETTLED     {report.source}  ({note})")
        typer.echo("")

        for case in report.cases:
            marker = {
                SettlementResult.MATCHED: "  [match]     ",
                SettlementResult.MISMATCHED: "  [MISMATCH]  ",
                SettlementResult.PENDING: "  [pending]   ",
                SettlementResult.UNVERIFIABLE: "  [unverif.]  ",
            }[case.result]
            label = case.reason.value if case.reason else case.result.value
            typer.echo(f"{marker}{case.reference_id:<28} {label}")
            typer.echo(f"                {case.detail}")

        typer.echo("")
        typer.echo(f"matched      : {len(report.matched)}")
        typer.echo(f"MISMATCHED   : {len(report.mismatched)}")
        for reason, count in sorted(report.mismatches_by_reason().items()):
            typer.echo(f"    {reason}: {count}")
        typer.echo(f"pending      : {len(report.pending)}")
        typer.echo(f"unverifiable : {len(report.unverifiable)}")
        if report.unrelated:
            typer.echo(
                f"unrelated    : {report.unrelated}  (organic merchant traffic, not judged)"
            )
        typer.echo("")
        typer.echo(f"authorized   : {report.authorized_total}")
        typer.echo(f"settled      : {report.settled_total}")
        typer.echo(f"fees + tax   : {report.fees_total}")
        typer.echo("")
        typer.echo(f"VERDICT      : {report.verdict.value.upper()}")

        if report.verdict is SettlementResult.MISMATCHED:
            raise typer.Exit(code=1)


def _latest_session(ledger: object) -> str:
    sessions = [
        event.session_id
        for event in ledger.read_by_types(["session_started"])  # type: ignore[attr-defined]
    ]
    return sessions[-1] if sessions else ""
