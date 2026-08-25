"""Razorpay integration: webhook ingestion and preflight reads.

**Deterministic.** Signature verification and event mapping are pure
functions of their inputs; the only I/O is reading a webhook payload the
caller supplies.

Belay reaches Razorpay's write APIs *only* through the official
`razorpay/razorpay-mcp-server` (MIT), launched as a wrapped stdio MCP
subprocess by `belay wrap --command`. There is deliberately no HTTP client
for payments in this package: every money-moving call must traverse the
governed lifecycle in `belay/proxy/lifecycle.py`, and a second, ungoverned
path to Razorpay would defeat that.

Webhooks are the exception, and are read-only: they are the first facts in
the system that do not originate from our own request, which is what makes
them usable as independent evidence in `belay/settlement/`.
"""
