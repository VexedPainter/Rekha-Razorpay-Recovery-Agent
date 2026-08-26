# Rekha

**AI revenue recovery for Razorpay, on a deterministic financial control plane.**

रेखा — *the line.* From *Lakshman Rekha*: a boundary drawn with the understanding
that crossing it has consequences. An AI agent decides which failed payments are
worth chasing and how; it never decides what it is allowed to do. That line is the
project.

Submission for the **Razorpay AI Buildathon 2026, Track 03 — AI Revenue Recovery.**

```
217 failed payments diagnosed        INR 6,55,134 at risk
100% macro F1 on a held-out set      vs 93.0% for a keyword baseline
+79.3% recovery from sequencing      vs a fair single-attempt baseline
100% of attacks blocked              0 false positives on legitimate traffic
717 tests, 84% branch coverage       L3 conformance, 31 spec MUSTs covered
```

Every number above is reproduced by a command in this README. Several of them are
unflattering, and those are the ones worth reading.

---

## The problem

Indian e-commerce loses a large fraction of checkout revenue to failed payments —
bank timeouts, expired cards, abandoned UPI collect requests, customers who had no
balance until payday. Each failure has a cause, and the cause implies a different
remedy at a different time. Doing this well is judgement work at a volume no human
performs.

So it is a natural fit for an LLM. Which creates the actual problem: **an agent that
can create payment links can also create the wrong payment link, twice, for the
wrong amount, to a customer who already paid.** Enthusiasm is not a control.

Rekha splits the system in two and puts the line between them.

## Architecture

```
                    ┌──────────────────────────────────────┐
   failed payments  │  recovery/          NON-DETERMINISTIC │
   ───────────────► │  the only package that calls an LLM   │
                    │  diagnoses cause, proposes a sequence │
                    └──────────────────┬───────────────────┘
                                       │  proposals: untrusted requests
                    ═══════════════════▼═══════════════════  ← the line
                    ┌──────────────────────────────────────┐
                    │  rekha/             DETERMINISTIC     │
                    │  mandate → contracts → policy →      │
                    │  approval → idempotent execution →   │
                    │  hash-chained evidence               │
                    └──────────────────┬───────────────────┘
                                       │
                    ┌──────────────────▼───────────────────┐
                    │  Razorpay MCP server (or sandbox)    │
                    └──────────────────────────────────────┘
```

| Package | Responsibility | Determinism |
|---|---|---|
| `rekha/` | contracts, planning, policy, approvals, idempotent execution, ledger, compensation | deterministic |
| `rekha/finance/` | `Money` (integer paise), `MerchantMandate` | deterministic |
| `rekha/razorpay/` | webhook ingest, forecast recording, sequence advancement | deterministic |
| `rekha/settlement/` | three-way reconciliation | deterministic |
| `recovery/` | AI diagnosis, strategy, prioritisation, providers | **non-deterministic** |
| `bench/` | adversarial suite, held-out evaluation, calibration, backtest | deterministic |
| `conformance/` | target-agnostic L1/L2/L3 suite | deterministic |

### The load-bearing invariant

`recovery/` is the only package permitted to call an LLM, and it may **not** import
`rekha.ledger`, `rekha.approvals`, `rekha.policy`, `rekha.executor`,
`rekha.settlement`, or `rekha.finance.mandate`.

An agent that can write its own evidence can rewrite its own track record. An agent
that can read the approval queue can learn to route around it. So the boundary is
enforced by an AST check in `tests/test_layer_boundaries.py` — including a negative
case that pins the detector, because a boundary test that cannot fail is decoration.

`rekha.finance.money` **is** allowed: a mandate is authority, `Money` is arithmetic.
Forbidding a value type would push raw integers across the boundary and make the
control plane guess at units.

---

## What the AI actually does, measured

### Diagnosis: 100% macro F1 on a held-out set

```bash
rekha bench evaluate
```

```
classifier                     accuracy   macro P   macro R  macro F1
majority class (bank_transient)   26.0%      4.3%     16.7%      6.9%
keyword rule table                96.0%     91.7%     97.3%     93.0%
gemini-3.6-flash                 100.0%    100.0%    100.0%    100.0%
```

