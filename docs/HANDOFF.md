# HANDOFF — resume state

Paste `RESUME_PROMPT` (bottom of this file) into a fresh session to continue.
`AGENTS.md` carries the standing engineering rules and is auto-loaded by agent
sessions; this file carries the *current position*.

Last updated: 2026-08-25, after Phase 5.

---

## The project

An **AI revenue recovery agent for Razorpay with a deterministic financial
control plane**, for the Razorpay AI Buildathon, **Track 03 — AI Revenue
Recovery**.

An LLM inspects failed payments, diagnoses why they failed, and proposes a
recovery action. A deterministic control plane decides whether that action is
permitted, whether a human must approve it, executes at most once, and
afterwards proves from settlement data that the money which moved is the money
that was authorized.

```
Intent -> Authorization -> Execution -> Actual Outcome -> Verification
```

This repository was a general AI-agent tool-safety proxy (`belay-mcp`). It was
retargeted on 2026-08-25; see `docs/adr/0028-retargeting-to-financial-control.md`.
Pre-retarget state is preserved at tag `v0.2.0a1-agent-gate`.

## Hard constraints

- **Application deadline: 2026-09-05.** Submission is a public repo + a
  **5-minute pitch video** + an architecture walkthrough to a panel. Not a live
  demo — it is recorded, so it can be retaken.
- Apply at `forms.gle/d9r2gvxp8cmoZhon9`. Internship is in-person, Bangalore,
  from September, 6 or 12 months, Rs 75,000/month.
- Judged on architecture, code quality, and the ability to explain the build.

## The official track bar (verbatim from razorpay.com/buildathon)

> Build an agent that detects revenue at risk, determines the right
> intervention, and executes a bounded recovery workflow.
> Example directions: **Payment degradation -> root cause -> recovery action**,
> checkout drop-off recovery, failed-subscription recovery, ...
> **The bar: Don't just identify the problem. Show measured money recovered
> across a batch, with compliant escalation, stopping rules, and an audit
> trail.**

Four requirements, and status:

| Requirement | Status |
| --- | --- |
| compliant escalation | DONE — mandate approval threshold, human-in-the-loop |
| stopping rules | DONE — Phase 4 cumulative + velocity limits |
| audit trail | DONE — hash-chained ledger + Ed25519 signed evidence |
| **measured money recovered across a batch** | **NOT DONE — Phases 5-7. Hard requirement, not optional.** |

## Where it lives / how to run

```
C:\Users\Pc\Desktop\Razorpay_Hackathon
```

Local only, never pushed. `.git` is intact (history is a pitch asset). Repo-local
git identity is set to `Jairo Gelpi <gelpierreape@gmail.com>`; global config
untouched.

Always use the venv Python (project needs 3.12; system Python is 3.11):

```powershell
cd C:\Users\Pc\Desktop\Razorpay_Hackathon
.\.venv\Scripts\python.exe -m pytest -q --no-cov          # fast suite
.\.venv\Scripts\python.exe -m pytest -m slow --no-cov     # subprocess suite
.\.venv\Scripts\python.exe examples\demo_fanout.py        # the fan-out demo
.\.venv\Scripts\python.exe examples\demo.py --oops        # control-plane demo
.\.venv\Scripts\belay-conformance.exe run --target belay --level 3
.\.venv\Scripts\python.exe scripts\traceability.py --check
.\.venv\Scripts\ruff.exe check . ; .\.venv\Scripts\mypy.exe belay
```

Some slow tests shell out to `belay-conformance`, so put
`.\.venv\Scripts` on PATH before running the slow suite.

## Verified state at last commit (db5b58b)

| | |
| --- | --- |
| Tests | **576 pass / 0 fail**, + 22 slow |
| Branch coverage | 84.13% (CI floor 83%, upward-only) — measures `belay/` AND `recovery/` |
| L3 conformance | PASSED |
| Spec MUSTs | 31, all covered, CI-enforced |
| ruff / mypy | clean |
| Python LOC | ~21,000 (31,896 pre-strip; 15,551 immediately after the Phase 1 strip) |

Two end-to-end runs, both offline with no credentials:

```
belay recover
  revenue at risk   : INR 655,134.00  (200 failed payments)
  diagnosed         : 200 in 8 model calls  [diagnose_v1@780066f4]
  selected          : 14   not worth chasing: 41   declined (budget): 145
  executed          : 2  (INR 3,029.00)     <- under the Rs 2,500 threshold
  awaiting approval : 12 (INR 46,907.00)    <- above it
  chain: OK  coherence: OK

python examples/demo_fanout.py
  25 links at exactly INR 50,000, 26th refused, INR 30,000 prevented
```

