"""The runnable demo scripts must actually run.

A demo that has silently rotted is worse than no demo: it is discovered live, in
front of an audience. These spawn the real scripts as subprocesses and check the
output they are supposed to produce, so a change to the control plane that breaks
a demo breaks the build instead.

Marked `slow`: each starts a real MCP server subprocess.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(script: str, *args: str) -> str:
    """Run a demo script, returning stdout. Fails the test on a non-zero exit.

    stderr is captured separately and only surfaced on failure -- the MCP SDK
    logs request traffic there, which would drown the assertion output.
    """
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / script), *args],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=REPO_ROOT,
    )
    assert completed.returncode == 0, (
        f"{script} exited {completed.returncode}\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr[-3000:]}"
    )
    return completed.stdout


@pytest.mark.slow
def test_the_fanout_demo_blocks_the_attack_and_verifies_itself() -> None:
    """Rs 2,000 x 40 against a Rs 50,000/day mandate: 25 through, 15 blocked."""
    out = _run("examples/demo_fanout.py")

    assert "cumulative_limit_exceeded" in out
    assert "links created   : 25" in out
    assert "links blocked   : 15" in out
    assert "actually spent  : INR 50,000.00" in out
    assert "prevented       : INR 30,000.00" in out

    # The demo checks its own claim against the sandbox's state rather than its
    # own bookkeeping. That assertion is the point of the demo, so pin it.
    assert "VERIFIED on the Razorpay sandbox: 25 links exist" in out
    assert "chain: OK" in out
    assert "coherence: OK" in out


@pytest.mark.slow
def test_the_closed_loop_demo_measures_money_recovered() -> None:
    """The track bar's hard requirement, pinned end to end.

    'Show measured money recovered across a batch' -- so the demo must produce a
    recovered figure that is *smaller* than the requested figure and derived from
    webhooks rather than from our own actions. A demo where those two numbers are
    equal would mean recovery was assumed rather than measured.
    """
    out = _run("examples/demo_recovery.py")

    assert "revenue at risk" in out
    assert "amount requested" in out
    assert "amount RECOVERED" in out
    assert "recovered count" in out

    requested = _rupees(out, "amount requested")
    recovered = _rupees(out, "amount RECOVERED")
    assert recovered > 0, "nothing was recovered -- the webhook leg did not land"
    assert recovered < requested, (
        "recovered equals requested, which means recovery was assumed rather than "
        "measured from webhooks"
    )

    # A retried delivery must not inflate the figure.
    assert "duplicates  : 1" in out

    assert "chain: OK" in out
    assert "coherence: OK" in out


def _rupees(text: str, label: str) -> float:
    """Pull `INR 6,560.00` out of a report line as a number."""
    for line in text.splitlines():
        if label in line:
            for token in line.replace(",", "").split():
                try:
                    return float(token)
                except ValueError:
                    continue
    raise AssertionError(f"no numeric value found for {label!r}")


@pytest.mark.slow
def test_the_control_plane_demo_still_governs_and_rewinds() -> None:
    """The Phase 1 regression gate, as a test rather than something I remember
    to run: plan -> pause -> approve -> execute -> undo, then verify the chain."""
    out = _run("examples/demo.py", "--oops")

    assert "chain: OK" in out
    assert "coherence: OK" in out
    assert "session fully compensated" in out