The cohort is synthetic, so ground truth exists — we chose why each payment failed
when we generated it. That label lives under a `_truth` key which the sandbox strips
at its read boundary, so it cannot reach the model however a payment is fetched. Two
tests enforce it, and one guards against itself.

**The keyword baseline was included expecting it might win.** A twelve-line rule
table scores 93%. If it had matched the model, the report says so outright. Read the
*margin*, not the absolute figure: 100% is high partly because synthetic failure
modes have clean, separable error text, and both classifiers read the same text.

Macro-averaged deliberately — the 3 `permanently_dead` payments count as much as the
26 `bank_transient` ones. Micro averaging would let strong performance on common
classes bury total failure on the rare ones, and `permanently_dead` is where being
wrong costs the most.

### Sequencing: +79.3% recovery, and it is not clever

```bash
rekha bench backtest --sweep
```

```
arm            recovered      payments   contacts   INR/contact
single-shot    INR 163,051    54/196     196        832
sequenced      INR 292,290    82/196     355        823
uplift         INR 129,239 (+79.3%)      +159 contacts
```

Read the last column. Sequencing recovers 79% more by making 81% more contacts, at
slightly *worse* efficiency per contact. **The gain is persistence, not
intelligence.**

Which relocates the engineering value honestly: not that the AI schedules
brilliantly, but that repeated contact is made *safe* — one live payment link at a
time, a hard contact ceiling, and every step re-authorized against current evidence
instead of riding the first approval. Persistence is easy. Persistence that cannot
double-charge a customer is the work.

The uplift depends on an assumption this repo cannot measure — how much customers
resent being chased:

```
fatigue 0.30 → +38.4%     0.65 → +79.3%     1.00 → +114.9%
```

So it is a parameter, printed in every report and swept by `--sweep`. **Quote the
range, not the point.**

### Forecast quality: right ranking, wrong levels

```bash
rekha bench calibration
```

```
mean forecast 0.240   actual rate 0.418   Brier 0.2596   skill vs base rate  -6.7%
forecast when recovered 0.283   when missed 0.208        separation         +0.075
```

The model is uniformly ~18 points too pessimistic. Brier punishes level error, so
**skill is negative** — the forecasts lose to a forecaster that simply quotes the
base rate to everyone.

But the *ranking* is right: payments it rates higher do recover more often. Those are
different questions. The forecast is used **only to rank payments against a budget**,
where a constant offset cancels out exactly — so it is fit for its actual purpose
and unfit for one nothing puts it to. Correcting the level would improve the Brier
score and change no decision the system makes.

Worth noting how this was found: at n=5 an earlier report showed skill **+9.6%**. At
n=196 it is **−6.7%**. The small sample did not merely lack precision, it pointed the
wrong way. That is why `coverage` is reported rather than buried.

### Timing: real, but narrower than hoped

The model proposes only **two** distinct first-step delays (24h and 48h), correctly
giving the longer one to `insufficient_funds`, and uses `wait` as an opening action
for 13 payments — all of them insufficient-funds, never anywhere else.
`corr(delay, true recoverability) = −0.525` across 196 payments, correctly signed.

But since the delay tracks cause class almost exactly, that signal is largely
**inherited from correct classification** rather than independent per-payment
scheduling. The defensible claim is narrow: the model works out that
insufficient-funds failures need days rather than hours. It does not do fine-grained
scheduling. Tightening the prompt with explicit per-cause hours would widen the
spread, but then the rule table is doing the work while looking like model judgement.

### Adversarial: 100% blocked, 0 false positives

```bash
rekha bench run
```

Seven scenarios including prompt injection through a customer-supplied order note,
amount inflation, unauthorised refunds, and approval bypass. A block rate without a
false-positive rate is meaningless — a system that refuses everything scores 100% —
so both are reported, over ten legitimate recoveries that must succeed.

---

## Financial correctness

**Money is integer minor units.** `Money` has no `__float__`. Currency mismatch
raises. Excess precision is refused, never rounded. Floating-point rupees is how you
lose ₹0.01 per transaction and cannot reconcile at the end of the month.

**Default-deny.** `resolve()` refuses any tool with no contract. The pinned
`ContractSet` *is* the agent's action space, so adding a capability is a reviewable
diff rather than a prompt edit.

**Requested ≠ recovered.** A created payment link is something we did; recovery is a
webhook fact. Conflating them makes any recovery system look perfect. Enforced by
test.