## Architecture

| Package | Responsibility | Determinism |
| --- | --- | --- |
| `belay/` | control plane: contracts, planning, policy, approvals, idempotent execution, ledger, rewind | deterministic |
| `belay/finance/` | `Money` (integer paise), `MerchantMandate` | deterministic |
| `belay/policy/cumulative.py` | cumulative spend + velocity fold | deterministic, pure |
| `belay/razorpay/` | webhook ingestion, preflight (EMPTY — Phase 7) | deterministic |
| `belay/settlement/` | three-way verification (EMPTY — Phase 8) | deterministic, pure |
| `recovery/` | AI diagnosis, strategy, prioritization | **non-deterministic** |
| `bench/` | adversarial scenarios + metrics (EMPTY — Phase 10) | deterministic |
| `conformance/` | L1/L2/L3 target-agnostic suite | deterministic |

**The load-bearing invariant.** `recovery/` is the only package allowed to call
an LLM, and may not import `belay.ledger`, `belay.approvals`, `belay.policy`,
`belay.executor`, `belay.settlement`, or `belay.finance.mandate`. Enforced by AST
in `tests/test_layer_boundaries.py`, which includes a negative case pinning the
detector. The AI proposes; the control plane authorizes.

`belay.finance.money` is deliberately allowed: the line is capability vs value
type. A mandate is authority; `Money` is arithmetic. Forbidding it would push raw
ints across the boundary and have the control plane infer their units.

## Phases done

**Phase 1 (6b1808d) — strip.** Deleted `belay/hooks`, `belay/supervisor`, the
editor-onboarding CLI surface, coding-agent tooling (export-pr, explore, causal,
learn), packaging/release machinery. 31,896 -> 15,551 LOC. `main.py` 3,394 ->
1,020 lines. The 4 pre-existing Windows test failures vanished (they lived in the
deleted modules). Coverage *rose* 82.15% -> 83.95%. Scaffolded the five new
packages + the layer-boundary test. ADR 0028.

**Phase 2 (223231a) — financial domain model.** `Money` as integer minor units,
no `__float__`, currency mismatch raises, human YAML (`{major: "5000.00"}`)
parsed via `Decimal`, excess precision refused rather than rounded.
`MerchantMandate` replaces `IntentContract`, reusing its hash-pinning; checked
*before* contracts/planning/policy. Incoherent mandates refused at load time.
`Lifecycle.action_describer` reads `(amount, method)` from args — injected, never
defaulted to a guess, fails closed. Error registry split into `SPEC_ERROR_CODES`
(the normative 17, closed) + `FINANCE_ERROR_CODES` (additive), so the "17 codes"
test still means what it meant.

**Phase 3 (98f1f0c) — sandbox + contract pack.** `examples/razorpay-sandbox/`:
real FastMCP stdio server, 17 tools, seeded 200 failed payments, **INR 6,55,134
at risk**, realistic Razorpay error taxonomy, 37 payments deliberately above the
per-action ceiling, prompt-injection planted in one `notes` field.
`packs/razorpay/contracts.yaml`: 13 contracts. Payment links are **conditional**
(undo = cancel, condition `$result.status != 'paid'`) because a paid link
genuinely cannot be cancelled. `create_refund` is declared and *fully working* on
purpose — the mandate is what refuses it, which is what makes the injection
scenario prove something about authorization. Added `Effect.amount_from` so a
variable amount flows from args into the plan (without it, aggregate limits are
impossible). `packs/razorpay/policy.yaml` (operator backstop) and
`examples/mandates/merchant.yaml` (merchant grant) — two documents, two
keyholders, both must pass.

**Phase 4 (6241b45) — cumulative + velocity limits.** `belay/policy/cumulative.py`
`fold_authorized_actions` (pure) + `CumulativeTracker`. Closed `Cap.per: session`,
which was declared in the model and **never read by the engine** for the life of
the project. Refactored `QuotaTracker` to share the fold. `per: session` with
`max_count`/`max_recipients` is now a load-time error (no unambiguous aggregate
meaning). Mandate `max_cumulative` + `max_actions_per_window` enforced in the
lifecycle, scoped per merchant across sessions. Indexed `EventRow.type` +
`LedgerStore.read_by_types`. `examples/demo_fanout.py` proves it.

