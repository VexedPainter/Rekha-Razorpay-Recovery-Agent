# ADR 0029: the financial control plane

- **Status:** accepted
- **Date:** 2026-08-26
- **Builds on:** [ADR 0028](0028-retargeting-to-financial-control.md)

Covers phases 2 through 8 of the retarget as one record, because the decisions
interlock and reading them apart would lose the reasoning.

## 1. Money is an integer, and the type is awkward on purpose

`Cap.max_amount` was a `float` and `Effect.amount` was `dict[str, object]`.
Replaced with `Money{minor_units: int, currency: str}`.

Three reasons, in order of how much they matter:

1. **A limit is a comparison, and a cumulative limit sums before comparing.**
   `0.1 + 0.2 > 0.3` is `True` in binary floating point, and the accumulated error
   runs in the direction of authorizing *more* than the merchant permitted.
2. **Razorpay denominates in paise.** Storing rupees and converting at the boundary
   invites an off-by-100 in the one place it cannot be recovered from.
3. **Evidence must be reproducible.** Ledger events are hashed, and a float's
   shortest-repr can differ across platforms, so a signed bundle might not verify
   on a machine that is not ours.

`Money` has no `__float__`, no `__int__`, and no arithmetic with bare numbers.
Mixing currencies raises. Human-authored YAML (`{major: "5000.00"}`) parses through
`Decimal`, never `float`, and **excess precision is refused rather than rounded** —
`10.005` INR is ambiguous between 1000 and 1001 paise, and guessing on a monetary
limit is not acceptable.

**Rejected:** `Decimal` throughout. It permits fractional minor units, which is not
a thing that exists in a payment system, and it would let a rounding policy be
chosen implicitly at each call site.

## 2. The mandate is checked before everything else

`MerchantMandate` replaces `IntentContract`, reusing its hash-pinning, and is
evaluated *before* contract resolution, planning, and policy.

Those layers answer "is this action safe?". The mandate answers a prior question:
"did the merchant authorize this agent to do this kind of thing at all?" That answer
must not depend on the rest of the pipeline having run — and a forbidden action with
no contract must report `mandate_violation`, not `contract_missing`, or the agent is
told to go and write a contract for something the merchant never approved.

`purpose` is free text and deliberately **not** enforced. Pretending to
machine-check "only chase genuinely recoverable payments" would be worse than not
checking it.

Incoherent mandates are refused at load time rather than when money is about to
move: mismatched currencies, negative limits, an action both allowed and forbidden,
and `max_per_action` above `max_cumulative` (which would mean the aggregate ceiling
could never bind, so one of the two numbers is wrong).

### `action_describer` is injected and never defaulted

The mandate needs an amount to enforce a ceiling, and only the integration knows
Razorpay spells it `args["amount"]` in minor units. `belay/proxy/` must stay
domain-agnostic, so this is injected — and **not** defaulted to a guess at field
names, because reading the wrong key would silently enforce *no* ceiling, which is
the worst available failure mode. A describer that raises fails closed.

## 3. `Cap.per: session` was declared and never read

For the entire life of the project, `Cap.per` existed in the model and
`PolicyEngine` never consulted it, so `per: session` silently behaved as
`per: call`. A cap an operator believes is an aggregate budget, but which only ever
sees one action, is worse than no cap: it reads as protection in a policy document
while providing none. `Cap`'s own docstring diagnosed this and proposed the fix.

`belay/policy/cumulative.py` implements it. Two design points carried over from
`QuotaTracker`, which had solved the same shape of problem for *counting*:

- **The ledger is the only source of truth.** No second tally to drift, nothing a
  crash can lose; a restarted process recomputes the same number.
- **Keyed by `plan_id`, never `step_seq`.** A paused call re-plans under a new
  `step_seq` when retried after approval, so matching an approval to the step that
  executed only works through the `plan_id` both share. Keying on `step_seq`
  double-charges every human-approved recovery, and the error stays invisible until
  a budget runs out at half its stated value.

`QuotaTracker` was refactored onto the shared fold. Two copies of that reasoning is
one too many — if they diverged, one of two limits would be silently wrong.

