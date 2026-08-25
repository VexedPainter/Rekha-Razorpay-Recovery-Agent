# HANDOFF — resume state

Paste `RESUME_PROMPT` (bottom of this file) into a fresh session to continue.
`AGENTS.md` carries the standing engineering rules and is auto-loaded by agent
sessions; this file carries the *current position*.

Last updated: 2026-08-26, after Phase 8 + the adversarial suite, metrics, README
and ADR 0029.

---

## The project is functionally complete

An **AI revenue recovery agent for Razorpay with a deterministic financial control
plane**, for the Razorpay AI Buildathon, **Track 03 — AI Revenue Recovery**.

All four requirements of the official track bar are met and demonstrable:

| Requirement (verbatim from razorpay.com/buildathon) | Status |
| --- | --- |
| measured money recovered across a batch | **DONE** — INR 6,560 recovered of INR 10,791 requested, from webhooks |
| compliant escalation | **DONE** — 10 of 15 recoveries paused for a human |
| stopping rules | **DONE** — cumulative + velocity ceilings |
| audit trail | **DONE** — 78 hash-chained events, offline-verifiable signed evidence |

## What is left

**Nothing blocking.** Remaining work is yours, not code:

1. **Record the 5-minute pitch video.** Every beat is a runnable command (below).
2. **Push to a public GitHub repo** on the new account. 111+ commits and 3 tags
   travel with the push; nothing is lost. See "Pushing" below.
3. **Apply** at `forms.gle/d9r2gvxp8cmoZhon9` before **2026-09-05**.

Optional polish, in value order, none required:
- one live Razorpay run (`belay recover --live`, already wired, needs no Docker)
- a static HTML dashboard (`belay dashboard` exists but is not retargeted)
- a spec section for the financial control plane in `docs/spec.md`

## Where it lives / how to run

```
C:\Users\Pc\Desktop\Razorpay_Hackathon
```

Local only, never pushed. `.git` intact (history is a pitch asset). Repo-local git
identity `Jairo Gelpi <gelpierreape@gmail.com>`; global config untouched.

Always use the venv Python (project needs 3.12; system Python is 3.11):

```powershell
cd C:\Users\Pc\Desktop\Razorpay_Hackathon
.\.venv\Scripts\python.exe -m pytest -q --no-cov              # 656 tests, ~50s
.\.venv\Scripts\python.exe -m pytest -m slow --no-cov         # 26 subprocess tests
```

Put `.\.venv\Scripts` on PATH before the slow suite — some tests shell out to
`belay-conformance`.

## The demo, as commands

Each of these is one beat of the pitch video. All offline, no credentials.

```powershell
# 1. The whole closed loop, ~1 minute
.\.venv\Scripts\python.exe examples\demo_recovery.py

# 2. Seven adversarial scenarios, each naming the layer that refused it
.\.venv\Scripts\belay.exe bench run

# 3. The fan-out attack in isolation
.\.venv\Scripts\python.exe examples\demo_fanout.py

# 4. Settlement mismatch detection (three variants)
.\.venv\Scripts\python.exe -m belay.cli.main recover --provider replay
.\.venv\Scripts\python.exe scripts\simulate_payments.py --rate 0.6 --inject unauthorized
.\.venv\Scripts\python.exe -m belay.cli.main webhooks replay webhooks.json
.\.venv\Scripts\belay.exe settle-verify --recon recon.json      # exits 1

# 5. Measured metrics, folded from the ledger
.\.venv\Scripts\belay.exe bench metrics --db recovery.db

# 6. Offline-verifiable signed evidence
.\.venv\Scripts\belay.exe keygen demo.key
.\.venv\Scripts\belay.exe verify-export <session-id> --db recovery.db --key demo.key -o evidence.json
.\.venv\Scripts\belay.exe verify-evidence evidence.json

# 7. The inherited control plane still governs and rewinds
.\.venv\Scripts\python.exe examples\demo.py --oops
```

## Verified state at last commit (8cc3108)

| | |
| --- | --- |
| Tests | **656 pass / 0 fail**, + 26 slow |
| Branch coverage | 84.18% (CI floor 83%, upward-only) — measures `belay/` AND `recovery/` |
| L3 conformance | PASSED |
| Spec MUSTs | 31, all covered, CI-enforced |
| ruff / mypy | clean |
| Adversarial suite | 6/6 blocked, 0/10 false positives |