**Phase 5 (db5b58b) — AI recovery layer.** `recovery/proposal.py` (RecoveryProposal,
strict, `clamped()` bounds expected recovery at the original amount),
`recovery/diagnose.py` (batched 25/call: 200 payments in 8 requests, so a free tier
suffices; every input payment gets exactly one proposal; malformed output rejected
wholesale, never patched), `recovery/prioritize.py` (deterministic ranking and
budget packing — the model scores, code decides), `recovery/agent.py` (the loop;
distinguishes executed / pending_approval / refused; `refusals_by_layer()` names
which control fired), `recovery/providers/` (Gemini free / Groq free / Anthropic
paid / Replay fixtures, raw HTTP via already-present httpx, no vendor SDKs),
`belay/cli/recover.py` (`belay recover`, computes the budget the agent may not
read), `scripts/record_fixtures.py`.

**NO PAID API KEY IS REQUIRED.** Provider selection prefers free tiers and falls
back to recorded fixtures. `recovery/fixtures/` is currently SYNTHETIC
(rule-derived, stamped `"provider": "synthetic"` in each file so it cannot be
mistaken for model output). Re-record real reasoning with
`python scripts/record_fixtures.py --from-provider gemini` once a key exists
(`GEMINI_API_KEY` in `.env`, free at aistudio.google.com, no card). **The README
must state which fixtures are real.**

## Real bugs found and fixed — do not reintroduce

1. **`LedgerStore` ignored its clock** — stamped `datetime.now(UTC)` while
   window limits compared that against an injected `Clock.now()`. Two time
   sources for one comparison. `tests/policy/test_quota.py` had a workaround
   that rewrote `at` after the fact, breaking the hash chain. `LedgerStore` now
   takes a `Clock`.
2. **Reads would have paused 200 times** — read effects declared no `count`, so
   they landed in `unknown[]` and spec §6.3 worst-casing beat the `fetch_*: allow`
   tool rule (most-restrictive wins across dimensions). Read counts are now
   declared.
3. **`approval_threshold` was dead config** — declared in Phase 2, never
   enforced. Found by a test expecting a pause that got an execution.
4. **A demo reported paused actions as successes** — `govern_and_execute` returns
   a `pending_approval` dict rather than raising, and the demo treated it as a
   result. Conflating executed / paused / refused is how a caller believes money
   moved when it did not. Both demo and tests now distinguish all three.
5. **Wall-clock made fixtures stale within the hour** — payment `age_hours` was
   computed from `time.time()`, so every diagnosis request was unique and no
   recorded fixture ever matched. `recovery/agent.py::derive_now_epoch` anchors to
   the newest payment in the batch instead: reproducible, and more defensible
   since the agent reasons about a snapshot.
