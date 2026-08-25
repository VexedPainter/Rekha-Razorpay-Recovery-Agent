# Belay Recovery

**AI revenue recovery for Razorpay, with a deterministic financial control plane.**

> **Status: under active construction.** This README is a stub. The project
> is being retargeted from a general AI-agent tool-safety proxy into a
> revenue-recovery system for the Razorpay AI Buildathon (AI Revenue Recovery
> track). See [`docs/adr/0028-retargeting-to-financial-control.md`](docs/adr/0028-retargeting-to-financial-control.md)
> for what changed and why. The pre-retarget project is preserved at the tag
> `v0.2.0a1-agent-gate`.

## The problem

A merchant has a pile of failed payments. Some are recoverable: the customer's
OTP timed out, their bank had a transient error, they abandoned checkout. An
AI agent is genuinely good at working out which ones are worth chasing and
how.

So: would you give that agent your Razorpay API keys?

## The answer

No. You give it a **mandate**, and put a deterministic control plane between
it and your money.

```
   AI proposes                    Control plane authorizes           Razorpay executes
┌────────────────────┐        ┌───────────────────────────┐       ┌──────────────────┐
│  recovery/         │        │  belay/                   │       │  razorpay-mcp-   │
│                    │        │                           │       │  server (MIT)    │
│  diagnose failure  │──MCP──▶│  mandate                  │──MCP─▶│                  │
│  pick strategy     │        │  per-action limit         │       │  payment links   │
│  prioritize        │        │  cumulative limit         │       │  refunds         │
│  estimate value    │        │  human approval           │       │  settlements     │
│                    │        │  idempotency              │       └────────┬─────────┘
│  RecoveryProposal  │        │  hash-chained evidence    │                │
└────────────────────┘        └───────────────────────────┘                ▼
   non-deterministic                  deterministic                  actual money
   holds no credentials               pure functions                      │
                                              ▲                           │
                                              │      ┌────────────────────┘
                                    ┌─────────┴──────┴──────┐
                                    │  belay/settlement/    │
                                    │  three-way verify:    │
                                    │  authorized vs        │
                                    │  reported vs settled  │
                                    └───────────────────────┘
```

The claim this architecture buys, and the one the demo makes good on:

> A compromised, jailbroken, or simply wrong agent changes **which**
> recoveries are attempted. It cannot change the mandate, the limits, the
> approval requirement, or the evidence.

That is not a statement about prompt quality. It is enforced mechanically:
[`tests/test_layer_boundaries.py`](tests/test_layer_boundaries.py) parses the
AST of every module in `recovery/` and fails the build if the AI layer imports
anything that could authorize, execute, or record.

## What already works

The control plane is not new. It was built and tested as a general
transactional safety layer, is L3 conformant against its own published
[specification](docs/spec.md), and is being retargeted rather than rewritten:

- **Contracts** declare, per action, whether it is reversible and what the
  concrete undo is. No contract means the action is refused.
- **Dry-run planning** predicts effects before anything executes, and marks
  honestly what is measured versus estimated.
- **Policy** enforces blast-radius limits and returns the most restrictive
  verdict across every dimension that fired.
- **Human approval** parks anything policy pauses. The agent has no code path
  to approve its own action, and a granted approval is single-use.
- **Idempotent execution** guarantees a retried action calls the upstream once.
- **An append-only, hash-chained ledger** makes every decision independently
  verifiable, replayable, and Ed25519-signable into an offline-verifiable
  bundle.
- **Compensation** undoes a mistaken action in reverse order and reports
  honestly what could not be undone.

## Try the control plane today

```bash
pip install -e ".[dev]"
pytest                                              # 408 passed, ~20s
python examples/demo.py --oops                      # governed lifecycle + rewind
belay-conformance run --target belay --level 3      # L3 PASSED
```

`examples/demo.py` drives a real MCP session through the full lifecycle --
plan, pause, approve, execute, then undo a mistake -- and ends by verifying
the hash chain. Output is generated live, not scripted.

## Roadmap

| Phase | Delivers | Status |
| --- | --- | --- |
| 1 | Strip to the control plane; package skeleton; layer-boundary enforcement | **done** |
| 2 | `Money` (integer paise), `MerchantMandate` hash-pinned into the ledger | next |
| 3 | Razorpay sandbox MCP server + contract pack | |
| 4 | Cumulative and velocity limits | |
| 5 | AI recovery layer: diagnose, strategise, prioritize | |
| 6 | Live Razorpay test-mode execution + signed evidence | |
| 7 | Webhook ingestion | |
| 8 | Three-way settlement verification | |
| 9 | Adversarial benchmark and metrics | |
| 10 | Five-minute demo | |

## License

MIT -- see [`LICENSE`](LICENSE). The specification text
([`docs/spec.md`](docs/spec.md)) is additionally available under CC-BY-4.0.

Razorpay is a trademark of Razorpay Software Private Limited. This project is
independent and unaffiliated; it is built *on* Razorpay's public APIs and
their MIT-licensed [official MCP server](https://github.com/razorpay/razorpay-mcp-server).
