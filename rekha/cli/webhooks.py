"""`rekha webhooks` -- ingest and replay Razorpay webhooks.

There is no HTTP server here, deliberately. A public endpoint means a tunnel, a
hostname, and a demo that fails when someone's network does. Saved payloads
replayed through the real verifier produce identical evidence with none of that:
the signature check, the deduplication, and the ledger append are the same code a
live delivery would take.

The file format is a JSON array of delivery envelopes::

    [{"event_id": "evt_...", "signature": "<hex>", "body": "<raw json string>"}]

`body` is the exact raw string that was signed. Storing the raw string rather than
a parsed object matters: HMAC is over bytes, so re-serializing a parsed object
would change whitespace and invalidate a genuine signature.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer


def register(app: typer.Typer) -> None:
    """Attach the `webhooks` command group to the CLI app."""
    webhooks_app = typer.Typer(
        help="Ingest Razorpay webhooks: verify signatures, deduplicate, record.",
        no_args_is_help=True,
    )
    app.add_typer(webhooks_app, name="webhooks")

    @webhooks_app.command("replay")
    def replay(
        path: str = typer.Argument(..., help="JSON file of webhook delivery envelopes."),
        db: str = typer.Option("recovery.db", "--db", help="Ledger to append to."),
        session: str = typer.Option(
            "", "--session", help="Session id to record under. Defaults to the "
            "session the recovery actions were recorded in, so correlation works."
        ),
        secret: str = typer.Option(
            "", "--secret", help="Webhook secret. Defaults to RAZORPAY_WEBHOOK_SECRET."
        ),
    ) -> None:
        """Replay saved webhooks through the real verifier into the ledger."""
        from recovery.providers import load_env

        from rekha.ledger.store import LedgerStore
        from rekha.ledger.verify import verify_chain
        from rekha.razorpay.webhooks import ingest

        env = load_env()
        resolved_secret = secret or env.get("RAZORPAY_WEBHOOK_SECRET", "")
        if not resolved_secret:
            raise typer.BadParameter(
                "no webhook secret: pass --secret or set RAZORPAY_WEBHOOK_SECRET in .env"
            )

        envelopes = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(envelopes, list):
            raise typer.BadParameter(f"{path} must contain a JSON array of envelopes")

        ledger = LedgerStore(f"sqlite:///{db}")
        target = session or _latest_session(ledger)
        if not target:
            raise typer.BadParameter(
                f"{db} has no sessions to attach webhooks to -- run `rekha recover` first"
            )

        accepted = duplicates = unknown = rejected = 0
        for envelope in envelopes:
            body = envelope.get("body", "")
            try:
                result = ingest(
                    ledger,
                    target,
                    body,
                    envelope.get("signature", ""),
                    resolved_secret,
                    event_id=envelope.get("event_id", ""),
                )
            except Exception as exc:
                rejected += 1
                typer.echo(f"  REJECTED  {getattr(exc, 'code', type(exc).__name__)}")
                continue

            if result.duplicate:
                duplicates += 1
                continue
            accepted += 1
            if result.unknown_event:
                unknown += 1
            fact = result.fact
            if fact is not None:
                paid = f" paid={fact.amount_paid}" if fact.amount_paid else ""
                typer.echo(f"  {fact.event:<28} {fact.payment_link_id or '-'}{paid}")

        typer.echo("")
        typer.echo(f"accepted    : {accepted}")
        typer.echo(f"duplicates  : {duplicates}  (retries change nothing)")
        if unknown:
            typer.echo(f"unknown     : {unknown}  (verified and recorded, not modelled)")
        if rejected:
            typer.echo(f"REJECTED    : {rejected}  (signature did not verify)")
        typer.echo(f"session     : {target}")
        chain = verify_chain(ledger.read(target))
        typer.echo(f"chain       : {'OK' if chain.ok else 'BROKEN'}")

    @webhooks_app.command("recoveries")
    def recoveries(
        db: str = typer.Option("recovery.db", "--db", help="Ledger to read."),
        session: str = typer.Option("", "--session", help="Session id. Default: latest."),
    ) -> None:
        """Show recovery actions matched against the webhooks that followed.

        This is the "measured money recovered" view: what was authorized on our
        side, against what Razorpay says the customer actually paid.
        """
        from rekha.finance.money import total as sum_money
        from rekha.ledger.store import LedgerStore
        from rekha.razorpay.webhooks import correlate_recoveries

        ledger = LedgerStore(f"sqlite:///{db}")
        target = session or _latest_session(ledger)
        if not target:
            raise typer.BadParameter(f"{db} has no sessions")

        matched = correlate_recoveries(ledger.read(target))
        if not matched:
            typer.echo("no recovery actions executed in this session")
            return

        currency = matched[0].authorized.currency
        for recovery in matched:
            state = (
                "RECOVERED" if recovery.is_fully_recovered
                else "partial" if recovery.is_recovered
                else "awaiting payment"
            )
            typer.echo(
                f"  {recovery.payment_link_id}  authorized={recovery.authorized}  "
                f"paid={recovery.paid}  {state}"
            )

        authorized_total = sum_money([r.authorized for r in matched], currency=currency)
        recovered_total = sum_money([r.paid for r in matched], currency=currency)
        typer.echo("")
        typer.echo(f"links created    : {len(matched)}")
        typer.echo(f"amount requested : {authorized_total}")
        typer.echo(f"amount RECOVERED : {recovered_total}")
        typer.echo(f"recovered count  : {sum(1 for r in matched if r.is_recovered)}")


def _latest_session(ledger: Any) -> str:
    """The most recently started session in a ledger.

    Webhooks must land in the same session as the actions they concern, or
    correlation finds nothing. Defaulting to the latest session is what makes
    `rekha recover` then `rekha webhooks replay` work without the user having to
    copy a session id between two commands.
    """
    sessions = [
        event.session_id for event in ledger.read_by_types(["session_started"])
    ]
    return sessions[-1] if sessions else ""
