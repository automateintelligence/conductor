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
import shlex
import sys

from conductor.paths import (
    AmbiguousSpecReference,
    InvalidSpecRoots,
    project_root,
    resolve_gate,
    spec_from_goal_text,
    spec_roots,
)

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


def _source_candidates(spec_path: str) -> list[str]:
    """The accepted assertions-source spellings for a spec path, PREFERRED FIRST.

    spec-craft (`/spec-craft:executable-assertions`) WRITES `docs/specs/<stem>.assertions.md`
    and conductor only READS it, so the stem form is the binding one and is tried first. The
    legacy `<spec>.md.assertions.md` form — what conductor demanded when it appended
    `.assertions.md` to a path that already ended in `.md` — stays accepted so repos that
    bridged the mismatch with a committed file or symlink keep resolving and keep verifying.

    Preference alone does NOT decide when both are present — see `_pick_source`.

    A path that already names an `.assertions.md` file is taken verbatim."""
    if spec_path.endswith(".assertions.md"):
        return [spec_path]
    stem = spec_path[:-3] if spec_path.endswith(".md") else spec_path
    candidates = [stem + ".assertions.md"]
    legacy = spec_path + ".assertions.md"
    if legacy not in candidates:
        candidates.append(legacy)
    return candidates


class AmbiguousAssertionsSource(RuntimeError):
    """Multiple ``<spec-root>/*.assertions.md`` across ``paths.spec_roots()`` and no goal names
    one — fail closed."""


class MissingAssertionsSource(RuntimeError):
    """The goal names a spec but its `.assertions.md` sibling is absent — fail
    closed: freezing without the done-definition reopens the integrity hole."""


class DivergentAssertionsSource(RuntimeError):
    """Both accepted spellings of one spec's assertions source exist and say DIFFERENT
    things, so the repo holds two disagreeing done-definitions and preference order would
    pick one of them blind — fail closed."""


class UnreadableAssertionsSource(RuntimeError):
    """A candidate assertions source EXISTS but cannot be read, so its bytes are unknown.

    Its own condition, deliberately not folded into ``DivergentAssertionsSource``: divergence
    is a claim that two done-definitions disagree, and nothing here compared them. The remedy
    differs too — fix the mode or the ownership, versus reconcile two files — and a freeze
    refusal that names the wrong one sends the operator to the wrong repair. Unreadable is also
    not "absent" (``MissingAssertionsSource``): deleting the file would make the freeze
    succeed against the remaining spelling, while a mode change must not."""


def _source_digest(path: str, repo_root: str) -> str:
    """``_sha256_file`` for an assertions-source candidate, with an unreadable file turned into
    a domain refusal. The raw ``OSError`` escaped ``record()`` as a traceback: ``freeze.main``
    catches domain errors only, so ``gate freeze`` crashed instead of refusing. It did fail
    closed — no baseline was written — but nothing greppable said why."""
    try:
        return _sha256_file(path)
    except OSError as exc:
        rel = os.path.relpath(path, repo_root)
        raise UnreadableAssertionsSource(
            f"unreadable-assertions-source: {rel} exists but could not be read ({exc}); "
            "the done-definition cannot be frozen without its bytes — fix the file's "
            "permissions (removing it is a different decision, and a louder one)"
        ) from exc


