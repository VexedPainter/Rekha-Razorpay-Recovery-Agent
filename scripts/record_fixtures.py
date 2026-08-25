"""Generate diagnosis fixtures so the default test suite runs offline.

Two modes, and the difference matters for honesty:

  `--from-provider`  Calls a real LLM (Gemini/Groq/Anthropic) and records exactly
                     what it returned. These are REAL model outputs. This is what
                     should back the demo and what the repo should ship where
                     possible.

  `--synthetic`      Writes plausible outputs derived from each payment's error
                     fields by a rule table. NOT model output, and never presented
                     as such: the fixture records `"provider": "synthetic"` so
                     nothing downstream can mistake it for reasoning that happened.
                     It exists so CI, tests, and a demo on a machine with no API
                     key still exercise the full code path.

The distinction is recorded in the fixture files themselves rather than in a
comment here, because a fixture that cannot say where it came from will
eventually be described as something it is not.

    python scripts/record_fixtures.py --synthetic
    python scripts/record_fixtures.py --from-provider gemini
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "examples" / "razorpay-sandbox"))

from cohort import generate_cohort  # noqa: E402
from recovery.agent import derive_now_epoch  # noqa: E402
from recovery.diagnose import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_PROMPT,
    RESPONSE_SCHEMA,
    _snapshot_for_prompt,
    load_prompt,
)
from recovery.proposal import PaymentSnapshot  # noqa: E402
from recovery.providers import ReplayProvider, resolve_provider  # noqa: E402
from recovery.providers.base import load_env  # noqa: E402

FIXTURES = REPO_ROOT / "recovery" / "fixtures"
NOW_EPOCH = 1788000000

# ---------------------------------------------------------------- synthetic rules
#
# A deterministic reading of Razorpay's real error taxonomy. This is NOT a
# substitute for the AI -- if it were good enough, the project would not need a
# model. It is deliberately cruder than the model's job: it maps error_reason and
# error_step to a cause, and applies flat recovery odds per cause with a simple
# age decay. It cannot weigh a batch, spot a bank outage across eleven payments,
# notice an injected instruction, or explain itself in language a merchant reads.
#
# It exists so the pipeline is testable without a key.

_CAUSE_BY_REASON: dict[str, str] = {
    "insufficient_funds": "insufficient_funds",
    "payment_cancelled": "customer_recoverable",
    "invalid_card": "method_unsupported",
}

_CAUSE_BY_STEP: dict[str, str] = {
    "payment_authentication": "authentication_failed",
    "payment_authorization": "bank_transient",
    "payment_initiation": "method_unsupported",
}

#: Flat recovery odds per cause, as a percentage. Round numbers on purpose: these
#: are not calibrated estimates, and pretending otherwise by using 63% would be
#: worse than admitting they are placeholders.
_ODDS: dict[str, int] = {
    "customer_recoverable": 55,
    "bank_transient": 60,
    "authentication_failed": 45,
    "insufficient_funds": 25,
    "method_unsupported": 30,
    "permanently_dead": 0,
}

_DIAGNOSIS: dict[str, str] = {
    "customer_recoverable": "The customer started the payment and left before finishing.",
    "bank_transient": "The bank or gateway failed temporarily; the payment itself was fine.",
    "authentication_failed": "The customer could not complete the OTP or 3D Secure step.",
    "insufficient_funds": "The customer's account did not have enough balance at the time.",
    "method_unsupported": "The payment method cannot be used -- it has expired or is restricted.",
    "permanently_dead": "This payment was blocked for risk reasons and will not go through.",
}


def _classify(payment: dict[str, object]) -> str:
    description = str(payment.get("error_description", "")).lower()
    if "high risk" in description or "blocked" in description:
        return "permanently_dead"
    reason = str(payment.get("error_reason", ""))
    if reason in _CAUSE_BY_REASON:
        return _CAUSE_BY_REASON[reason]
    if "timeout" in description or "gateway" in description:
        return "bank_transient"
    return _CAUSE_BY_STEP.get(str(payment.get("error_step", "")), "customer_recoverable")


def _synthetic_entry(snapshot: PaymentSnapshot) -> dict[str, object]:
    cause = _classify(
        {
            "error_description": snapshot.error_description,
            "error_reason": snapshot.error_reason,
            "error_step": snapshot.error_step,
        }
    )
    odds = _ODDS[cause]

    # Older failures are less recoverable: the customer has moved on. Linear decay
    # to a floor at three weeks, which is the cohort's age range.
    decay = max(0.3, 1.0 - (snapshot.age_hours / (24 * 21)) * 0.7)
    if not snapshot.contact_present:
        odds = 0  # unreachable, whatever the cause

    if cause == "permanently_dead" or odds == 0:
        strategy = "do_nothing"
        expected = 0
    else:
        # Prefer UPI unless the customer needs a choice of instrument, mirroring
        # the prompt's guidance so fixture-backed runs behave like real ones.
        strategy = (
            "payment_link"
            if cause == "method_unsupported" or snapshot.amount.minor_units > 500000
            else "upi_link"
        )
        expected = int(snapshot.amount.minor_units * odds / 100 * decay)

    route = (
        "not worth chasing"
        if strategy == "do_nothing"
        else f"a {strategy} is the best route"
    )
    return {
        "payment_id": snapshot.payment_id,
        "cause_class": cause,
        "strategy": strategy,
        "amount_paise": snapshot.amount.minor_units,
        "expected_recovery_paise": expected,
        "confidence": "medium",
        "diagnosis": _DIAGNOSIS[cause],
        "reasoning": f"Classified as {cause} from the failure detail; {route}.",
        "follow_up": _synthetic_follow_up(cause, strategy),
    }


#: Rule-derived follow-up plans, one per cause. Crude by design and in exactly the
#: way that shows what the model contributes: these delays are fixed per cause,
#: whereas the model reads the individual payment -- it can tell that an
#: insufficient-funds failure on the 28th should wait for payday while one on the
#: 2nd should not, which a table keyed only on cause cannot express.
_FOLLOW_UP: dict[str, list[tuple[str, int, str]]] = {
    "customer_recoverable": [
        ("remind", 12, "A nudge often converts an abandoned attempt."),
        ("do_nothing", 48, "Two touches is enough for an abandoned checkout."),
    ],
    "bank_transient": [
        ("remind", 6, "The outage has likely cleared; nudge the existing link."),
        ("payment_link", 24, "Offer other instruments if the original still fails."),
        ("do_nothing", 48, "Stop after offering an alternative."),
    ],
    "authentication_failed": [
        ("remind", 12, "The customer may complete authentication on a second try."),
        ("do_nothing", 48, "Stop rather than push a failing instrument."),
    ],
    "insufficient_funds": [
        ("remind", 72, "Three days allows a balance to recover."),
        ("do_nothing", 96, "Stop rather than repeatedly ask for money they lack."),
    ],
    "method_unsupported": [
        ("remind", 24, "The link already offers alternative methods."),
        ("do_nothing", 48, "Stop; the original instrument cannot work."),
    ],
    "permanently_dead": [],
}


def _synthetic_follow_up(cause: str, strategy: str) -> list[dict[str, object]]:
    if strategy == "do_nothing":
        return []
    return [
        {"strategy": name, "wait_hours": hours, "rationale": why}
        for name, hours, why in _FOLLOW_UP.get(cause, [])
    ]


def _snapshots(count: int) -> list[PaymentSnapshot]:
    # Same anchor the agent derives at runtime, so a recorded fixture matches the
    # request the agent will actually make. See recovery/agent.py::derive_now_epoch.
    payments = generate_cohort(count)
    anchor = derive_now_epoch(payments)
    return [PaymentSnapshot.from_razorpay(p, now_epoch=anchor) for p in payments]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=200, help="cohort size to cover")
    parser.add_argument(
        "--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="payments per request"
    )
    parser.add_argument(
        "--prompt", default=DEFAULT_PROMPT, help="prompt name in recovery/prompts/"
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=8192,
        help=(
            "output budget per request. Groq's free tier counts this against its "
            "8000 tokens-per-minute limit, so a long prompt needs a smaller value "
            "there -- 8192 requested plus a 2.5k prompt is rejected before the "
            "model sees it."
        ),
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "skip batches whose fixture already exists. Matters on a metered free "
            "tier: without it, retrying after a failure at batch 15 re-requests the "
            "14 that already succeeded and can exhaust a daily quota on work "
            "already done."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--synthetic", action="store_true", help="rule-derived, no API call")
    group.add_argument(
        "--from-provider",
        metavar="NAME",
        help="record real model output (gemini, groq, anthropic)",
    )
    args = parser.parse_args()

    system, prompt_version = load_prompt(args.prompt)
    snapshots = _snapshots(args.count)
    FIXTURES.mkdir(parents=True, exist_ok=True)

    if args.from_provider:
        env = load_env()
        provider = resolve_provider(args.from_provider, env=env)
        recorder = ReplayProvider(FIXTURES, model=provider.model, live=provider)
        print(f"recording real output from {provider.name}/{provider.model}")
    else:
        provider = None
        recorder = None
        print("writing synthetic fixtures (rule-derived, marked as such)")

    written = 0
    skipped = 0
    failed: list[tuple[int, str]] = []
    from belay.canonical import canonical_bytes, sha256_hex

    for start in range(0, len(snapshots), args.batch_size):
        chunk = snapshots[start : start + args.batch_size]
        batch_no = start // args.batch_size + 1
        user = json.dumps(
            {"payments": [_snapshot_for_prompt(s) for s in chunk]},
            indent=2,
            ensure_ascii=False,
        )
        key = sha256_hex(canonical_bytes({"system": system, "user": user}))[:20]

        if args.resume and (FIXTURES / f"{key}.json").exists():
            skipped += 1
            print(f"  batch {batch_no}: already recorded, skipping")
            continue

        if recorder is not None:
            try:
                recorder.complete_json(
                    system=system, user=user, schema=RESPONSE_SCHEMA, max_tokens=args.max_tokens
                )
            except Exception as exc:
                # Keep going rather than aborting. On a metered free tier a single
                # rate-limit response should not discard the batches that would
                # have succeeded after it, and `--resume` can fill the gaps later.
                failed.append((batch_no, f"{type(exc).__name__}: {str(exc)[:120]}"))
                print(f"  batch {batch_no}: FAILED -- {type(exc).__name__}")
                continue
            written += 1
            print(f"  batch {batch_no}: recorded {len(chunk)} payments -> {key}.json")
            continue

        (FIXTURES / f"{key}.json").write_text(
            json.dumps(
                {
                    # Named so nothing downstream can mistake this for model
                    # output. A fixture that cannot say where it came from will
                    # eventually be described as something it is not.
                    "provider": "synthetic",
                    "model": "rule-derived (scripts/record_fixtures.py --synthetic)",
                    "prompt_version": prompt_version,
                    "system": system,
                    "user": user,
                    "response": {
                        "proposals": [_synthetic_entry(s) for s in chunk]
                    },
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        written += 1
        print(f"  batch {batch_no}: {len(chunk)} payments -> {key}.json")

    print(f"\n{written} fixture file(s) written to {FIXTURES.relative_to(REPO_ROOT)}")
    if skipped:
        print(f"{skipped} batch(es) already present and skipped")
    print(f"prompt: {prompt_version}")
    if failed:
        print(f"\n{len(failed)} batch(es) FAILED:")
        for batch_no, detail in failed:
            print(f"  batch {batch_no}: {detail}")
        print("Re-run with --resume to retry only these, or use a different provider.")
    if not args.from_provider:
        print(
            "\nThese are SYNTHETIC. Re-record with --from-provider gemini once a key\n"
            "is available, so the repo ships real model reasoning."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
