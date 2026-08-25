# Architecture decision records

Why the system is shaped the way it is. Each record states the decision, what was
rejected, and the reasoning — including the decisions that turned out to be wrong.

## Reading order for a reviewer

The two that matter most for this submission are the last two, because they explain
the pivot from a general agent-safety proxy to a financial control plane:

| ADR | Decision |
|---|---|
| [0028](0028-retargeting-to-financial-control.md) | Strip the general-purpose agent tooling and retarget at payments |
| [0029](0029-financial-control-plane.md) | `Money` as integer paise, merchant mandates, and where the AI boundary sits |

Then, for the foundations the recovery system is built on:

| ADR | Decision |
|---|---|
| [0001](0001-e1-contracts-expressions.md) | Contracts as data; default-deny resolution |
| [0002](0002-e2-ledger.md) | Hash-chained evidence rather than logs |
| [0004](0004-e4-planner-policy.md) | An explicit `Plan` before any action |
| [0005](0005-e5-approvals.md) | Human-in-the-loop queue the agent cannot reach |
| [0006](0006-e6-saga-executor.md) | Idempotent execution with compensation |
| [0008](0008-e8-conformance-suite.md) | A target-agnostic conformance suite |
| [0013](0013-e13-signed-evidence.md) | Signed, externally verifiable evidence export |
| [0018](0018-traceability-matrix.md) | Every spec MUST mapped to a test |

## A note on language

**Records 0002–0007, 0009, 0010, 0012–0016 and 0019 are written in Spanish.** They
date from the project's earlier phases, before it was retargeted at Razorpay. The
code, tests, specification, README and all recent records are in English.

They are kept rather than deleted or hastily translated. They document decisions that
are still load-bearing — the ledger design, the approval queue, the saga executor —
and a decision record rewritten after the fact is worth less than one written at the
time. Deleting them to make the directory look tidier would remove the evidence that
these choices were reasoned about rather than assumed.

Records describing features that were later removed — the native agent gate, the
editor hooks, the release governance tooling — have been deleted, because a decision
record for code that does not exist is actively misleading rather than merely untidy.
ADR 0028 explains what was stripped and why.
