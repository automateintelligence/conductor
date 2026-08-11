"""Tests for conductor.core.names — the single-sourced derived names."""

from __future__ import annotations

import ast
import pathlib

import pytest

from conductor import paths
from conductor.core import runkey, schema
from conductor.core.names import DerivedNames, derived_names, is_safe_segment

# The safe-segment rule, stated once and asserted against every wrapper. `..` is the case that
# matters most: the character class permits dots, so `a..b` MATCHES the pattern and still
# traverses — which is how `schema`'s independent copy came to accept `assertions/a..b` while
# `paths` refused it. `paths._safe_slug` and `runkey.is_safe_run_key` were also identical by
# coincidence rather than construction, with nothing pinning them equal.
_SEGMENT_CASES = (
    ("alpha-1a2b3c4d", True),
    ("alpha-1a2b3c4d-g2", True),
    ("a", True),
    ("0", True),
    ("a.b_c-d", True),  # dot, underscore and hyphen are all inside the class
    ("a..b", False),  # matches the pattern, traverses anyway
    ("..", False),
    ("../outside", False),
    ("a/b", False),
    ("", False),
    ("-leading", False),
    (".leading", False),
    ("Upper", False),
    ("alpha.lock", False),
)


@pytest.mark.parametrize("segment,expected", _SEGMENT_CASES)
def test_every_wrapper_of_the_segment_rule_agrees_with_it(segment, expected):
    """One case list, four predicates: the definition plus every wrapper that delegates to it.
    Asserting the wrappers rather than only the definition is the point — a wrapper that stopped
    delegating, or drifted back to its own copy, fails here."""
    assert is_safe_segment(segment) is expected, segment
    assert paths._safe_slug(segment) is expected, segment
    assert runkey.is_safe_run_key(segment) is expected, segment
    assert schema._is_safe_gate_dir(f"assertions/{segment}") is expected, segment


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


def _imported_modules(source: str) -> set[str]:
    """Every module name imported anywhere in ``source``, including inside functions.

    A substring scan of the raw text misses `from conductor import paths` and a function-local
    import; walking the AST sees both, which matters because this is the check that keeps
    conductor/paths.py able to import this module without a cycle."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            found.add(base)
            found.update(
                f"{base}.{alias.name}" if base else alias.name for alias in node.names
            )
    return found


def test_imported_modules_catches_all_import_spellings():
    """The _imported_modules AST walker catches all import spellings, including ``from conductor import paths``."""
    # Direct import caught
    source1 = "import conductor.paths"
    assert "conductor.paths" in _imported_modules(source1)

    # from conductor import paths caught (yields conductor.paths)
    source2 = "from conductor import paths"
    assert "conductor.paths" in _imported_modules(source2)

    # from conductor.core import runkey caught
    source3 = "from conductor.core import runkey"
    assert "conductor.core.runkey" in _imported_modules(source3)

    # Function-local import caught
    source4 = """
def f():
    from conductor.paths import something
"""
    assert "conductor.paths.something" in _imported_modules(source4)


def test_names_module_does_not_import_conductor_paths():
    """conductor.core.names imports nothing from conductor.paths — this keeps conductor/paths.py cycle-free."""
    module_path = (
        pathlib.Path(__file__).parent.parent.parent.parent
        / "conductor"
        / "core"
        / "names.py"
    )
    content = module_path.read_text()
    imported = _imported_modules(content)

    # Check that no imported name is conductor.paths or a submodule of it.
    for name in imported:
        assert not (name == "conductor.paths" or name.startswith("conductor.paths.")), (
            f"names.py must not import conductor.paths or its submodules, but found: {name}"
        )


def test_names_module_does_not_import_conductor_core_runkey():
    """conductor.core.names imports nothing from conductor.core.runkey — this keeps conductor/paths.py cycle-free."""
    module_path = (
        pathlib.Path(__file__).parent.parent.parent.parent
        / "conductor"
        / "core"
        / "names.py"
    )
    content = module_path.read_text()
    imported = _imported_modules(content)

    # Check that no imported name is conductor.core.runkey or a submodule of it.
    for name in imported:
        assert not (
            name == "conductor.core.runkey" or name.startswith("conductor.core.runkey.")
        ), (
            f"names.py must not import conductor.core.runkey or its submodules, but found: {name}"
        )
