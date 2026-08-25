# Handoff

**Rekha** — AI revenue recovery for Razorpay on a deterministic financial control
plane. Submission for the Razorpay AI Buildathon 2026, Track 03.

Author: Rahul J <rahuljaiprakashden@gmail.com>
Branch: `razorpay-recovery` · 122 commits · tags `v0.1.0`, `v0.2.0a1`,
`v0.2.0a1-agent-gate`

---

## Verified state

Every number below was produced by running the command, not by recollection.

| Gate | Result |
|---|---|
| Fast tests | **717 pass / 0 fail** |
| Slow tests | 26 pass |
| Branch coverage | **84.02%** (floor 83%, measures `rekha/` and `recovery/`) |
| L3 conformance | PASSED (`--target rekha`) |
| Spec MUSTs | 31, all covered by at least one test |
| ruff | clean |
| mypy | clean, strict, 81 source files |
| Adversarial | 7/7 blocked, 0/10 false positives |
| Held-out diagnosis | 100.0% macro F1 (keyword baseline 93.0%, majority 6.9%) |
| Backtest | +79.3% recovery from sequencing, 196 observed outcomes |
| Demo | ₹6,55,134 at risk → ₹7,452 recovered, chain OK |

## Environment

- Python **3.12** via `uv`, venv at `.\.venv\`. System Python 3.11 will not work.
- `uv pip install -e . --python .\.venv\Scripts\python.exe`
- Entry points: `rekha`, `rekha-conformance`
- Repo-local git identity is set; global config untouched.
- **`rekha recover` defaults to a live provider.** Pass `--provider replay` to use
  the recorded fixtures offline. `examples/demo_recovery.py` already does.

## Reproducing every claim

```powershell
$env:PYTHONIOENCODING="utf-8"
$env:PATH="C:\Users\Pc\Desktop\Razorpay_Hackathon\.venv\Scripts;$env:PATH"

python -m pytest -q                                   # 717 pass, coverage gate
python -m pytest -m "slow and not live_conformance" -q # 26 pass
ruff check . ; mypy rekha recovery bench
rekha-conformance run --target rekha --level 3
python scripts\traceability.py --check

python examples\demo_recovery.py
rekha bench run
rekha bench evaluate
rekha bench backtest --sweep
rekha bench calibration --db demo-recovery.db

