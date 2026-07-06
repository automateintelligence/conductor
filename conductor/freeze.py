"""Done-gate freeze guard (design §5 integrity).

The done-gate (`conductor assert run`) only means something if the assertions it runs are the
ones a human confirmed at setup, not ones the worker weakened to make a red gate green.

`record()` snapshots, per assertion id, a digest of its manifest entry plus digests of the
test files its command references. `verify()` fails closed if any snapshotted assertion was
modified or removed. ADDING new assertions (legitimate gap-closing) is allowed; WEAKENING or
REMOVING a frozen one is not. Product code that a test merely imports is never named in the
command, so it is not frozen and the worker can still implement it.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shlex
import sys

from conductor.paths import project_root

_THIS = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(
    _THIS
)  # tool code (imports) — NOT where a project's gate lives
PROJECT = project_root()
ASSERTIONS_DIR = os.path.join(PROJECT, "assertions")
DEFAULT_MANIFEST = os.path.join(ASSERTIONS_DIR, "manifest.yaml")
DEFAULT_BASELINE = os.path.join(ASSERTIONS_DIR, ".frozen")

# Entry fields that define the check; weakening any of them is tampering.
_ENTRY_FIELDS = ("command", "setup", "teardown", "timeout", "level", "kind", "claim")


def _load(manifest_path: str) -> list:
    """Single-source the manifest parse through the runner's own loader."""
    if PLUGIN_ROOT not in sys.path:
        sys.path.insert(0, PLUGIN_ROOT)
    from assertions import run as runner

    return runner.load_assertions(manifest_path)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry_digest(entry: dict) -> str:
    canon = json.dumps(
        {k: str(entry.get(k, "")) for k in _ENTRY_FIELDS}, sort_keys=True
    )
    return _sha256(canon.encode())


def _sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return _sha256(f.read())


def _is_test_file(name: str) -> bool:
    return name == "conftest.py" or (
        name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))
    )


def _collect_test_files(directory: str) -> list:
    """Files pytest treats as checks under a directory target: test_*.py / *_test.py /
    conftest.py, recursively. Product modules a test merely imports are not collected."""
    found: list = []
    for root, _dirs, names in os.walk(directory):
        if "__pycache__" in root:
            continue
        for name in names:
            if _is_test_file(name):
                found.append(os.path.join(root, name))
    return found


def _referenced_files(entry: dict, repo_root: str) -> dict:
    """The gate's check files named in command/setup/teardown -> sha256. A FILE token freezes
    that file; a DIRECTORY token freezes the test files pytest would collect under it
    (test_*.py / *_test.py / conftest.py); a GLOB token freezes its matching files. Imported
    product code is never named, so it is not frozen and stays editable by the worker."""
    files: dict = {}
    for field in ("command", "setup", "teardown"):
        raw = str(entry.get(field, "") or "")
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        for tok in tokens:
            path = tok if os.path.isabs(tok) else os.path.join(repo_root, tok)
            if os.path.isfile(path):
                files[tok] = _sha256_file(path)
            elif os.path.isdir(path):
                for fp in _collect_test_files(path):
                    files[os.path.relpath(fp, repo_root)] = _sha256_file(fp)
            elif any(c in tok for c in "*?["):
                for fp in glob.glob(path, recursive=True):
                    if os.path.isfile(fp):
                        files[os.path.relpath(fp, repo_root)] = _sha256_file(fp)
    return files


class AmbiguousAssertionsSource(RuntimeError):
    """Multiple docs/specs/*.assertions.md and no goal names one — fail closed."""


class MissingAssertionsSource(RuntimeError):
    """The goal names a spec but its `.assertions.md` sibling is absent — fail
    closed: freezing without the done-definition reopens the integrity hole."""