`per: session` with `max_count` or `max_recipients` is now a **load-time error**.
Those have no unambiguous aggregate meaning (a count of what — effects or actions?),
and silently treating them as `per: call` is exactly the bug that existed before.

**Why the mandate's aggregate ceiling lives in the lifecycle, not the engine:**
`PolicyEngine.evaluate` is a pure function of `(plan, policy)`. Folding in the
mandate would mean it could no longer be reasoned about from its own two inputs. So
policy caps live in the engine and mandate ceilings in the lifecycle, combined
most-restrictive-wins.

## 4. The AI proposes; nothing else

`recovery/` is the only package permitted to call a model, and may not import
`belay.ledger`, `belay.approvals`, `belay.policy`, `belay.executor`,
`belay.settlement`, or `belay.finance.mandate`. Enforced by AST in
`tests/test_layer_boundaries.py`, which includes a negative case pinning the
detector — a guard that cannot fail proves nothing.

`belay.finance.money` **is** permitted. The line is capability versus value type: a
mandate is authority, and an AI layer that could construct one could widen its own
permissions; `Money` is an immutable integer whose possession grants nothing.
Forbidding it would force raw ints across the boundary and make the control plane
infer their units, which is the bug `Money` exists to prevent. Denying a value type
buys no safety and costs correctness.

### Output is trusted neither structurally nor numerically

Malformed entries are rejected wholesale, never patched. A non-positive amount is
rejected rather than clamped — clamping to zero yields a worthless payment link,
clamping up decides on the model's behalf. `expected_recovery` is capped at the
original amount, because a model claiming to recover more than was lost is wrong in
a way no prompt reliably prevents. An answer about a payment nobody asked about is
dropped: the model does not get to widen its own batch. A provider failure proposes
nothing, because an unreachable model is not permission to act unadvised.

**Every input payment gets exactly one proposal.** That invariant is what makes
"measured money recovered across a batch" meaningful — the denominator has to be
the whole batch.

### Batched diagnosis, at 10 payments per call

Measured against real free tiers: Gemini completes 15 per request in ~24s and
reliably drops the connection at 25; Groq's free tier caps at 8,000 tokens/minute,
which batch-10 already exceeds. Gemini's free tier allows **20 requests**, so a
200-payment cohort at one call per payment was never viable. 10 gives 20 calls with
margin.

### Providers behind an interface, over raw HTTP

Free options first (Gemini, Groq), Anthropic optional, and recorded fixtures as the
fallback so the system runs with no credentials at all. Raw `httpx` rather than
three vendor SDKs: `httpx` is already a transitive dependency, so this adds none,
and every byte sent to a model is visible in one small package — which matters for
a project whose argument is that the model cannot exceed its authority.

**Real API calls found three bugs no local test would have.** Gemini 3.x is a
thinking model whose response carries reasoning as its own part, so reading
`parts[0]` picked up a thought and failed to parse. Groq's obvious default model was
retired and 404'd. And batch-25 dropped connections. All three are recorded in the
commit history because "we tested it against the real thing" is the only reason they
were found.

## 5. Payment links are `conditional`, not `reversible`

Cancellation is a real undo that stops working the moment the customer pays — the
sandbox and the real API both refuse it. So the contract declares `conditional`, the
condition is evaluated at commit time, and an already-paid link honestly registers
as irreversible rather than claiming an undo that would fail.

`create_refund` is declared and **fully functional** on purpose. The mandate is what
refuses it. If the tool were simply absent, the prompt-injection scenario would
prove nothing about authorization — only about a missing integration.

## 6. Webhooks: verify, deduplicate, record, and nothing else

A webhook is the first fact in this system not derived from our own request, which
is what makes it usable as evidence.

- **Constant-time comparison.** `==` on an HMAC returns faster on an early mismatch,
  leaking how much of a forged signature was correct. `hmac.compare_digest`, with a
  test that reads the source to assert it.
- **Dedupe reads the ledger, not memory**, so a retry after a crash cannot
  double-count.
