"""A-DH-1 — host vocabulary is confined to the adapter layer (property).

Source: docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.assertions.md §3.

Claim: no Python module outside the host-adapter layer contains a Claude slash-command
invocation, a Codex dollar-prefixed skill invocation, ``CLAUDE_PLUGIN_ROOT``, or a
host-specific permission or sandbox flag string.

The two file sets are DERIVED FROM THE PACKAGE, never hand-listed: the adapter set is the set
of modules the adapter loader (``base.load``) actually resolves plus the package those modules
live in; the core set is every other Python module in the installed ``conductor`` package. A
hand-maintained exclusion list is the failure mode this derivation exists to prevent — a new
core module would simply never be scanned.

Bare executable names (``claude``, ``codex``) are deliberately NOT in the token list: they are
too ambiguous to match reliably in prose, and A-DH-2 catches wrong-host spawning behaviourally
instead. The assertion spec says so explicitly; do not add them.
"""

from __future__ import annotations

import importlib
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conductor.hosts import base  # noqa: E402  (needs ROOT on sys.path first)

#: The spec's own enumeration, in two subsets. The must-contain clause needs them apart.
CLAUDE_TOKENS = (
    "/conductor:",
    "/spec-craft:",
    "CLAUDE_PLUGIN_ROOT",
    "--dangerously-skip-permissions",
)
CODEX_TOKENS = (
    "$conductor:",
    "$spec-craft:",
    "--dangerously-bypass-approvals-and-sandbox",
    "--sandbox",
)
ALL_TOKENS = CLAUDE_TOKENS + CODEX_TOKENS


def _package_root() -> pathlib.Path:
    package = importlib.import_module("conductor")
    assert package.__file__ is not None
    return pathlib.Path(package.__file__).resolve().parent


def _adapter_files() -> set[pathlib.Path]:
    """The modules ``base.load`` resolves, plus the package they live in.

    Resolved by ASKING THE LOADER for every supported host and reading the class's own
    ``__module__`` — so an adapter moved to a new file, or a third host added to
    ``HOST_IDS``, is picked up without editing this assertion.
    """
    modules = set()
    for host_id in base.HOST_IDS:
        adapter = base.load(host_id)
        module = importlib.import_module(type(adapter).__module__)
        assert module.__file__ is not None, host_id
        modules.add(pathlib.Path(module.__file__).resolve())
    files = set(modules)
    for package_dir in {path.parent for path in modules}:
        files |= {p.resolve() for p in package_dir.rglob("*.py")}
    return files


def _core_files() -> set[pathlib.Path]:
    """Every other Python module in the package. Docs, specs, tests and the assertions file
    live outside the package tree and are therefore outside both sets by construction."""
    everything = {p.resolve() for p in _package_root().rglob("*.py")}
    return everything - _adapter_files()


def _hits(files: set[pathlib.Path], tokens: tuple[str, ...]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(files):
        text = path.read_text(encoding="utf-8", errors="surrogateescape")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for token in tokens:
                if token in line:
                    found.setdefault(token, []).append(
                        f"{path}:{lineno}: {line.strip()[:110]}"
                    )
    return found


def test_the_scan_sees_a_populated_adapter_tree() -> None:
    """Anti-stub: an empty or missing package must not pass vacuously.

    Satisfied by the permission/sandbox flags alone, so it stays valid whichever way the plan
    writer settles Codex's skill-invocation syntax.
    """
    adapter_files = _adapter_files()
    assert adapter_files, "the adapter loader resolved no modules at all"
    assert _hits(adapter_files, CLAUDE_TOKENS), (
        "no Claude-specific token anywhere in the adapter set — the scan is looking at an "
        f"empty or wrong tree: {sorted(adapter_files)}"
    )
    assert _hits(adapter_files, CODEX_TOKENS), (
        "no Codex-specific token anywhere in the adapter set — the scan is looking at an "
        f"empty or wrong tree: {sorted(adapter_files)}"
    )


def test_the_core_set_is_a_real_disjoint_population() -> None:
    """Anti-stub for the other half: an empty core set would make the must-not-contain clause
    unfalsifiable. The two sets must also be disjoint, or an adapter counted as core would
    fail the invariant it is exempt from."""
    core, adapters = _core_files(), _adapter_files()
    assert core, "the core set is empty; the must-not-contain clause would be vacuous"
    assert not (core & adapters), sorted(core & adapters)


def test_no_core_module_contains_host_vocabulary() -> None:
    """Must-not-contain: any token from either subset in any core-set module."""
    hits = _hits(_core_files(), ALL_TOKENS)
    report = "\n".join(
        f"  {token}\n" + "\n".join(f"    {loc}" for loc in locations)
        for token, locations in sorted(hits.items())
    )
    assert not hits, (
        "host vocabulary found outside the adapter layer — every string below belongs in "
        f"conductor/hosts/:\n{report}"
    )