def _assertions_source(repo_root: str) -> dict:
    """{relpath: sha256} for the human-authored `<spec>.assertions.md` — the
    done-DEFINITION, made tamper-evident alongside the manifest and test files.

    Preferred, precise path: parse `<project>/.conductor/goal.md` for a
    `docs/specs/<name>.md` path and take its `.assertions.md` sibling; a goal
    whose named spec has no `.assertions.md` sibling — or that names no spec at
    all — fails closed. Glob `docs/specs/*.assertions.md` ONLY when no goal file
    exists: exactly one match -> use it; multiple -> fail closed (freezing every
    spec's assertions silently would let an edit to an UNRELATED spec's
    assertions break this run's gate); none -> no source entry (old behavior)."""
    goal_path = os.path.join(repo_root, ".conductor", "goal.md")
    if os.path.isfile(goal_path):
        with open(goal_path, encoding="utf-8") as f:
            goal = f.read()
        m = re.search(r"docs/specs/[^\s`'\"]+?\.md", goal)
        if m:
            rel = m.group(0) + ".assertions.md"
            path = os.path.join(repo_root, rel)
            if os.path.isfile(path):
                return {rel: _sha256_file(path)}
            raise MissingAssertionsSource(
                f"missing-assertions-source: the goal names "
                f"{m.group(0)} but {rel} does not exist"
            )
        # a goal that names no spec must not silently glob an unrelated spec's
        # assertions — fail closed
        raise MissingAssertionsSource(
            "unidentifiable-assertions-source: .conductor/goal.md exists but "
            "names no docs/specs/<name>.md path"
        )
    matches = sorted(
        glob.glob(os.path.join(repo_root, "docs", "specs", "*.assertions.md"))
    )
    if len(matches) > 1:
        rels = ", ".join(os.path.relpath(p, repo_root) for p in matches)
        raise AmbiguousAssertionsSource(
            f"ambiguous-assertions-source: no goal names a spec and multiple "
            f"candidates exist ({rels})"
        )
    if matches:
        rel = os.path.relpath(matches[0], repo_root)
        return {rel: _sha256_file(matches[0])}
    return {}


def gate_state(manifest_path: str, repo_root: str) -> dict:
    state: dict = {}
    for entry in _load(manifest_path):
        state[str(entry["id"])] = {
            "entry": _entry_digest(entry),
            "files": _referenced_files(entry, repo_root),
        }
    return state


def record(
    manifest_path: str = DEFAULT_MANIFEST,
    baseline_path: str = DEFAULT_BASELINE,
    repo_root: str = PROJECT,
) -> str:
    """Snapshot the current gate to the baseline file (called at /conductor:start)."""
    state = gate_state(manifest_path, repo_root)
    doc: dict = {"version": 1, "ids": state}
    sources = _assertions_source(repo_root)
    if sources:
        doc["sources"] = sources
    with open(baseline_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, sort_keys=True)
    return baseline_path


def verify(
    manifest_path: str = DEFAULT_MANIFEST,
    baseline_path: str = DEFAULT_BASELINE,
    repo_root: str = PROJECT,
) -> dict:
    """Return {ok, tampered: [reasons], frozen}. No baseline -> frozen False, ok True (the
    guard is opt-in by the baseline's presence). Otherwise fail-closed: a frozen id removed,
    its entry changed, or a referenced file changed/removed. New ids are allowed."""
    if not os.path.exists(baseline_path):
        return {"ok": True, "tampered": [], "frozen": False}
    try:
        with open(baseline_path, encoding="utf-8") as f:
            base_doc = json.load(f)
        base = base_doc["ids"]
        base_sources = base_doc.get("sources", {}) or {}
    except Exception as exc:
        return {
            "ok": False,
            "tampered": [f"baseline-unreadable: {exc}"],
            "frozen": True,
        }
    try:
        current = gate_state(manifest_path, repo_root)
    except Exception as exc:
        return {
            "ok": False,
            "tampered": [f"manifest-unloadable: {exc}"],
            "frozen": True,
        }
    tampered: list = []
    for aid, snap in base.items():
        cur = current.get(aid)
        if cur is None:
            tampered.append(f"{aid}: removed")
            continue
        if cur["entry"] != snap["entry"]:
            tampered.append(f"{aid}: entry-changed")
        for rel, dig in snap["files"].items():
            now = cur["files"].get(rel)
            if now is None:
                tampered.append(f"{aid}: test-file-removed ({rel})")
            elif now != dig:
                tampered.append(f"{aid}: test-file-changed ({rel})")
    # the human-authored assertions source (a pre-upgrade baseline has no "sources"
    # key and verifies exactly as before)
    for rel, dig in base_sources.items():
        path = rel if os.path.isabs(rel) else os.path.join(repo_root, rel)
        if not os.path.isfile(path):
            tampered.append(f"assertions-source-removed ({rel})")
        elif _sha256_file(path) != dig:
            tampered.append(f"assertions-source-changed ({rel})")
    return {"ok": not tampered, "tampered": tampered, "frozen": True}


def main(argv: list | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd == "lint":
        from conductor import gate_lint

        return gate_lint.main()
    if cmd == "freeze":
        try:
            print(f"[GATE] froze done-gate baseline -> {record()}")
        except (AmbiguousAssertionsSource, MissingAssertionsSource) as exc:
            print(f"[GATE] {exc}", file=sys.stderr)
            return 1
        return 0
    if cmd == "verify":
        res = verify()
        if res["ok"]:
            note = "" if res["frozen"] else " (no baseline; gate not frozen)"
            print(f"[GATE] done-gate baseline intact{note}")
            return 0
        for t in res["tampered"]:
            print(f"[GATE] TAMPERED: {t}", file=sys.stderr)
        return 1
    print("usage: conductor gate {lint|freeze|verify}", file=sys.stderr)
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
