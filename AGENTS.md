# AGENTS.md

Rules for any human or AI agent working on this repository. Read this before
changing anything.

## What this project is

An **AI revenue recovery agent for Razorpay, with a deterministic financial
control plane.**

An LLM inspects failed payments, diagnoses why they failed, estimates
recovery potential, and proposes a recovery action. A deterministic control
plane then decides whether that action is permitted, requires a human, and
executes at most once -- and afterwards proves from settlement data that the
money which moved is the money that was authorized.

The loop the project exists to close:

```
Intent -> Authorization -> Execution -> Actual Outcome -> Verification
```

This repository was previously a general tool-safety proxy for AI agents. It
was retargeted on 2026-08-25; see `docs/adr/0028-retargeting-to-financial-control.md`
for what was deleted and why. The pre-retarget state is preserved at the tag
`v0.2.0a1-agent-gate`.

## Architecture: five packages, one boundary

| Package | Responsibility | Determinism |
| --- | --- | --- |
| `belay/` | financial control plane: contracts, planning, policy, approvals, idempotent execution, hash-chained evidence, compensation | **deterministic** |
| `belay/finance/` | `Money` (integer minor units), `MerchantMandate` | deterministic |
| `belay/razorpay/` | webhook ingestion, preflight reads | deterministic |
| `belay/settlement/` | three-way settlement verification | deterministic, pure |
| `recovery/` | AI diagnosis, strategy, prioritization, estimation | **non-deterministic** |
| `bench/` | adversarial scenarios and metrics | deterministic |
| `conformance/` | target-agnostic L1/L2/L3 conformance suite | deterministic |

### The rule that matters more than any other

**The AI proposes. The control plane authorizes.**

`recovery/` is the only package allowed to call a language model. In exchange
it must never import `belay.ledger`, `belay.approvals`, `belay.policy`,
`belay.executor`, `belay.settlement`, or `belay.finance`. It holds no
Razorpay credentials and appends to no ledger. Its only route outward is an
MCP client session against the Belay proxy. Its only output is a
`RecoveryProposal`.

`tests/test_layer_boundaries.py` enforces this by parsing the AST of every
module under `recovery/`. Do not weaken it, do not add exceptions to it, and
do not "temporarily" import an enforcement module to make something work. If
the AI layer needs a value from the control plane, it must be **passed in**.

The point: a compromised, jailbroken, or simply wrong agent changes *which*
recoveries are attempted. It must not be able to change the mandate, the
limits, the approval requirement, or the evidence.

## Non-negotiables

- **No LLM on the authorization path.** Anything deciding whether money moves
  must be a pure function of `(proposal, mandate, policy, ledger)` so it is
  replayable and provable. If you find yourself asking a model whether an
  action is allowed, stop.
- **Money is an integer.** Minor units (paise for INR) plus a currency, via
  `belay/finance/money.py`. A `float` anywhere on an amount path is a
  correctness bug. Currency mismatches raise; they never coerce.
- **Default-deny.** `belay/proxy/lifecycle.py::resolve()` refuses any tool
  with no contract (`contract_missing`). The pinned `ContractSet` *is* the
  agent's action space. Never add an `unsafe_passthrough` to make a demo work.
- **The ledger is append-only.** `LedgerStore` exposes no update and no
  delete, deliberately. Do not add one.
- **Honest reporting.** Absence of evidence is never reported as agreement.
  `pending` and `unverifiable` are real verdicts and must not collapse into
  `matched`. `fully_rewound` is never true while an irreversible step is in
  scope. This taxonomy discipline (`belay/rewind/service.py`) is deliberate;
  mirror it in new verdicts rather than returning a boolean.
- **Contracts are data, never code.** `belay/contracts/expressions.py` is a
  closed grammar with no `eval`, no `ast`, and one blessed builtin. It is a
  security boundary. Do not widen it.
- **Never commit credentials.** `.env` is gitignored. Keys come from the
  environment only. Test mode only -- never production keys, never real card
  data.

## Conventions

- Python 3.12+. `ruff check .` and `mypy belay` must be clean.
- `pytest` must be green before any commit. The fast suite runs in ~20s;
  there is no excuse for skipping it.
- Branch coverage floor is enforced in CI and is **upward-only**. Raise it as
  more lands; never lower it silently.
- Tests that call a real external API are marked `live_llm`, `live_razorpay`,
  or `live_conformance` and are excluded from the default suite. The default
  suite must run offline, with no credentials, with no network.
  `tests/tools/test_project_config.py` enforces that a new `live_*` marker
  cannot be added without being gated out of CI.
- Every normative MUST in `docs/spec.md` needs a test tagged `@spec("X.Y")`.
  `scripts/traceability.py --check` runs in CI and fails on a gap.
- Significant decisions get an ADR in `docs/adr/`. Record the alternatives
  you rejected and why, not just what you chose.
- Adversarial tests must assert **which layer** refused an action, not merely
  that something did. A test that only checks "it was blocked" cannot
  distinguish a designed control from luck.

## Reuse before you build

Most of what a new feature needs already exists and is tested. Before writing
a new mechanism, check:

- Rolling windows over ledger history -> `belay/policy/quota.py::QuotaTracker`
  (note it keys on `plan_id`, not `step_seq`, because a paused call's retry
  re-plans under a new `step_seq` -- getting this wrong double-counts).
- Single-use authorization -> `belay/approvals/queue.py::consume()`. This is
  a compare-and-swap, because a read-then-write version was reproducibly
  proven wrong under a real race. Do not "simplify" it.
- Argument tampering after approval -> already prevented, because
  `plan_id = hash(session, tool, args)`. A changed amount produces a
  different `plan_id` and the old approval is simply never found.
- Exactly-once execution -> `belay/executor/idempotency.py`.
- Tamper-evident provenance -> `belay/ledger/`, `SignedEvidence`.
- Compensating a mistaken action -> `belay/rewind/`.
- Deriving state from history alone -> `belay/ledger/replay.py`'s pure fold.
- Keeping PII out of evidence -> `redact` in contracts.

## Razorpay integration

Write paths reach Razorpay **only** through the official
`razorpay/razorpay-mcp-server` (MIT), launched as a wrapped stdio MCP
subprocess by `belay wrap --command docker ...`. Do not add an HTTP client
for payments: a second, ungoverned path to Razorpay defeats the entire
control plane. Do not fork or vendor the Razorpay MCP server; pin it.

Webhooks are the one exception and are read-only.

`examples/razorpay-sandbox/` mimics the Razorpay tool surface so the whole
system runs offline, in CI, with no credentials. The sandbox is the default;
`--live` opts into the real test-mode server. **The demo must never depend on
the network.**

## What not to build

Deliberately out of scope. Do not add these without an explicit decision:

- A payment gateway, checkout, or anything that duplicates Razorpay.
- A general agent "firewall" -- Razorpay ships first-party guardrails.
- Auth, multi-tenancy, RBAC, OAuth, secret rotation, HSM/KMS key custody.
- A vector database or RAG pipeline. There is no corpus problem here.
- Real customer messaging infrastructure -- `send_payment_link` does it.
- General double-entry accounting.
- PCI scope, card storage, tokenization.
- Publishing to PyPI or npm.
- Renaming the `belay` package (mass diff, zero signal).
- Migrating `mcp` to 2.0 (real breaking change, invisible to the product,
  documented pin).
