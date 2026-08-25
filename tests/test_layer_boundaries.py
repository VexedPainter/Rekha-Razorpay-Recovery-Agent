"""The AI layer proposes; the control plane authorizes.

This is the central architectural claim of the project, and it is worth
exactly nothing as a sentence in a README. So it is enforced here instead:
if any module under `recovery/` ever imports an enforcement module, this
test fails and the build fails with it.

`recovery/` is the only package permitted to consult a language model. In
exchange it is denied every capability that could turn a model's output into
a financial fact directly:

- `belay.ledger`     -- it cannot write evidence, so it cannot forge it.
- `belay.approvals`  -- it cannot approve its own action (spec section 12,
                        no-self-approval, extended to the AI layer).
- `belay.policy`     -- it cannot evaluate, relax, or bypass a limit.
- `belay.executor`   -- it cannot execute; it can only ask the proxy to.
- `belay.settlement` -- it cannot mark its own outcome verified.
- `belay.finance.mandate` -- it cannot construct or reinterpret a mandate.

`belay.finance.money` is deliberately NOT forbidden, and the reason is the line
this whole test is drawing. A `MerchantMandate` is *authority*: if the AI layer
could build one it could widen its own permissions, and the boundary would be
decorative. `Money` is *arithmetic*: an immutable integer amount whose possession
grants nothing. Forbidding it would force the AI layer to pass raw ints around
and have the control plane infer their units -- precisely the bug `Money` exists
to prevent. Denying a value type buys no safety and costs correctness.

What `recovery/` may otherwise import is deliberately narrow: `belay.errors` (to
recognise a refusal it received) and the MCP client SDK (its one route
outward, through the governed proxy). Everything else it needs, it must be
handed.

The consequence is the claim the demo makes good on: a compromised,
jailbroken, or merely wrong agent changes *which* recoveries are attempted.
It cannot change the mandate, the limits, the approval requirement, or the
evidence.

Implemented with `ast`, not string matching, so a commented-out or
string-literal mention of a forbidden module is not a false positive, and an
aliased or conditional import is not a false negative.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AI_PACKAGE = REPO_ROOT / "recovery"

#: Importing any of these from `recovery/` would hand the AI layer a
#: capability the control plane exists to withhold from it.
FORBIDDEN_ROOTS = frozenset(
    {
        "belay.ledger",
        "belay.approvals",
        "belay.policy",
        "belay.executor",
        "belay.settlement",
        # Note `belay.finance.mandate`, not `belay.finance`. The distinction is
        # between a *capability* and a *value type*:
        #
        #   `mandate` is authority. If the AI layer could construct or reinterpret
        #   a `MerchantMandate`, it could widen its own permissions, and the whole
        #   boundary would be decorative.
        #
        #   `money` is arithmetic. `Money` is an immutable integer amount with a
        #   currency; holding one grants nothing. Forbidding it would force the AI
        #   layer to pass raw ints around and have the control plane guess at
        #   their units -- which is exactly the class of bug `Money` exists to
        #   prevent. Denying a value type buys no safety and costs correctness.
        "belay.finance.mandate",
    }
)


def _imported_modules(source: str) -> set[str]:
    """Every module name imported by `source`, dotted and fully qualified.

    Handles both `import a.b` and `from a.b import c`, including aliased
    (`import a.b as c`) and function-local imports, since either would be a
    real capability leak. Relative imports (`from . import x`) resolve
    within `recovery/` itself and are therefore never forbidden.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import, internal to recovery/
                continue
            if node.module:
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _violations(imported: set[str]) -> set[str]:
    """Forbidden roots hit by `imported`, matching a module or any submodule."""
    return {
        root
        for root in FORBIDDEN_ROOTS
        for name in imported
        if name == root or name.startswith(f"{root}.")
    }


def test_ai_layer_cannot_import_the_enforcement_layer() -> None:
    """No module under `recovery/` may import an enforcement module."""
    offenders: dict[str, set[str]] = {}
    for path in sorted(AI_PACKAGE.rglob("*.py")):
        found = _violations(_imported_modules(path.read_text(encoding="utf-8")))
        if found:
            offenders[str(path.relative_to(REPO_ROOT))] = found

    assert not offenders, (
        "The AI layer must propose, never authorize. These modules import "
        "enforcement code they must not be able to reach:\n"
        + "\n".join(f"  {mod}: {sorted(bad)}" for mod, bad in sorted(offenders.items()))
        + "\n\nIf the AI layer needs a value from one of these, it must be "
        "passed in by the caller, not imported."
    )


def test_the_boundary_check_actually_detects_a_violation() -> None:
    """Guard against the guard: a test that can never fail proves nothing.

    The test above passes trivially while `recovery/` is empty, so it would
    also pass if `_imported_modules` silently returned nothing. This pins
    the detector against a known-bad sample.
    """
    sneaky = "\n".join(
        [
            "import belay.errors",  # allowed
            "from belay.ledger.store import LedgerStore",  # forbidden
            "def f():",
            "    import belay.policy.engine  # forbidden, function-local",
            "    from belay.approvals import queue as q  # forbidden, aliased",
        ]
    )
    assert _violations(_imported_modules(sneaky)) == {
        "belay.ledger",
        "belay.policy",
        "belay.approvals",
    }


def test_money_is_allowed_but_mandate_is_not() -> None:
    """The capability/value-type line, pinned.

    Getting this backwards in either direction is a real mistake: forbidding
    `Money` would push raw ints across the boundary, and permitting `mandate`
    would let the AI layer rewrite its own authority.
    """
    assert _violations(_imported_modules("from belay.finance.money import Money")) == set()
    assert _violations(_imported_modules("from belay.finance import money")) == set()
    assert _violations(
        _imported_modules("from belay.finance.mandate import MerchantMandate")
    ) == {"belay.finance.mandate"}
    assert _violations(_imported_modules("import belay.finance.mandate")) == {
        "belay.finance.mandate"
    }


def test_allowed_imports_are_not_flagged() -> None:
    """`belay.errors` and the MCP SDK are the AI layer's legitimate imports."""
    fine = "\n".join(
        [
            "from belay.errors import BelayError",
            "from mcp import ClientSession",
            "from . import proposal",
            "import json",
        ]
    )
    assert _violations(_imported_modules(fine)) == set()
