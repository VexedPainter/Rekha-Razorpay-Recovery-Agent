# ADR 0028: retargeting Rekha from tool safety to financial control

- **Status:** accepted
- **Date:** 2026-08-25
- **Supersedes:** nothing. Retires the Native Agent Gate line of work (E18, E19, R1.x) and the adoption/DX surface (E17, E22, E23).

## Context

Rekha was built as a transactional safety layer for AI agent tool calls: an
MCP proxy plus a Native Agent Gate, governing filesystem and shell actions
with contracts, dry-run planning, policy, approvals, saga execution, rewind,
and a hash-chained ledger. At `ed7fdf1` it was 31,896 lines of Python, L3
conformant, with 28 ADRs and a CI-enforced spec traceability matrix.

The project is being retargeted at the Razorpay AI Buildathon's **AI Revenue
Recovery** track: an AI agent that inspects failed payments, diagnoses why
they failed, and proposes recovery actions, where a deterministic control
plane holds the merchant's mandate and, afterwards, proves from settlement
data that the money which moved is the money that was authorized.

An audit of the existing codebase against that target found the retarget to
be substantially a matter of pointing existing machinery at a new domain
rather than building new machinery:

- `Effect` already admits `spend` and `amount` in its effect vocabulary.
- `PolicyEngine` already enforces amount, count, and recipient caps and
  returns the most restrictive verdict across dimensions.
- `ApprovalQueue.consume()` is already a race-tested, single-use capability
  lease -- exactly what prevents an approval being replayed against a second
  financial action.
- `_plan_id = hash(session, tool, args)` already makes post-approval
  argument tampering structurally impossible rather than merely detectable.
- `IdempotencyStore` already guarantees a retried action calls the upstream
  once.
- `LedgerStore` plus `SignedEvidence` already produce offline-verifiable,
  tamper-evident provenance.
- `rekha/proxy/upstream.py` already launches an arbitrary stdio MCP
  subprocess via the official SDK, and the official
  `razorpay/razorpay-mcp-server` is an MIT-licensed stdio server -- so
  Razorpay can be wrapped with no new transport code at all.

Three gaps were found that are genuine engineering rather than plumbing, and
they define the work ahead:

1. `Cap.per: session` is declared in `rekha/policy/model.py` and **never read
   by `PolicyEngine`**. Cumulative limits therefore do not exist. Per-call
   caps alone do not bound an agent: forty payment links of Rs 4,000 each
   pass a Rs 5,000 per-action cap while breaching a Rs 50,000 daily budget.
2. Monetary amounts are `float` (`MaxAmount.value`, `Effect.amount`).
   Razorpay denominates in minor units. Floats on an amount path are a
   correctness bug.
3. There is no webhook ingestion, no settlement data, and no verification of
   actual financial outcome. `grep` finds zero occurrences of `webhook`,
   `settlement`, or `razorpay` under `rekha/`.

Meanwhile roughly two thirds of the codebase served the old domain
exclusively: the Claude Code / Codex / OpenCode Native Agent Gate
(`rekha/hooks/`, 1,982 LOC), the authenticated IPC supervisor that existed
only to serve it (`rekha/supervisor/`, 1,339 LOC), the editor onboarding and
client-registration surface (`rekha/cli/connection*`, `client_*`,
`host_detection`, `agent_instructions`), coding-agent session tooling
(`export_pr`, `explore`, `causal`, `learn`), and the distribution machinery
(`npm/`, PyInstaller binaries, signed release bundles, install scripts).

## Decision

Delete the code that serves only the retired domain, and scaffold the five
packages the new one needs.

**Deleted** (source plus tests): `rekha/hooks/`, `rekha/supervisor/`,
`rekha/cli/{connection,connection_models,client_registration,client_configs,host_detection,agent_instructions,export_pr,explore,causal}.py`,
`rekha/action_envelope.py`, `rekha/bundled_packs.py`, `rekha/packs/`,
`packs/{filesystem,git,claude-code-native}/`, `npm/`,
`scripts/{build_binary,install.ps1,install.sh,release_preflight,smoke_connect}.py`,
the per-feature demo scripts, `.github/workflows/release.yaml`, and the
`hooks`/`supervisor`/`release`/`connect`/`init`/`uninstall`/`doctor`/`repair`/`bootstrap`/`learn`/`explore`/`causal`/`export-pr`
CLI command groups.

**Kept untouched:** `rekha/{contracts,planner,policy,approvals,executor,ledger,proxy,rewind,db}/`
and `conformance/`. These are the control plane, and the retarget depends on
them being preserved, not rewritten.

**Kept temporarily:** `rekha/intent/`. `MerchantMandate` will replace
`IntentContract`, reusing its hash-pinning mechanism, in the next phase.

**Created**, each declaring its determinism contract in its docstring:

