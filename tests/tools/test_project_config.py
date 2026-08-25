"""Regression tests for the project-wide fast-gate configuration."""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parents[2]


def _contains_option(addopts: str, option: str) -> bool:
    """Return whether *option* is an exact pytest argument in ``addopts``."""
    return option in shlex.split(addopts)


def _marker_expressions(arguments: list[str]) -> list[str]:
    """Return marker expressions from pytest arguments in their effective order."""
    marker_expressions = []
    for index, argument in enumerate(arguments):
        if argument == "-m" and index + 1 < len(arguments):
            marker_expressions.append(arguments[index + 1])
        elif argument.startswith("-m="):
            marker_expressions.append(argument.removeprefix("-m="))
    return marker_expressions


def _effective_marker_expression(addopts: str) -> str:
    """Return the final marker expression passed through pytest's addopts."""
    return _marker_expressions(shlex.split(addopts))[-1]


def _test_job_pytest_commands() -> list[list[str]]:
    """Return parsed pytest commands from executable scalars in CI's test job."""
    workflow_path = PROJECT_ROOT / ".github" / "workflows" / "ci.yaml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    test_steps = workflow["jobs"]["test"]["steps"]
    commands = []
    for step in test_steps:
        run = step.get("run")
        if not isinstance(run, str):
            continue
        arguments = shlex.split(" ".join(run.split()))
        if arguments and arguments[0] == "pytest":
            commands.append(arguments)
    return commands


def _declared_markers(project_config: dict) -> list[str]:
    """Marker names declared in `[tool.pytest.ini_options].markers`."""
    declarations = project_config["tool"]["pytest"]["ini_options"]["markers"]
    return [declaration.split(":", 1)[0].strip() for declaration in declarations]


def test_fast_gate_has_the_effective_safe_branch_coverage_configuration() -> None:
    """The default pytest gate measures the intended suite at its floor.

    The set of markers the default gate must exclude is derived from the
    declared markers rather than hard-coded, so adding a new `live_*` marker
    without also excluding it from the default gate fails this test instead
    of silently letting real API calls (and real costs) into CI.
    """
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as config_file:
        project_config = tomllib.load(config_file)

    addopts = project_config["tool"]["pytest"]["ini_options"]["addopts"]
    fail_under = project_config["tool"]["coverage"]["report"]["fail_under"]

    assert _contains_option(addopts, "--cov-branch")
    assert _contains_option(addopts, "--cov") or _contains_option(addopts, "--cov=belay")
    assert not _contains_option(addopts, "--no-cov")

    effective = _effective_marker_expression(addopts)
    must_exclude = [
        marker
        for marker in _declared_markers(project_config)
        if marker == "slow" or marker.startswith("live_")
    ]
    assert must_exclude, "expected at least the `slow` marker to be declared"
    for marker in must_exclude:
        assert f"not {marker}" in effective, (
            f"marker {marker!r} is declared but not excluded from the default "
            f"gate ({effective!r}) -- a `live_*` marker left in the default "
            f"suite spends real credits in CI"
        )
    assert fail_under >= 83


def test_ci_test_job_uses_safe_default_and_slow_pytest_gates() -> None:
    """CI runs default and slow tests without empty or live marker selections."""
    pytest_commands = _test_job_pytest_commands()

    assert ["pytest"] in pytest_commands
    assert any(
        "--no-cov" in command
        and "slow and not live_conformance" in _marker_expressions(command)
        for command in pytest_commands
    )
    for command in pytest_commands:
        for marker_expression in _marker_expressions(command):
            assert marker_expression
            assert "live_conformance" not in marker_expression.replace(
                "not live_conformance", ""
            )


def test_contains_option_requires_a_standalone_argument() -> None:
    """Coverage options with a matching substring must not satisfy the gate."""
    assert not _contains_option("--cov-branching --cov=belay", "--cov-branch")
    assert _contains_option("--cov-branch --cov=belay", "--cov-branch")
