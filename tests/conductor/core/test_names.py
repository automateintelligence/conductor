"""Tests for conductor.core.names — the single-sourced derived names."""

from __future__ import annotations

import pathlib

from conductor.core.names import DerivedNames, derived_names


def test_derived_names_returns_expected_literals():
    """derived_names produces the exact format strings — as literals, not computed."""
    names = derived_names("alpha-1a2b3c4d")
    # These literals must never be built from the module's own constants — they pin the format.
    assert names.gate_dir == "assertions/alpha-1a2b3c4d"
    assert names.integration_branch == "conductor/run-alpha-1a2b3c4d"


def test_derived_names_returns_namedtuple_with_accessible_fields():
    """The returned value is a NamedTuple whose fields are reachable as .gate_dir and .integration_branch."""
    names = derived_names("beta-5e6f7g8h")
    assert isinstance(names, DerivedNames)
    assert isinstance(names, tuple)
    assert names[0] == "assertions/beta-5e6f7g8h"
    assert names[1] == "conductor/run-beta-5e6f7g8h"
    assert names.gate_dir == names[0]
    assert names.integration_branch == names[1]


def test_derived_names_with_generation_suffix():
    """A generation-suffixed key flows through unchanged into both names."""
    names = derived_names("alpha-1a2b3c4d-g2")
    assert names.gate_dir == "assertions/alpha-1a2b3c4d-g2"
    assert names.integration_branch == "conductor/run-alpha-1a2b3c4d-g2"

    names = derived_names("alpha-1a2b3c4d-g10")
    assert names.gate_dir == "assertions/alpha-1a2b3c4d-g10"
    assert names.integration_branch == "conductor/run-alpha-1a2b3c4d-g10"


def test_names_module_does_not_import_conductor_paths():
    """conductor.core.names imports nothing from conductor.paths — this keeps conductor/paths.py cycle-free."""
    module_path = (
        pathlib.Path(__file__).parent.parent.parent.parent
        / "conductor"
        / "core"
        / "names.py"
    )
    content = module_path.read_text()
    # Check for import statements that would create a cycle.
    assert "from conductor.paths" not in content
    assert "import conductor.paths" not in content


def test_names_module_does_not_import_conductor_core_runkey():
    """conductor.core.names imports nothing from conductor.core.runkey — this keeps conductor/paths.py cycle-free."""
    module_path = (
        pathlib.Path(__file__).parent.parent.parent.parent
        / "conductor"
        / "core"
        / "names.py"
    )
    content = module_path.read_text()
    # Check for import statements that would create a cycle.
    assert "from conductor.core.runkey" not in content
    assert "import conductor.core.runkey" not in content
