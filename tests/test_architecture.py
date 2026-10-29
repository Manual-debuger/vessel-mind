"""The package's dependency direction, as the import-linter contracts in pyproject.toml state it.

core imports nothing else in vessel_mind; agent and rag import only core, and
agent never imports rag; only the eval harness imports server, and nothing
imports eval.
"""

from pathlib import Path

from importlinter.cli import EXIT_STATUS_SUCCESS, lint_imports

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_imports_follow_the_dependency_direction():
    # import-linter prints each broken contract and the import chain that breaks it.
    assert lint_imports(config_filename=str(PYPROJECT), no_cache=True) == EXIT_STATUS_SUCCESS