6. **PowerShell backtick escaping corrupted two files** during editing (a lone
   `\r` into `pyproject.toml`, a literal `` `n `` into a test). Use the file-edit
   tool for multi-line changes; do not use PowerShell regex with backticks.

## Environment findings

- **Razorpay test keys work.** `.env` is populated and gitignored. All six
  endpoints return HTTP 200 via `scripts/check_razorpay.py` — but **0 records**,
  because the account has never processed a payment. `/settlements` and the recon
  endpoint are authorized and reachable but empty. So **settlement verification
  leg 3 is fixture-backed**, behind a `SettlementSource` protocol, and the README
  must say so plainly rather than implying a live reconciliation that never ran.
- **No Docker on this machine, and none needed.** `npx`/`mcp-remote` are
  available (Node 24), so Phase 6 uses Razorpay's **remote** MCP server at
  `https://mcp.razorpay.com/mcp`, which supports `create_payment_link`.
- **Anthropic is the on-brand model choice** — Razorpay Agent Studio is built on
  Anthropic's Claude Agent SDK and Razorpay is an official Claude connector. No
  model restrictions stated. The plan is to put the provider behind an interface
  anyway, both so Gemini/Groq free tiers work and because "the model sits behind
  an interface because nothing on the authorization path may depend on it" is a
  strong architecture-round line.
- `ANTHROPIC_API_KEY` — user was going to add it. Anthropic has a $5 minimum
  credit purchase; actual usage is cents. CI must never call it.

## Demo scenarios: 4 of 6 already work

Runnable now: mandate refuses a refund the AI proposed · duplicate execution
collapses to one link · default-deny on an undeclared tool · **cumulative
fan-out** (25 links at exactly INR 50,000, 26th refused, INR 30,000 prevented,
verified against sandbox state). Remaining: approval reuse (Phase 10),
settlement mismatch (Phase 8).

## Remaining plan

- **Phase 6 — live Razorpay test mode** via `npx mcp-remote` (already wired as
  `belay recover --live`; needs one real run to confirm, plus a signed evidence
  bundle from it).
- **Phase 7 — webhooks.** HMAC verify (official `razorpay` SDK), dedupe by event
  id, map to ledger events. `belay webhooks replay <file>` — no public endpoint,
  no tunnel. **This is what produces "money recovered", so it is required for the
  track bar.**
- **Phase 8 — settlement verification.** Three-way fold: AUTHORIZED (ledger) vs
  REPORTED (webhooks) vs SETTLED (recon). Verdicts `matched` / `mismatched` /
  `pending` / `unverifiable`, mirroring `RewindReport.verified_result`'s honest
  taxonomy. `mismatched` sub-reasons include `unauthorized_payment` — a settled
  payment with no authorizing ledger entry, the case that proves the loop closes.
  `fee_variance_only` must NOT count as a mismatch.
- **Phase 10 — batch metrics + adversarial suite** (`bench/`). Must produce
  **measured money recovered across the 200-payment batch** with a recovery rate,
  plus a benign control cohort (without one, a block rate is uninterpretable).
  Every attack asserts *which layer* caught it.
- **Phase 11 — the 5-minute demo** + minimal static dashboard.
- **Phase 12 — README, spec section, ADRs, traceability, limitations section.**

## Working agreement

One phase per session. After each: run the full gate (fast tests, slow tests,
demo, conformance, traceability, ruff, mypy), report real numbers, commit
locally with a detailed message. Never start a phase on a red build. Do not
push. Do not rename the `belay` package. Do not attempt the `mcp<2.0` -> 2.0
migration.

---

## RESUME_PROMPT

Copy everything below into a fresh session.

```text
I'm continuing work on a Razorpay AI Buildathon submission. The repo is at
C:\Users\Pc\Desktop\Razorpay_Hackathon (local only, never pushed).

FIRST: read docs/HANDOFF.md and AGENTS.md in that repo. They contain the full
current state, the architecture, the official track bar, four real bugs already
found and fixed, and the remaining plan. Then run the gate to confirm the state
matches what HANDOFF.md claims:

  cd C:\Users\Pc\Desktop\Razorpay_Hackathon
  .\.venv\Scripts\python.exe -m pytest -q --no-cov
  .\.venv\Scripts\python.exe examples\demo_fanout.py
  .\.venv\Scripts\python.exe -m belay.cli.main recover

Expect 576 passed / 0 failed; the fan-out demo blocking 15 of 40 links with INR
30,000 prevented; and `recover` diagnosing 200 payments in 8 model calls, executing
2 and parking 12 for approval. Use .\.venv\Scripts\python.exe for everything — the
project needs Python 3.12 and the system Python is 3.11.

Context in one paragraph: this repo was my own general AI-agent tool-safety MCP
proxy (belay-mcp, 31,896 LOC, L3 conformant, 28 ADRs). We retargeted it into an
AI revenue recovery agent for Razorpay with a deterministic financial control
plane. Phases 1-5 are done and committed: strip to the control plane; Money as
integer paise + MerchantMandate; a Razorpay sandbox MCP server plus a 13-contract
pack; cumulative/velocity limits; and the AI recovery layer. The central invariant
is that the AI proposes and the control plane authorizes — recovery/ is the only
package allowed to call an LLM and is forbidden by an AST test from importing
anything that could authorize, execute, or record. No paid API key is needed:
providers are pluggable (Gemini/Groq free tiers) and fall back to recorded
fixtures.

Deadline is 2026-09-05: a public repo, a 5-minute pitch VIDEO, and an
architecture walkthrough to a panel. The official Track 03 bar requires measured
money recovered across a batch, compliant escalation, stopping rules, and an
audit trail. We have three of those four; "measured money recovered across a
batch" needs Phase 7 (webhooks) and is a hard requirement.

Next up is Phase 6 (one real Razorpay test-mode run via `belay recover --live`,
which uses npx mcp-remote and needs no Docker) then Phase 7 (webhook ingestion,
which is what turns a created payment link into *recovered money*).

Work one phase at a time. After the phase, run the full gate (fast tests, slow
tests, both demos, conformance, traceability, ruff, mypy), report the real
numbers against the baselines in HANDOFF.md, and commit locally with a detailed
message. Don't push. Don't rename the belay package. Don't attempt the mcp 2.0
migration. Update docs/HANDOFF.md at the end of the phase.

Ask me before starting if anything in HANDOFF.md doesn't match what you find.
```
