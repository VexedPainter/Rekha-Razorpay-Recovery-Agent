"""Adversarial benchmark and metrics.

**Deterministic.** Seeded cohort generation and an injected `Clock`, so a
benchmark run is reproducible: the same seed and the same clock must produce
the same table, or a regression is indistinguishable from noise.

Two halves, and both matter:

- `attacks/` -- one module per adversarial scenario. Every scenario asserts
  *which layer* refused the action, not merely that something did. A test
  that only checks "it was blocked" cannot tell a designed control from a
  lucky accident.
- `metrics.py` -- every metric is computed as a fold over the hash-chained
  ledger, so the measurements are as verifiable as the actions they measure.

The benign control cohort is not optional. A system that blocks everything
scores a perfect block rate, so an unauthorized-action block rate is
uninterpretable without a false-positive rate measured over legitimate
traffic.
"""