One offline run: 200 failed payments, INR 6,55,134 at risk, 15 selected, 5 executed,
10 escalated, **INR 6,560 recovered**, 78 events, chain + coherence OK.

## Architecture

| Package | Responsibility | Determinism |
| --- | --- | --- |
| `belay/` | contracts, planning, policy, approvals, idempotent execution, ledger, compensation | deterministic |
| `belay/finance/` | `Money` (integer paise), `MerchantMandate` | deterministic |
| `belay/policy/cumulative.py` | cumulative spend + velocity, folded from the ledger | deterministic, pure |
| `belay/razorpay/webhooks.py` | HMAC verify, dedupe, ingest, correlate | deterministic |
| `belay/settlement/` | three-way verification (+ `live.py` read-only source) | deterministic, pure |
| `recovery/` | AI diagnosis, strategy, prioritisation, providers | **non-deterministic** |
| `bench/` | adversarial scenarios + metrics | deterministic |
| `conformance/` | target-agnostic L1/L2/L3 suite | deterministic |

**The load-bearing invariant.** `recovery/` is the only package allowed to call an
LLM, and may not import `belay.ledger`, `belay.approvals`, `belay.policy`,
`belay.executor`, `belay.settlement`, or `belay.finance.mandate`. Enforced by AST in
`tests/test_layer_boundaries.py`, which includes a negative case pinning the
detector. `belay.finance.money` IS allowed: a mandate is authority, `Money` is
arithmetic, and forbidding a value type would push raw ints across the boundary.

Full reasoning for every decision: `docs/adr/0028-*.md` and `docs/adr/0029-*.md`.

## Phases done

1. **Strip** (`6b1808d`) — deleted the Native Agent Gate and adoption/DX surface.
   31,896 → 15,551 LOC. The 4 pre-existing Windows failures vanished with their
   modules. Coverage *rose*.
2. **Financial domain** (`223231a`) — `Money` (integer paise, no `__float__`),
   `MerchantMandate` replacing `IntentContract`, checked before everything else.
3. **Sandbox + contract pack** (`98f1f0c`) — real FastMCP Razorpay stand-in, 200
   seeded failed payments, 13 contracts, `Effect.amount_from`.
4. **Cumulative + velocity limits** (`6241b45`) — closed `Cap.per: session`, which
   was declared and never read for the project's entire life.
5. **AI recovery layer** (`db5b58b`, `d3436ae`) — proposal model, batched diagnosis,
   deterministic prioritisation, pluggable free providers, real Gemini fixtures.
6. **Webhooks** (`2a0e347`) — HMAC verify, durable dedupe, correlation as a pure
   fold. This is what makes money recovered *measured*.
7. **Settlement verification** (`0dfc38d`) — three-way reconciliation, four honest
   verdicts, four mismatch classes.
8. **Adversarial suite + README + ADR** (`8cc3108`).

## Real bugs found and fixed — do not reintroduce

1. **`LedgerStore` ignored its injected clock** — stamped `datetime.now(UTC)` while
   window limits compared that against `Clock.now()`. Two time sources for one
   comparison. `test_quota.py` had a workaround that rewrote `at` and broke the hash
   chain.
2. **Reads would have paused 200 times** — read effects declared no `count`, so they
   landed in `unknown[]` and spec §6.3 worst-casing beat the `fetch_*: allow` rule.
3. **`approval_threshold` was dead config** — declared in Phase 2, never enforced.
4. **A demo reported paused actions as successes** — `govern_and_execute` returns a
   `pending_approval` dict rather than raising. Conflating executed / paused /
   refused is how a caller believes money moved when it did not.
5. **Wall-clock anchoring staled fixtures within the hour** — payment `age_hours`
   came from `time.time()`, so every request was unique. Now anchored to the newest
   payment in the batch.
6. **Gemini 3.x is a thinking model** — the response carries reasoning as its own
   part, so reading `parts[0]` picked up a thought and failed to parse.
7. **Groq's default model was retired** — `llama-3.3-70b-versatile` 404s. Query the
   live catalogue rather than trusting memory.
8. **Batch-25 dropped connections** — measured limit is ~15; default is 10.
9. **Two README commands were wrong as first written** — `verify-export` needs a
   session id and a signing key; `keygen` takes a positional path. Found by running
   them.