- **Correlation is a separate pure fold.** A webhook must be recordable even when it
  concerns something we never did — precisely the case settlement verification
  exists to catch — and "measured money recovered" is a claim about two independent
  records agreeing, which should be recomputable from evidence rather than asserted
  at write time.
- **A partial payment followed by a full payment is not summed.**
  `payment_link.paid` restates the cumulative total, so adding them would report
  more recovered than the link was worth.
- **No HTTP endpoint.** That means a tunnel, a hostname, and a demo that fails when
  the network does. Saved payloads replayed through the real verifier produce
  identical evidence.

**Rejected:** the official `razorpay` SDK for signature verification. It is five
lines of `hmac` plus a constant-time compare, and taking on its dependency tree for
that is a poor trade. The real risk in hand-rolling is `==`, addressed above.

## 7. Settlement verification, and what it refuses to conclude

Three legs, and the third is what makes it verification rather than bookkeeping:
comparing AUTHORIZED against REPORTED alone is self-referential, since a webhook
arrives *because* we created a link.

**The join needs all three legs.** Our ledger knows payment link ids; settlement is
itemised by payment id; the webhook is the only record carrying both. There is a
test proving an entry cannot be attributed without it.

Verdicts follow Verified Rewind's four-value taxonomy for the same reason it exists
there: **absence of evidence must never read as agreement.** No settlement data
reports `unverifiable`, never `matched`.

Two decisions that keep it credible:

- **Fee variance is not a mismatch.** Razorpay deducts a fee and GST from every
  settlement, so net is below gross on every *correct* transaction. Reporting that
  would be a false positive on literally every payment, and a verifier that cries
  wolf on ordinary traffic gets its real findings ignored.
- **Scope is bounded to entries claiming our own reference.** A merchant's
  settlement report contains every payment they took. Flagging "settled with no
  authorizing ledger entry" across the whole report would mark every organic sale as
  unauthorized — alarming, wrong, and fatal to the credibility of every other
  verdict. Out-of-scope entries are counted as `unrelated` and reported, because
  "we checked 5 of 500" is material context.

`belay settle-verify` **exits non-zero** on a mismatch. A verification tool that
reports a discrepancy on stdout and exits 0 cannot gate anything.

`LiveSettlementSource` bypasses the governed MCP proxy deliberately: the proxy gates
actions that *change* things, and routing a read-only audit query through it would
add a ledger entry and an approval surface to a step whose purpose is to check from
outside. Verification must not be able to affect what it verifies.

## 8. The benign cohort is not optional

`bench/` runs six adversarial scenarios plus **ten legitimate recoveries that must
not be blocked.** A system that refuses everything scores a perfect block rate, so
an unauthorized-action block rate is uninterpretable without a false-positive rate
measured over legitimate traffic.

Every scenario asserts *which layer* refused the action, not merely that something
did. A test that only checks "it was blocked" cannot distinguish a designed control
from a coincidence — and there is a test asserting each scenario is caught by the
*intended* control, so a scenario that starts passing for the wrong reason fails the
build.

## Consequences

| | Before retarget | Now |
| --- | --- | --- |
| Tests passing | 985 (4 failing) | 656 (0 failing), + 26 subprocess |
| Branch coverage | 82.15% | 84% (floor 83%, measures `recovery/` too) |
| L3 conformance | PASSED | PASSED |
| Spec MUSTs covered | all | all (31), CI-enforced |

Measured on one offline run: 200 failed payments, ₹6,55,134 at risk, 15 recoveries
selected, 5 executed, 10 escalated to a human, ₹6,560 recovered from webhooks, 78
ledger events, chain and coherence OK. Adversarial suite: 6 of 6 blocked, 0 of 10
false positives.

Six bugs were found by building this, all recorded in the commit history: the ledger
ignoring its injected clock; read effects with no declared count pausing 200 times;
`approval_threshold` declared and never enforced; a demo counting paused actions as
successes; wall-clock anchoring staling fixtures within the hour; and the three
provider bugs above. Each was found by running the thing rather than reasoning about
it, which is the argument for the demos being executable tests.
