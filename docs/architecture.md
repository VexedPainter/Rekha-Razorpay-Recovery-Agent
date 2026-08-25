# Architecture

## The one decision everything follows from

An LLM good enough to judge which failed payments deserve chasing is also good
enough to be confidently wrong, and to be manipulated by text a customer typed into
an order note. So the system is two packages with an enforced boundary, not one
package with careful prompting.

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

`recovery/` may **not** import `rekha.ledger`, `rekha.approvals`, `rekha.policy`,
`rekha.executor`, `rekha.settlement`, or `rekha.finance.mandate`. Enforced by an AST
check in `tests/test_layer_boundaries.py`, which includes a negative case pinning the
detector — a boundary test that cannot fail is decoration.

`rekha.finance.money` **is** permitted. A mandate is authority; `Money` is
arithmetic. Forbidding a value type would push raw integers across the boundary and
force the control plane to infer units, which is worse.

## Packages

| Package | Responsibility | Determinism |
|---|---|---|
| `rekha/contracts/` | what each tool may do; default-deny resolution | deterministic |
| `rekha/planner/` | turn a tool call into an explicit `Plan` before acting | deterministic |
| `rekha/policy/` | irreversibility rules, spending caps, velocity limits | deterministic, pure folds |
| `rekha/approvals/` | human-in-the-loop queue. CLI-only, never agent-reachable | deterministic |
| `rekha/executor/` | idempotent execution, compensation on failure | deterministic |
| `rekha/ledger/` | hash-chained evidence, signing, verification | deterministic |
| `rekha/finance/` | `Money` (integer paise), `MerchantMandate` | deterministic |
| `rekha/razorpay/` | webhook ingest, forecast recording, sequence advancement | deterministic |
| `rekha/settlement/` | three-way reconciliation, four verdicts | deterministic |
| `recovery/` | AI diagnosis, strategy, prioritisation, providers | **non-deterministic** |
| `bench/` | attacks, held-out evaluation, calibration, backtest | deterministic |
| `conformance/` | target-agnostic L1/L2/L3 suite | deterministic |

## Order of checks

The sequence matters, and it is checked in order of authority — facts first, then the
merchant's limits, then the model's preference. A proposal can only ever *narrow*
what the guardrails already permit.

1. **Mandate** — is this merchant, this action, this amount permitted at all? Checked
   *before* contracts, planning or policy: a proposal outside the mandate should
   never reach the machinery that would decide how to do it well.
2. **Contract resolution** — is there a declared contract for this tool? No contract
   means refusal. The pinned `ContractSet` *is* the agent's action space, so adding a
   capability is a reviewable diff rather than a prompt edit.
3. **Plan** — the effects, reversibility and amounts are made explicit before
   anything runs.
4. **Policy** — irreversibility, cumulative spend, per-customer velocity. Pure folds
   over the ledger, so a decision is recomputable from evidence.
5. **Approval** — above the threshold, a human decides. The agent has no path to the
   queue.
6. **Execution** — idempotent by reference id, with compensation on partial failure.
7. **Evidence** — every decision, refusal and result appended to a hash chain.

## Data flow for one recovery

```
Razorpay failed payment
   │
   ├─ PaymentSnapshot        narrow view: error fields only. No keys, no tokens,
   │                         no card data. `notes` IS included, because that is
   │                         where a real injection arrives.
   ├─ diagnose_batch()       10 payments per LLM call. Batching is not only for
   │                         cost: seeing a batch lets the model notice that
   │                         eleven failures share a bank and are one outage.
   ├─ RecoveryProposal       cause, opening action, amount, expected recovery,
   │                         plus an ordered follow-up plan. Validated wholesale;
   │                         a malformed entry is dropped with a recorded reason,
   │                         never patched into something plausible.
   ├─ clamped()              expected recovery capped at the original amount. A
   │                         model claiming to recover more than was lost is not
   │                         optimistic, it is wrong.
   ├─ prioritize()           deterministic ranking under a budget
   ├─ record_proposal()      the forecast written to the ledger BY THE CONTROL
   │                         PLANE, so the AI cannot curate its own track record
   ├─ [ the seven checks above ]
   ├─ execution              a payment link created, or a reminder sent
   ├─ webhook                Razorpay says what the customer actually did
   └─ correlate_recoveries() requested ≠ recovered, joined as a pure fold
```

## Multi-step sequencing

`rekha/razorpay/sequence.py` advances a plan one step at a time and **never
executes**. It returns the next step it believes permitted; the caller puts that
through the same seven checks any first action takes.

There is deliberately no fast path for an "already approved" step. Approval was
granted against the situation as it stood — by day four the payment may be refunded,
the customer may have paid by other means, or the cap may be exhausted.

State is **derived** from the ledger, never stored. A stored cursor can disagree with
the evidence, and when it does the agent is acting on a version of reality nobody can
audit.

Two invariants live here rather than in the prompt:

- **At most one live payment link per payment.** Two live links means the customer can
  pay twice. A step that would create a second demand returns `requires_cancel_of`.
- **Contact limits are configuration.** Three touches, seven days, twelve-hour
  minimum gap. Asked "how many times should I contact this customer?", a model gives
  a plausible answer that varies between runs — and this is a decision with legal and
  brand consequences.

## Measurement

Ground truth exists because the cohort is synthetic: we chose why each payment failed
when we generated it. It lives under a `_truth` key that the sandbox strips at its
single read boundary, so it cannot reach the model however a payment is fetched.

Simulated outcomes are drawn from that true probability, **never** from the model's
own forecast — otherwise every calibration score would be perfect by construction.

| Harness | Question |
|---|---|
| `bench/attacks.py` | Are attacks blocked, *and* is legitimate traffic left alone? |
| `bench/evaluate.py` | Is the diagnosis right, versus a majority and a keyword baseline? |
| `bench/calibration.py` | Are the probabilities any good, and on how many outcomes? |
| `bench/backtest.py` | What does sequencing earn, and at what cost in contacts? |

## Invariants worth knowing before changing anything

- **Money is integer minor units.** No `__float__`. Currency mismatch raises. Excess
  precision refused, not rounded.
- **Default-deny.** A tool with no contract is refused, not allowed.
- **Requested ≠ recovered.** A created link is something we did; recovery is a webhook
  fact. Enforced by test.
- **Honest verdicts.** `pending` and `unverifiable` never read as `matched`.
- **Fee variance is not a mismatch.** Flagging it would false-positive on every
  correct payment.
- **Payment age is anchored to the newest payment in the batch**, not the wall clock.
  Otherwise recorded fixtures go stale hourly and two runs disagree.
- **Prompts are hashed into every proposal.** Editing a prompt changes the recorded
  version, so behaviour cannot drift invisibly — and invalidates the fixtures
  recorded against it.
