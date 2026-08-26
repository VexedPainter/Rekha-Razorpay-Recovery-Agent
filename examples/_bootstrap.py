"""Fail fast, and legibly, on an unsupported Python.

This codebase uses PEP 695 (`type X = ...`) which is 3.12 syntax. On 3.11 the
failure is a `SyntaxError` raised while importing `recovery/agent.py`, pointing at a
line that is perfectly valid code. Someone seeing that reasonably concludes the
project is broken rather than that their interpreter is too old -- and on Windows
this is the DEFAULT experience, because a `uv venv` does not change what bare
`python` resolves to on PATH.

That matters more than it looks. It is the first command anyone runs on a fresh
clone, and a confusing traceback at that moment is indistinguishable from a project
that does not work.

Imported for its side effect, before anything that uses 3.12 syntax:

    import _bootstrap  # noqa: F401

A `SyntaxError` cannot be caught by the module that contains it, so the check has to
live in a separate module that is itself 3.11-parseable. Nothing here may use syntax
newer than 3.8 -- no walrus in a comprehension, no match, no `X | Y` annotations --
or the guard fails with exactly the error it exists to explain.
"""

import os
import sys

MINIMUM = (3, 12)


def _venv_hint():
    """The concrete command to run, not just the version required.

    Checks for a virtualenv in the repo root and names its interpreter directly,
    because "use Python 3.12" is advice and ".\\.venv\\Scripts\\python.exe" is an
    instruction.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    windows = os.path.join(root, ".venv", "Scripts", "python.exe")
    posix = os.path.join(root, ".venv", "bin", "python")

    candidates = (
        (windows, r".\.venv\Scripts\python.exe", r".\.venv\Scripts\Activate.ps1"),
        (posix, "./.venv/bin/python", "source ./.venv/bin/activate"),
    )
    for candidate, shown, activate in candidates:
        if os.path.exists(candidate):
            return (
                "A virtualenv with a supported Python already exists here.\n"
                "  Run the command again with it explicitly:\n\n"
                "      {} {}\n\n"
                "  Or activate it first, so bare `python` resolves to it:\n\n"
                "      {}".format(shown, " ".join(sys.argv) if sys.argv else "<script>", activate)
            )

    return (
        "No virtualenv found in the repository root. Create one:\n\n"
        "      uv venv\n"
        "      uv pip install -e .\n\n"
        "  then run the command again with .venv's interpreter."
    )


def check():
    if sys.version_info >= MINIMUM:
        return

    running = ".".join(str(part) for part in sys.version_info[:3])
    required = ".".join(str(part) for part in MINIMUM)

    sys.stderr.write(
        "\n"
        f"  This project requires Python {required} or newer. You are running {running}.\n"
        "\n"
        "  It is not a version preference: the code uses PEP 695 type-alias syntax\n"
        f"  (`type X = ...`), which {running} cannot parse. Without this check you would\n"
        "  have seen a SyntaxError pointing at a valid line of source.\n"
        "\n"
        f"  {_venv_hint()}\n"
        "\n"
        f"  Interpreter in use: {sys.executable}\n"
        "\n"
    )
    raise SystemExit(1)


check()