**At most one live payment link per payment.** Two live links means the customer can
pay twice, and the merchant refunds one having paid fees on both. Escalating the
method requires cancelling the live link first, and `Strategy.REMIND` exists so the
common second touch creates no new obligation at all.

**Honest verdicts.** Absence of evidence never reads as agreement — `pending` and
`unverifiable` are distinct from `matched`. Fee variance is *not* flagged as a
mismatch, because it would false-positive on every correct payment.

**Hash-chained evidence.** Every decision, refusal and result is appended to a
tamper-evident ledger, exportable and signature-verifiable.

---

## Quickstart

**Requires Python 3.12+.** The code uses PEP 695 type-alias syntax, so 3.11 cannot
parse it. Every entry point checks this and prints the exact command to run if your
`python` is older — a common situation on Windows, where creating a virtualenv does
not change what bare `python` resolves to on `PATH`.

```powershell
uv venv
uv pip install -e .
.\.venv\Scripts\Activate.ps1     # do this, or prefix commands with .\.venv\Scripts\
```

The repo ships **real recorded Gemini output**, so the full pipeline runs offline and
deterministically. Pass `--provider replay` to use it — the CLI otherwise defaults to
a live provider and will rate-limit without a key.

```powershell
# the headline demo: 200 failed payments, diagnosed, prioritised, executed
python examples\demo_recovery.py
```

If you skipped activation, the equivalent is:

```powershell
.\.venv\Scripts\python.exe examples\demo_recovery.py
```

### Measured claims

```bash
rekha bench run              # adversarial: block rate AND false-positive rate
rekha bench evaluate         # held-out diagnosis accuracy vs two baselines
rekha bench backtest --sweep # what sequencing earns, across the assumption
```

Those three are self-contained. Calibration scores a real ledger, so it needs one —
the demo writes `demo-recovery.db`:

```bash
python examples/demo_recovery.py
rekha bench calibration --db demo-recovery.db
```

### Conformance and traceability

```bash
rekha-conformance run --target rekha --level 3
python scripts/traceability.py --check
```

### Settlement: did the money that moved match what we authorized?

Three steps. `recover` creates the payment links, `simulate_payments.py` injects a
specific settlement fault, and `settle-verify` must catch it and exit non-zero:

```bash
rekha recover --provider replay
python scripts/simulate_payments.py --inject unauthorized   # or amount, duplicate
rekha settle-verify --db recovery.db --recon recon.json
```

```
[MISMATCH]  recover-pay_NEVER_AUTHORIZED  unauthorized_payment
```

To use a real LLM instead of the recordings, add a free Gemini or Groq key to `.env`
(see `.env.example`) and pass `--provider gemini`. To run against Razorpay's own MCP
server instead of the bundled sandbox, use `--live`.

---

## Honest limitations

- **The cohort is synthetic.** Real Razorpay traffic has messier error text, so
  classification accuracy would fall. The *margin* over the keyword baseline is the
  number that transfers.
- **The contact-fatigue assumption is invented.** It drives the sequencing uplift
  more than anything else measured here. Nothing in this repo establishes it.
- **Calibration is measured on chased payments only.** The agent chose those because
  it was confident, so the sample is biased and `coverage` says by how much.
  Measuring the rest would mean chasing payments judged hopeless, at the merchant's
  expense.
- **Settlement leg 3 is fixture-backed.** Razorpay's sandbox returns HTTP 200 with
  zero settlement records, so the reconciliation runs against recorded fixtures via
  a `SettlementSource` protocol rather than live data.
- **The train/test split is partly ceremonial.** Nothing is trained — the prompt is
  hand-written — so the split prevents tuning on test data but does not carry the
  weight it would for a fitted model. Reported rather than implied.

---

## Documentation

| Document | Purpose |
|---|---|
| [`START_HERE.txt`](START_HERE.txt) | Plain-English tour, no jargon |
| [`docs/architecture.md`](docs/architecture.md) | Package structure and data flow |
| [`docs/spec.md`](docs/spec.md) | Normative specification, 31 MUSTs |
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | Current state, verified numbers, next steps |
| [`docs/adr/`](docs/adr/) | Architecture decision records |

---

## Licence

MIT. Author: Rahul J.
