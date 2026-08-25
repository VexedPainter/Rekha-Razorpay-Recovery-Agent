"""Three-way settlement verification.

**Deterministic and pure.** `verify()` performs no I/O: it is a fold over
event lists the caller supplies, in the same spirit as
`belay/ledger/replay.py`. That is what makes its verdict replayable and
signable into an evidence bundle.

The question this package answers is not "did we send the right request?"
but "did the money that actually moved match what we authorized?" -- which
requires comparing three independently-sourced legs:

- **AUTHORIZED** -- folded from our own hash-chained ledger: `spend` effects
  that passed policy and reached `step_committed`.
- **REPORTED** -- Razorpay's webhook assertions of what happened
  (`belay/razorpay/webhooks.py`).
- **SETTLED** -- Razorpay's settlement reconciliation report, i.e. money
  that actually moved.

Comparing only AUTHORIZED against REPORTED would be self-referential: both
derive from our own request. The SETTLED leg is what makes this
*verification* rather than bookkeeping.

Verdicts follow the honest four-value taxonomy `belay/rewind/service.py`
established for Verified Rewind -- `matched` / `mismatched` / `pending` /
`unverifiable` -- for the same reason it exists there: absence of evidence
must never be reported as agreement.
"""
