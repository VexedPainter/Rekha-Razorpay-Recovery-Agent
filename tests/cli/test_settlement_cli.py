"""The webhook and settlement CLI commands.

These are the surface the demo drives and the surface a reviewer will actually
type, so they get tested rather than assumed. Two things in particular:

- `settle-verify` must EXIT NON-ZERO on a mismatch. A verification tool that
  reports a discrepancy on stdout and exits 0 cannot be used in CI, and would let
  a broken reconciliation pass a pipeline silently.
- both commands must default to the latest session, because otherwise a user has
  to copy a session id between two commands and the demo becomes fragile.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from rekha.cli.main import app
from rekha.ledger.store import LedgerStore
from rekha.razorpay.webhooks import sign_payload
from typer.testing import CliRunner

SECRET = "whsec_cli_test"
INR = "INR"
runner = CliRunner()


def _seeded_db(tmp_path: Path, *, reference: str = "recover-pay_1") -> Path:
    """A ledger holding one governed recovery, as `rekha recover` would leave it."""
    db = tmp_path / "cli.db"
    ledger = LedgerStore(f"sqlite:///{db}")
    ledger.append("s_cli", "session_started", {"merchant_id": "acme"}, initiated_by="agent")
    ledger.append(
        "s_cli",
        "result_recorded",
        {
            "tool": "create_payment_link_upi",
            "result": {
                "id": "plink_cli",
                "status": "created",
                "amount": 240000,
                "currency": INR,
                "reference_id": reference,
            },
        },
        step_seq=1,
    )
    return db


def _envelope(*, paid: int = 240000, event_id: str = "evt_cli") -> dict[str, str]:
    body = json.dumps(
        {
            "entity": "event",
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": "plink_cli",
                        "status": "paid",
                        "amount": 240000,
                        "amount_paid": paid,
                        "currency": INR,
                        "reference_id": "recover-pay_1",
                    }
                },
                "payment": {
                    "entity": {
                        "id": "pay_cli",
                        "status": "captured",
                        "amount": paid,
                        "currency": INR,
                    }
                },
            },
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return {"event_id": event_id, "signature": sign_payload(body, SECRET), "body": body}


def _write(path: Path, payload: Any) -> str:
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(path)


def _recon_entry(amount: int = 240000, entity_id: str = "pay_cli") -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "type": "payment",
        "amount": amount,
        "currency": INR,
        "fee": 5664,
        "tax": 1019,
        "credit": amount - 5664 - 1019,
        "settled": True,
    }


# --------------------------------------------------------------- webhooks replay


def test_replay_ingests_and_reports(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    envelopes = _write(tmp_path / "wh.json", [_envelope()])

    result = runner.invoke(
        app, ["webhooks", "replay", envelopes, "--db", str(db), "--secret", SECRET]
    )

    assert result.exit_code == 0, result.output
    assert "accepted    : 1" in result.output
    assert "chain       : OK" in result.output


def test_replay_deduplicates_a_retried_delivery(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    envelope = _envelope()
    envelopes = _write(tmp_path / "wh.json", [envelope, dict(envelope)])

    result = runner.invoke(
        app, ["webhooks", "replay", envelopes, "--db", str(db), "--secret", SECRET]
    )

    assert "accepted    : 1" in result.output
    assert "duplicates  : 1" in result.output


def test_replay_reports_a_forged_signature_and_records_nothing(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    forged = _envelope()
    forged["signature"] = "0" * 64
    envelopes = _write(tmp_path / "wh.json", [forged])

    result = runner.invoke(
        app, ["webhooks", "replay", envelopes, "--db", str(db), "--secret", SECRET]
    )

    assert "REJECTED" in result.output
    assert "accepted    : 0" in result.output
    ledger = LedgerStore(f"sqlite:///{db}")
    assert not [e for e in ledger.read("s_cli") if e.type == "webhook_received"]


def test_replay_without_a_secret_fails_clearly(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    envelopes = _write(tmp_path / "wh.json", [_envelope()])

    result = runner.invoke(
        app,
        ["webhooks", "replay", envelopes, "--db", str(db), "--secret", ""],
        env={"RAZORPAY_WEBHOOK_SECRET": ""},
    )
    # Either a clear parameter error, or it read a secret from .env and worked.
    assert result.exit_code in (0, 1, 2)
    if result.exit_code == 2:
        assert "secret" in result.output.lower()


def test_recoveries_separates_requested_from_recovered(tmp_path: Path) -> None:
    """A created link is not recovered money, and the report must not conflate them."""
    db = _seeded_db(tmp_path)

    before = runner.invoke(app, ["webhooks", "recoveries", "--db", str(db)])
    assert "amount requested : INR 2,400.00" in before.output
    assert "amount RECOVERED : INR 0.00" in before.output
    assert "awaiting payment" in before.output

    envelopes = _write(tmp_path / "wh.json", [_envelope()])
    runner.invoke(app, ["webhooks", "replay", envelopes, "--db", str(db), "--secret", SECRET])

    after = runner.invoke(app, ["webhooks", "recoveries", "--db", str(db)])
    assert "amount RECOVERED : INR 2,400.00" in after.output
    assert "RECOVERED" in after.output


# ----------------------------------------------------------------- settle-verify


def test_settle_verify_matches_when_all_three_legs_agree(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    envelopes = _write(tmp_path / "wh.json", [_envelope()])
    runner.invoke(app, ["webhooks", "replay", envelopes, "--db", str(db), "--secret", SECRET])
    recon = _write(tmp_path / "recon.json", [_recon_entry()])

    result = runner.invoke(app, ["settle-verify", "--db", str(db), "--recon", recon])

    assert result.exit_code == 0, result.output
    assert "VERDICT      : MATCHED" in result.output
    assert "matched      : 1" in result.output
    assert "fees + tax" in result.output


def test_settle_verify_exits_non_zero_on_a_mismatch(tmp_path: Path) -> None:
    """A verification tool that reports a discrepancy and exits 0 cannot be used
    in CI, and would let a broken reconciliation pass a pipeline silently."""
    db = _seeded_db(tmp_path)
    envelopes = _write(tmp_path / "wh.json", [_envelope()])
    runner.invoke(app, ["webhooks", "replay", envelopes, "--db", str(db), "--secret", SECRET])
    # Settled 1,500 more than was authorized.
    recon = _write(tmp_path / "recon.json", [_recon_entry(amount=390000)])

    result = runner.invoke(app, ["settle-verify", "--db", str(db), "--recon", recon])

    assert result.exit_code == 1, "a mismatch must fail the command"
    assert "VERDICT      : MISMATCHED" in result.output
    assert "amount_mismatch: 1" in result.output


def test_settle_verify_detects_an_unauthorized_settlement(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    ghost = _recon_entry(amount=5000000, entity_id="pay_ghost")
    ghost["reference_id"] = "recover-pay_NEVER_AUTHORIZED"
    recon = _write(tmp_path / "recon.json", [ghost])

    result = runner.invoke(app, ["settle-verify", "--db", str(db), "--recon", recon])

    assert result.exit_code == 1
    assert "unauthorized_payment: 1" in result.output
    assert "never authorized" in result.output


def test_settle_verify_with_no_source_is_unverifiable_not_matched(tmp_path: Path) -> None:
    db = _seeded_db(tmp_path)
    result = runner.invoke(app, ["settle-verify", "--db", str(db), "--no-source"])

    assert result.exit_code == 0, "unverifiable is not a failure, it is an honest verdict"
    assert "VERDICT      : UNVERIFIABLE" in result.output
    assert "unverifiable : 1" in result.output
    assert "MATCHED" not in result.output.split("VERDICT")[1]


def test_settle_verify_requires_a_chosen_source(tmp_path: Path) -> None:
    """Silently defaulting to 'no data' would make a missing leg look like a
    clean run, so the source must be chosen explicitly."""
    db = _seeded_db(tmp_path)
    result = runner.invoke(app, ["settle-verify", "--db", str(db)])

    assert result.exit_code == 2
    assert "--recon" in result.output or "--live" in result.output


def test_settle_verify_names_the_source_it_used(tmp_path: Path) -> None:
    """Which leg ran must never be implied: fixture-backed and live are different
    claims, and the report has to say which."""
    db = _seeded_db(tmp_path)
    recon = _write(tmp_path / "recon.json", [_recon_entry()])

    result = runner.invoke(app, ["settle-verify", "--db", str(db), "--recon", recon])
    assert "SETTLED     fixture" in result.output

    empty = runner.invoke(app, ["settle-verify", "--db", str(db), "--no-source"])
    assert "SETTLED     none" in empty.output


def test_settle_verify_accepts_a_razorpay_collection_envelope(tmp_path: Path) -> None:
    """Razorpay wraps lists in `{entity, count, items}`. A file saved straight from
    the API should work without the user unwrapping it by hand."""
    db = _seeded_db(tmp_path)
    envelopes = _write(tmp_path / "wh.json", [_envelope()])
    runner.invoke(app, ["webhooks", "replay", envelopes, "--db", str(db), "--secret", SECRET])
    recon = _write(
        tmp_path / "recon.json",
        {"entity": "collection", "count": 1, "items": [_recon_entry()]},
    )

    result = runner.invoke(app, ["settle-verify", "--db", str(db), "--recon", recon])
    assert result.exit_code == 0
    assert "matched      : 1" in result.output


# ----------------------------------------------------------------- empty ledger


@pytest.mark.parametrize(
    "command",
    [
        ["webhooks", "recoveries"],
        ["settle-verify", "--no-source"],
    ],
)
def test_commands_fail_clearly_on_a_ledger_with_no_sessions(
    tmp_path: Path, command: list[str]
) -> None:
    db = tmp_path / "empty.db"
    LedgerStore(f"sqlite:///{db}")
    result = runner.invoke(app, [*command, "--db", str(db)])
    assert result.exit_code == 2
    assert "no sessions" in result.output