def _pick_source(candidates: list[str], repo_root: str) -> str | None:
    """THE assertions-source choice among `_source_candidates`' spellings, or None when none
    exists.

    Preference order alone is only safe while the spellings AGREE. The sequence that breaks it
    is the one repos actually took: spec-craft wrote `<stem>.assertions.md`, conductor could
    not consume it, the repo copied or symlinked it to `<spec>.md.assertions.md` and
    MAINTAINED that copy, and both are committed. Preferring the stem form there freezes the
    ABANDONED file, and every later edit to the maintained one is invisible to the baseline —
    the done-definition stops being tamper-evident exactly where it matters.

    So: same file (equal realpath — the committed-symlink bridge) or same bytes (the committed
    copy) means the disagreement is not real, and the preferred spelling is taken silently.
    Genuinely different bytes are two done-definitions with no way to tell which one the human
    confirmed; refuse and name both rather than freeze either. A candidate whose bytes cannot be
    READ is neither case and raises ``UnreadableAssertionsSource`` (see ``_source_digest``)."""
    present = [p for p in candidates if os.path.isfile(p)]
    if not present:
        return None
    chosen = present[0]
    chosen_real = os.path.realpath(chosen)
    chosen_digest = None
    for other in present[1:]:
        if os.path.realpath(other) == chosen_real:
            continue
        if chosen_digest is None:
            chosen_digest = _source_digest(chosen, repo_root)
        if _source_digest(other, repo_root) == chosen_digest:
            continue
        a, b = (os.path.relpath(p, repo_root) for p in (chosen, other))
        raise DivergentAssertionsSource(
            f"divergent-assertions-source: {a} and {b} are both present and their "
            "contents differ, so this spec has two disagreeing done-definitions; delete "
            "or reconcile one before freezing the gate"
        )
    return chosen


def _assertions_source(repo_root: str) -> tuple[dict, str]:
    """({relpath: sha256}, via) for the human-authored `<spec>.assertions.md` —
    the done-DEFINITION, made tamper-evident alongside the manifest and test
    files. `via` is "env", "goal", "glob", or "none" (how the source was discovered).

    Highest precedence: `$CONDUCTOR_ASSERTIONS_SOURCE` names THIS run's spec (its
    `.md` — the `.assertions.md` sibling is taken — or the `.assertions.md` itself).
    `/conductor:start` sets it for the step-3 freeze, which runs BEFORE the goal is
    recorded: in a multi-spec repo the glob below would otherwise fail closed
    (`ambiguous-assertions-source`) or a stale `goal.md` would bind the wrong spec.
    Else, precise path: parse `<project>/.conductor/goal.md` for a
    `<spec-root>/<name>.md` path and take its assertions sibling under either
    accepted spelling (`_source_candidates`: spec-craft's `<stem>.assertions.md`
    first, then the legacy `<spec>.md.assertions.md`); a goal whose named spec has
    NEITHER — or that names no spec at all — fails closed. Glob
    `<spec-root>/*.assertions.md` (which matches both spellings) ONLY when no goal file
    exists: exactly one match -> use it; multiple -> fail closed (freezing every
    spec's assertions silently would let an edit to an UNRELATED spec's
    assertions break this run's gate); none -> no source entry (old behavior).

    Both the prose parse and the glob search `paths.spec_roots()`, which is `docs/specs`
    unless `$CONDUCTOR_SPEC_ROOTS` says otherwise. They MUST stay the same set: a goal that
    resolves through one root while the glob searches another would freeze one spec's
    done-definition and verify against a different one's."""
    override = os.environ.get("CONDUCTOR_ASSERTIONS_SOURCE")
    if override:
        base = (
            override if os.path.isabs(override) else os.path.join(repo_root, override)
        )
        candidates = _source_candidates(base)
        path = _pick_source(candidates, repo_root)
        if path:
            return {
                os.path.relpath(path, repo_root): _source_digest(path, repo_root)
            }, "env"
        raise MissingAssertionsSource(
            f"missing-assertions-source: CONDUCTOR_ASSERTIONS_SOURCE names "
            f"{override} but none of {', '.join(candidates)} exist"
        )
    goal_path = os.path.join(repo_root, ".conductor", "goal.md")
    if os.path.isfile(goal_path):
        with open(goal_path, encoding="utf-8") as f:
            goal = f.read()
        spec = spec_from_goal_text(goal)
        if spec:
            rels = _source_candidates(spec)
            path = _pick_source([os.path.join(repo_root, r) for r in rels], repo_root)
            if path:
                return {
                    os.path.relpath(path, repo_root): _source_digest(path, repo_root)
                }, "goal"
            raise MissingAssertionsSource(
                f"missing-assertions-source: the goal names "
                f"{spec} but none of {', '.join(rels)} exist"
            )
        # a goal that names no spec must not silently glob an unrelated spec's
        # assertions — fail closed
        roots = ", ".join(f"{r}/<name>.md" for r in spec_roots())
        raise MissingAssertionsSource(
            "unidentifiable-assertions-source: .conductor/goal.md exists but "
            f"names no {roots} path (and no `spec:` line); set CONDUCTOR_SPEC_ROOTS "
            "if this project keeps specs elsewhere"
        )
    matches = sorted(
        {
            match
            for root in spec_roots()
            for match in glob.glob(
                os.path.join(repo_root, *root.split("/"), "*.assertions.md")
            )
        }
    )
    if len(matches) > 1:
        rels = ", ".join(os.path.relpath(p, repo_root) for p in matches)
        raise AmbiguousAssertionsSource(
            f"ambiguous-assertions-source: no goal names a spec and multiple "
            f"candidates exist ({rels})"
        )
    if matches:
        rel = os.path.relpath(matches[0], repo_root)
        return {rel: _source_digest(matches[0], repo_root)}, "glob"
    return {}, "none"


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
    sources, via = _assertions_source(repo_root)
    if sources:
        doc["sources"] = sources
        doc["sources_via"] = via
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
    if base_sources:
        # bind the baseline to the CURRENT goal selection: a stale spec-A baseline
        # must not stay green after goal.md moves to spec B (whose assertions
        # would be unfrozen), nor may deleting goal.md fall open through the
        # single-file glob path. Old baselines without "sources" skip this.
        base_via = base_doc.get("sources_via")
        goal_file = os.path.join(repo_root, ".conductor", "goal.md")
        if base_via == "goal" and not os.path.isfile(goal_file):
            tampered.append(
                "assertions-source-unresolvable: .conductor/goal.md (which "
                "selected the frozen assertions source) was removed"
            )
        else:
            try:
                current_sources, _via = _assertions_source(repo_root)
                current_set: set | None = set(current_sources)
            except Exception as exc:  # ambiguous/missing now -> fail closed
                current_set = None
                tampered.append(f"assertions-source-unresolvable: {exc}")
            if current_set is not None and current_set != set(base_sources):
                tampered.append(
                    "assertions-source-set-changed "
                    f"(recorded {sorted(base_sources)}, current {sorted(current_set)})"
                )
    return {"ok": not tampered, "tampered": tampered, "frozen": True}