rekha recover --provider replay
python scripts\simulate_payments.py --inject unauthorized
rekha settle-verify --db recovery.db --recon recon.json   # exits 1, by design
```

---

## What the AI contributes, honestly

Stated this way because the measurements are the strongest part of the submission,
and the unflattering ones are what make the flattering ones believable.

**Diagnosis is genuinely good.** 100% macro F1 on a held-out set against ground truth
the model never sees, versus 93% for a twelve-line keyword table that was included
expecting it might win. Read the margin rather than the absolute number: the cohort is
synthetic and its error text is cleaner than reality, and both classifiers read the
same text.

**Sequencing works, but it is persistence rather than intelligence.** +79.3% money
recovered, achieved by making 81% more contacts at slightly *worse* efficiency per
contact (₹832 → ₹823). The engineering value is that repeated contact is made safe,
not that the schedule is clever.

**The uplift rests on an unmeasured assumption.** Contact fatigue drives it more than
anything else: +38.4% at 0.30, +114.9% at 1.00. Always quote the range. Printed in
every report, swept with `--sweep`.

**The forecasts rank well and are calibrated badly.** ~18 points too pessimistic, so
Brier skill is −6.7% — they lose to quoting the base rate. But separation is +0.075:
payments rated higher do recover more often. The number is used *only* to rank against
a budget, where a constant offset cancels out, so it is fit for its purpose and unfit
for one nothing uses it for.

**Sample size reversed a conclusion.** At n=5 that same measurement showed skill
+9.6%. At n=196 it shows −6.7%. The small sample did not merely lack precision, it
pointed the wrong way. This is why `coverage` is surfaced.

**Timing judgement is narrow.** Only two distinct first-step delays (24h, 48h),
correctly assigning the longer to `insufficient_funds`, and `wait` used as an opening
action for 13 payments — all insufficient-funds, never elsewhere.
`corr(delay, true recoverability) = −0.525`, correctly signed but largely inherited
from correct classification rather than independent scheduling.

---

## Constraints discovered by measurement

- **Gemini free tier: 20 requests/day, per project not per key.** A second key on the
  same account shares the quota. 200 payments at batch 10 is exactly 20 requests.
- **Gemini drops the connection at batch 25**, works at 15. `DEFAULT_BATCH_SIZE = 10`
  leaves margin.
- **Groq counts requested `max_tokens` against its 8000 TPM limit.** The longer v2
  prompt made even 3 payments fail with HTTP 413 at the default 8192 budget. Prompt
  length and batch size trade off against each other; `--max-tokens` exists for this.
- **Gemini 3.x is a thinking model** — `parts[0]` may be a thought. Parts marked
  `thought` must be skipped.
- **Groq's `llama-3.3-70b-versatile` is retired** (404). Default is
  `openai/gpt-oss-120b`.
- **Razorpay test keys work but return zero settlement records** on all six endpoints,
  so settlement leg 3 is fixture-backed through a `SettlementSource` protocol.
- **No Docker available.** `--live` uses `npx mcp-remote` (Node 24) against Razorpay's
  remote MCP server.

## Traps that cost time — do not reintroduce

1. `LedgerStore` ignored its injected clock. It now takes a `Clock`.
2. Read effects with no `count` produced `unknown[]`, and spec §6.3 worst-casing beat
   `fetch_*: allow` — which would pause 200 times.
3. `approval_threshold` was declared and never enforced.
4. A demo counted `pending_approval` returns as successes.
5. Wall-clock anchoring staled fixtures hourly.
6. Advancing past a `wait` step by incrementing the touch count corrupted the
   contact-limit check. Plan position and contacts used are different quantities.
7. `Event.at` is an ISO string, not an epoch number. Convert at the boundary with
   `event_epoch()`; do not store both.
8. Diagnosing a shuffled subset changes batch composition, misses the fixtures, and
   silently scores the fallback default at 3% — a plumbing bug that looks like model
   collapse.
9. A single-shot baseline that discards `wait` plans does nothing at all on those
   payments and inflates the uplift. A baseline chosen to lose is not a baseline.
10. `git reset --hard` does not remove ignored files and `git clean -fd` skips them,
    so a stale `rekha/**/__pycache__` kept the directory alive and `git mv belay
    rekha` nested the package inside it.
11. **PowerShell backtick escaping corrupts files.** Use the file-edit tool for
    multi-line changes, never PowerShell regex with backticks.
12. `ruff format` is not part of this project's workflow. Running it reflows 65 files
    of prose comments. Only `ruff check --fix` should run.

---

## Next steps

**Queued, needs Gemini quota to reset (rolling window; currently 429):**
- A `diagnose_v3` prompt with richer per-cause timing guidance, A/B'd against v2's
  measured 24/48h spread. Worth doing only *with* the comparison — otherwise the rule
  table is doing the work while looking like model judgement.
- Fixtures from a second provider for the same prompt, to measure provider agreement.

**No quota needed:**
- Cohort-level bank-outage detection: eleven failures sharing a bank is one incident,
  which is invisible when payments are judged alone. One prompt change plus a
  measurement.
- Knapsack budget optimisation measured in rupees against the current greedy baseline.

**Outstanding for the author:**
- Create an empty public repo, then `git remote add origin …` and
  `git push -u origin razorpay-recovery && git push --tags`.
- Add `rahuljaiprakashden@gmail.com` as a verified email on that account, or all 122
  commits show as unattributed.
- Update the two GitHub URLs in `pyproject.toml`.
- Record the 5-minute video. `START_HERE.txt` §3 is the script, §5 is what makes it
  credible.
- Apply at `forms.gle/d9r2gvxp8cmoZhon9` before **2026-09-05**.
- Rotate the Gemini key in `.env` — it was pasted into a chat transcript. `.env` is
  gitignored and has never been committed; verified.

## Working agreement

After each phase: run the full gate (fast tests, slow tests, both demos, conformance,
traceability, ruff, mypy), report real numbers against the previous baseline, and
commit locally with a detailed message. Never start on a red build. Do not push
without being asked. Do not attempt the `mcp` 2.0 migration.