| Package | Responsibility | Determinism |
| --- | --- | --- |
| `rekha/finance/` | `Money` (integer minor units), `MerchantMandate` | deterministic |
| `rekha/razorpay/` | webhook ingestion, preflight reads | deterministic |
| `rekha/settlement/` | three-way settlement verification | deterministic, pure |
| `recovery/` | AI diagnosis, strategy, prioritization | **non-deterministic** |
| `bench/` | adversarial scenarios and metrics | deterministic |

`recovery/` is a top-level package rather than a subpackage of `rekha/`,
mirroring the existing `rekha`/`conformance` split. That is deliberate: the
directory listing should make the architectural boundary legible without
reading any code.

## The load-bearing invariant

`recovery/` is the only package permitted to consult a language model. In
exchange it may not import `rekha.ledger`, `rekha.approvals`, `rekha.policy`,
`rekha.executor`, `rekha.settlement`, or `rekha.finance`. It holds no
Razorpay credentials. Its only route outward is an MCP client session against
the Rekha proxy -- the same governed surface any other agent faces. Its only
output is a proposal.

This is enforced by `tests/test_layer_boundaries.py`, which parses the AST of
every module under `recovery/` and fails the build on violation. The test
includes a negative case pinning the detector against known-bad input,
because a guard that cannot fail proves nothing.

The reason is the central claim of the project, and it is a claim about
architecture rather than about prompt quality: a compromised, jailbroken, or
simply mistaken agent changes *which* recoveries are attempted. It cannot
change the mandate, the limits, the approval requirement, or the evidence.
This is the existing `No LLM sits on the safety path` thesis preserved intact
and given something to sit above, not a new position adopted for a hackathon.

## Consequences

Measured immediately after the strip:

| | Before (`ed7fdf1`) | After |
| --- | --- | --- |
| Python LOC | 31,896 | 15,551 |
| `rekha/cli/main.py` | 3,394 lines | 1,020 lines |
| Tests passing | 985 | 408 |
| Tests failing | 4 | **0** |
| Fast suite runtime | 145 s | 19 s |
| Branch coverage | 82.15% | **83.95%** |
| Coverage floor | 81% | **83%** |
| Conformance | L3 | L3 (unchanged) |
| Spec MUSTs covered | all | all (31) |

Four observations worth recording:

1. **The four pre-existing test failures are gone**, because all four lived
   in the deleted modules. They were Windows-environment artifacts -- a
   `MAX_PATH` limit in `test_ascii_slug_collapses_and_truncates`, and
   named-pipe `BrokenPipeError` in three supervisor hard-kill recovery tests
   -- not logic defects. They were never fixed; they were made irrelevant.
2. **Coverage rose while code was deleted.** The 82.15% figure was being
   dragged down by the adoption/DX modules, exactly as the old README
   claimed; the spec-normative core was always high. The floor is raised to
   83% accordingly, upward-only per existing convention.
3. **L3 conformance and the traceability gate both still pass**, and
   `python examples/demo.py --oops` still ends in `chain: OK` /
   `coherence: OK` / "session fully compensated". That demo is the
   regression gate for this ADR: it exercises resolve, plan, policy,
   approvals, saga execution, compensation, and chain verification in one
   run, so its passing is the evidence that the control plane survived the
   strip intact.
4. `tests/tools/test_project_config.py` was strengthened rather than merely
   updated: it now derives the markers the default gate must exclude from the
   declared marker list, so a future `live_*` marker cannot be added without
   being gated out of CI. The immediate motivation was adding `live_llm` and
   `live_razorpay`; the durable benefit is that CI cannot start spending real
   API credits by omission.

## Alternatives considered

**Keep the Native Agent Gate and add payments alongside it.** Rejected. It is
a second, partially-parallel decision engine whose surfaces mostly
self-report `trust_tier: UNKNOWN`, and `rekha/action_envelope.py` existed
solely to observe that the two engines' input shapes agree -- its own
docstring notes neither of its conversion functions is called from any
production path. Carrying it would double the surface a reviewer must read to
find the payments control plane, for no benefit to the target product.

**Rename the `rekha` package to something payments-flavoured.** Rejected. A
mass import rewrite produces thousands of lines of diff with no behavioural
change and no signal, and it would obscure the git history that is one of
this repository's genuine assets.

**Migrate `mcp` from `<2.0` to 2.0.** Rejected for now. It is a real breaking
change (`inputSchema` -> `input_schema`, `readOnlyHint` -> `read_only_hint`,
a removed test helper) that the code is not updated for. It is invisible to
the product and a plausible way to lose two days. The pin stays, documented.

**Delete the work without recording why.** Rejected. The deleted code was
real, shipped, and tested, and is being retired because the product changed
-- not because it failed. It is preserved at the immutable tag
`v0.2.0a1-agent-gate` and this ADR is the record. Deleting one's own working
code for scope reasons is a legitimate engineering decision; doing it
silently is not.