def main(argv: list | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd == "lint":
        from conductor import gate_lint

        return gate_lint.main()
    # A typo'd $CONDUCTOR_SPEC_ROOTS must REFUSE, not end in a traceback. Checked before
    # resolve_gate because that parses .conductor/goal.md through the very same roots, so the
    # crash would otherwise escape upstream of the domain-error handler around `record` below.
    try:
        spec_roots()
    except InvalidSpecRoots as exc:
        print(f"[GATE] {exc}", file=sys.stderr)
        return 1
    # Per-spec gate (multi-spec safety): freeze/verify the manifest+baseline resolve_gate()
    # points at — assertions/<slug>/ for a namespaced run, else flat — with the same §5
    # fail-closed verdict the done-gate runner uses (single-sourced in paths.resolve_gate).
    root = project_root()
    gate = resolve_gate(root)
    # §5: refuse to freeze OR verify a gate this run is DODGING (repointed run metadata). An
    # edited .conductor/run_branch or a planted alternate manifest must not read green — and
    # freezing one would LAUNDER it into a valid baseline that later passes `assert run`.
    if cmd in ("freeze", "verify") and gate.fail_closed:
        print(f"[GATE] TAMPERED: {gate.fail_closed}", file=sys.stderr)
        return 1
    if cmd == "freeze":
        try:
            print(
                "[GATE] froze done-gate baseline -> "
                + record(gate.manifest, gate.baseline, root)
            )
        except (
            AmbiguousAssertionsSource,
            DivergentAssertionsSource,
            MissingAssertionsSource,
            UnreadableAssertionsSource,
            AmbiguousSpecReference,
            InvalidSpecRoots,
        ) as exc:
            print(f"[GATE] {exc}", file=sys.stderr)
            return 1
        return 0
    if cmd == "verify":
        res = verify(gate.manifest, gate.baseline, root)
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