10. **PowerShell backtick escaping corrupted two files** — a lone `\r` into
    `pyproject.toml`, a literal `` `n `` into a test. Use the file-edit tool for
    multi-line changes; never PowerShell regex with backticks.

## Environment

- **Razorpay test keys work.** `.env` is populated and gitignored. All six endpoints
  return HTTP 200 via `scripts/check_razorpay.py`, but **0 records** — the account
  has never taken a payment. So settlement leg 3 is fixture-backed, behind
  `SettlementSource`, and the README says so plainly.
- **Gemini and Groq keys both work**, verified with real calls. Fixtures in
  `recovery/fixtures/` are real Gemini output (`gemini-3.6-flash` for 13 batches,
  `gemini-3.5-flash-lite` for 7 after the free quota ran out). Gemini's free tier is
  **20 requests/day** — spent for the day after recording.
- **No Docker needed.** `npx mcp-remote` is available (Node 24), so `--live` uses
  Razorpay's remote MCP server.
- **A security near-miss, no leak:** keys were first pasted into `.env.example`, the
  tracked template. Caught before any commit, git history verified clean, template
  restored. `tests/tools/test_project_config.py` now fails the build if the template
  gains a real-looking value or loses a provider slot.

## Pushing (when ready)

111+ commits and tags `v0.1.0`, `v0.2.0a1`, `v0.2.0a1-agent-gate` all travel with a
push. A new repo on a new account loses nothing.

```powershell
git remote rename origin old-origin
git remote add origin https://github.com/NEW_ACCOUNT/REPO.git
git push -u origin razorpay-recovery
git push origin --tags
```

Create the repo **empty** (no README/license) or the push will conflict. Add
`gelpierreape@gmail.com` as a verified email on the new account so the 111 commits
attribute to it — cosmetic, but "111 commits over 5 weeks" is a real signal. Do NOT
rewrite history to change author emails.

GitHub needs a personal access token (`repo` scope) or `gh auth login`.

## Working agreement

One phase per session. After each: run the full gate (fast tests, slow tests, both
demos, conformance, traceability, ruff, mypy), report real numbers, commit locally
with a detailed message. Never start on a red build. Do not push. Do not rename the
`belay` package. Do not attempt the `mcp<2.0` → 2.0 migration.

---

## RESUME_PROMPT

```text
I'm continuing work on a Razorpay AI Buildathon submission. The repo is at
C:\Users\Pc\Desktop\Razorpay_Hackathon (local only, never pushed).

FIRST: read docs/HANDOFF.md, README.md and AGENTS.md in that repo. They contain the
full state, the architecture, ten real bugs already found and fixed, and what
remains. Then confirm the state matches:

  cd C:\Users\Pc\Desktop\Razorpay_Hackathon
  .\.venv\Scripts\python.exe -m pytest -q --no-cov
  .\.venv\Scripts\belay.exe bench run

Expect 656 passed / 0 failed, and the adversarial suite reporting 6/6 blocked with
0/10 false positives. Use .\.venv\Scripts\python.exe for everything -- the project
needs Python 3.12 and the system Python is 3.11.

Context: this repo was my own general AI-agent tool-safety MCP proxy (belay-mcp,
31,896 LOC, L3 conformant, 28 ADRs). We retargeted it into an AI revenue recovery
agent for Razorpay with a deterministic financial control plane. It is FUNCTIONALLY
COMPLETE -- all four requirements of the official Track 03 bar are met: measured
money recovered across a batch (INR 6,560 from webhooks), compliant escalation,
stopping rules, and an audit trail. The central invariant is that the AI proposes and
the control plane authorizes: recovery/ is the only package allowed to call an LLM
and is forbidden by an AST test from importing anything that could authorize,
execute, or record.

Nothing is blocking. What remains is mine to do: record a 5-minute pitch video, push
to a public repo, and apply before 2026-09-05. Optional polish is listed in
HANDOFF.md.

If I ask for changes, work one focused piece at a time. After each, run the full gate
(fast tests, slow tests, both demos, conformance, traceability, ruff, mypy), report
the real numbers against the baselines in HANDOFF.md, and commit locally with a
detailed message. Don't push. Don't rename the belay package. Don't attempt the mcp
2.0 migration. Update docs/HANDOFF.md at the end.

Ask me before starting if anything in HANDOFF.md doesn't match what you find.
```
