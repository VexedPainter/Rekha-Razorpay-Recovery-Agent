"""Financial domain model: `Money`, `MerchantMandate`.

**Deterministic.** Nothing in this package may consult a model, a network
service, or a clock it does not receive by injection. Every function here is
a pure function of its arguments.

Two responsibilities:

- `money.py` -- monetary amounts as integer minor units (paise for INR),
  never floats. Razorpay denominates in minor units; so do we, end to end.
  A float on an amount path is a correctness bug, not a style preference.
- `mandate.py` -- the `MerchantMandate`: the merchant's written, hash-pinned
  grant of authority to the recovery agent. What actions are permitted, up
  to what value per action, up to what value in aggregate, over what window,
  and above what threshold a human must approve.

The mandate is the outermost boundary of the control plane. An AI proposal
that violates it is refused before planning, policy, or execution is even
considered -- see `belay/proxy/lifecycle.py`.
"""
