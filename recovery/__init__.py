"""AI revenue recovery intelligence.

**Non-deterministic.** This is the only package in the repository permitted
to consult a language model.

It proposes, and nothing more. It holds no Razorpay credentials, opens no
database, and appends to no ledger. Its sole output is a `RecoveryProposal`,
and its sole route to the outside world is an MCP client session against the
Rekha proxy -- the same governed surface any other agent would face.

This boundary is enforced mechanically, not by convention:
`tests/test_layer_boundaries.py` fails the build if any module here imports
`rekha.ledger`, `rekha.approvals`, `rekha.policy`, `rekha.executor`, or
`rekha.settlement`.

The reason is the central claim of the project. A model is promptable,
non-deterministic, and cannot be audited after the fact. So the model is
given the one job it is genuinely better at -- judgment under uncertainty
about *what to attempt* -- and is structurally denied the ability to decide
what is *permitted*. A compromised, jailbroken, or simply wrong agent
changes which recoveries are attempted. It cannot change the mandate, the
limits, the approval requirement, or the evidence.
"""
