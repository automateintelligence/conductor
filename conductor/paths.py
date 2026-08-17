"""Where a conductor run's state and done-gate live.

The plugin dir holds read-only TOOL CODE; a run's state + gate belong to the PROJECT — the git
repo you invoke conductor from. ``bin/conductor`` resolves the project once and exports
``CONDUCTOR_HOME`` so the runner, the freeze guard, and the handoff writer all agree on the same
location. Kept in one module so those callers cannot diverge.
"""

from __future__ import annotations

import glob
import hashlib
import os
import pathlib
import re
import subprocess
from typing import NamedTuple

from conductor.core.names import derived_names, is_safe_segment


def project_root() -> str:
    """The PROJECT that owns run state + the done-gate: ``$CONDUCTOR_HOME``, else the git repo
    of the current directory, else the current directory. Distinct from the plugin dir (tool
    code), which must never hold a project's gate/state."""
    home = os.environ.get("CONDUCTOR_HOME")
    if home:
        return home
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
        if top:
            return top
    except Exception:
        pass
    return os.getcwd()


# --- Per-spec done-gate location (multi-spec safety) -----------------------------------
#
# The done-gate (manifest.yaml, .frozen, tests, run/results.json) is a TRACKED path. Left
# flat at ``assertions/`` it is a single per-repo slot: two specs conducted in sibling
# worktrees each rebuild that one slot on their own branch and contend for it at the shared
# base — whichever merges last defines ``assertions/`` on the default branch and drops the
# other's frozen gate. Namespacing the gate at ``assertions/<slug>/`` lets sibling specs
# coexist. ``spec_slug`` is the single source of that slug (``branches.run_branch_name``
# reuses it) so the gate dir and the run branch never diverge.


def spec_slug(spec_path: str) -> str:
    """Deterministic ref-safe slug for a spec path — the SINGLE source shared by the run
    branch (``conductor/run-<slug>``) and the per-spec gate dir (``assertions/<slug>/``).

    Slug = the spec filename's stem, lowercased, non-``[a-z0-9._-]`` runs collapsed to one
    hyphen, dot runs to one dot, stripped of leading/trailing ``-``/``.``. A stem that
    strips to nothing, cannot start with an alphanumeric, or would end in ``.lock`` (all
    git-ref-invalid) falls back to a deterministic ``spec-<sha256[:8]>`` of the full path."""
    stem = pathlib.PurePath(spec_path).stem.lower()
    slug = re.sub(r"\.{2,}", ".", re.sub(r"[^a-z0-9._-]+", "-", stem)).strip("-.")
    if not slug or not re.match(r"[a-z0-9]", slug) or slug.endswith(".lock"):
        slug = "spec-" + hashlib.sha256(spec_path.encode()).hexdigest()[:8]
    return slug


def _safe_slug(s: str) -> bool:
    """Whether ``s`` is safe to use as the ``assertions/<slug>`` filesystem component: a single
    ref-safe segment (what ``spec_slug`` guarantees), with no path separators and no ``..``. An
    edited ``.conductor/run_branch`` — now a path component — that doesn't qualify (e.g.
    ``conductor/run-../../outside``) is rejected so it cannot traverse out of the gate dir.

    A thin delegation to ``names.is_safe_segment``, which is THE definition. It kept its own copy
    of the pattern until ``schema``'s copy drifted from it (``assertions/a..b`` was writable and
    unresolvable); the name stays because it is used a dozen times below."""
    return is_safe_segment(s)


def _run_branch_slug(root: str) -> str | None:
    """The slug from ``<root>/.conductor/run_branch`` (``conductor/run-<slug>``), else None.
    Present at RUN time (start writes it during topology setup) and equal, by construction,
    to ``spec_slug(<spec>)``. A malformed/edited suffix that is not a safe path component is
    rejected (None) rather than joined into a gate path."""
    prefix = "conductor/run-"
    try:
        with open(
            os.path.join(root, ".conductor", "run_branch"), encoding="utf-8"
        ) as f:
            name = f.read().strip()
    except OSError:
        return None
    if not (name.startswith(prefix) and len(name) > len(prefix)):
        return None
    suffix = name[len(prefix) :]
    return suffix if _safe_slug(suffix) else None


# --- goal.md -> the spec it declares (THE shared resolver) -----------------------------
#
# ``_goal_slug`` (the gate slug) and ``freeze._assertions_source`` (the frozen done-definition)
# each kept their own copy of this regex, and each took the LEFTMOST match. Because both took
# the leftmost they AGREED, so a spec merely mentioned in passing above the intended one
# repointed the gate slug and the frozen assertions source together — two independent
# declarations that could not disagree, and so could not catch it. One resolver serves both.

_SPEC_PATH_RE = re.compile(r"docs/specs/[^\s`'\"]+?\.md")
# an explicit declaration line, e.g. `spec: docs/specs/foo.md`
#
# The field's value is deliberately NOT constrained to the `docs/specs/*.md` shape. The prose
# fallback above hardcodes that root, so this field is the ONLY way a project that keeps specs
# under `spec/` or `docs/requirements/` can name one at all. Narrowing it to match the fallback
# would close the sole escape hatch — do not "fix" it back.
_SPEC_FIELD_RE = re.compile(
    r"^[ \t]*spec:[ \t]*(\S+)[ \t]*$", re.IGNORECASE | re.MULTILINE
)


class AmbiguousSpecReference(ValueError):
    """``goal.md`` prose names more than one spec and no ``spec:`` line picks one. The goal is
    freeform (``bin/conductor goal set`` writes whatever was typed), so a second path is as
    likely to be background as the subject — guessing binds the run's gate and its frozen
    done-definition to a spec nobody chose. Fail closed and name the candidates instead."""


def spec_from_goal_text(text: str) -> str | None:
    """THE spec a goal declares, or None when it names none.

    An explicit ``spec: <path>`` line wins outright — that is how a goal whose prose mentions
    several specs states which one is the subject. Otherwise fall back to the historical
    ``docs/specs/<name>.md`` prose scan, which stays exact for the one-spec goals already in
    the wild. Two or more DISTINCT paths with no single declaration raise
    ``AmbiguousSpecReference``; the same path repeated is not ambiguous.

    EVERY ``spec:`` field is collected, not just the first. Taking the leftmost here would
    re-create, inside the explicit field, the exact leftmost-wins defect this resolver exists
    to eliminate — two ``spec:`` lines are two declarations and the goal states no single
    subject, so they fail closed on the same rule as two prose paths."""
    fields: list[str] = []
    for hit in _SPEC_FIELD_RE.finditer(text):
        value = hit.group(1).strip("`'\"<>")
        if value and value not in fields:
            fields.append(value)
    if len(fields) > 1:
        raise AmbiguousSpecReference(
            "ambiguous-spec-reference: .conductor/goal.md declares "
            f"{len(fields)} different `spec:` lines ({', '.join(fields)}); "
            "leave exactly one so the run's gate and frozen done-definition bind to the "
            "spec you chose"
        )
    if fields:
        return fields[0]
    found: list[str] = []
    for hit in _SPEC_PATH_RE.finditer(text):
        if hit.group(0) not in found:
            found.append(hit.group(0))
    if len(found) > 1:
        raise AmbiguousSpecReference(
            "ambiguous-spec-reference: the goal names "
            f"{len(found)} specs ({', '.join(found)}) and no `spec:` line says which one "
            "this run is for; add a `spec: <path>` line to .conductor/goal.md"
        )
    return found[0] if found else None


def spec_from_goal(root: str) -> str | None:
    """``spec_from_goal_text`` applied to ``<root>/.conductor/goal.md``; None when there is no
    goal file. Raises ``AmbiguousSpecReference`` exactly as the text form does."""
    try:
        with open(os.path.join(root, ".conductor", "goal.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    return spec_from_goal_text(text)


def _goal_slug(root: str) -> str | None:
    """The slug of the spec named in ``<root>/.conductor/goal.md``, else None. Fallback source
    when ``run_branch`` is absent. An AMBIGUOUS goal yields None rather than the leftmost
    candidate — ``resolve_gate`` turns that same ambiguity into a ``fail_closed`` verdict
    naming the candidates, so nothing silently resolves to a spec nobody chose."""
    try:
        spec = spec_from_goal(root)
    except AmbiguousSpecReference:
        return None
    return spec_slug(spec) if spec else None


def _ambient_slug(root: str) -> tuple[str | None, str]:
    """The slug from AMBIENT run metadata + how it was found: ``.conductor/run_branch`` first
    (present at run time), then the spec named in ``.conductor/goal.md``. ``(None, "none")``
    when neither resolves. Distinct from the explicit ``$CONDUCTOR_GATE_SLUG`` override."""
    s = _run_branch_slug(root)
    if s:
        return s, "run_branch"
    s = _goal_slug(root)
    if s:
        return s, "goal"
    return None, "none"


def has_namespaced_frozen_gate(repo_root: str | None = None) -> bool:
    """True if any ``assertions/<slug>/.frozen`` exists — the repo has FROZEN per-spec gates.
    The flat baseline ``assertions/.frozen`` is NOT namespaced (no subdir) and never counts
    here (``resolve_gate`` checks the flat baseline separately)."""
    root = repo_root or project_root()
    return bool(glob.glob(os.path.join(root, "assertions", "*", ".frozen")))


def run_gate_dir(repo_root: str, run_key: str) -> str:
    """The done-gate directory a run key names: ``<repo>/assertions/<run-key>``.

    The run key is the SINGLE source shared by the run's integration-branch suffix
    (``conductor/run-<run-key>``) and this directory, so the two cannot diverge. ``resolve_gate``
    verifies that equality against ``run.json`` rather than re-deriving it from ambient files.

    Refuses an unsafe key instead of composing a traversal path out of it, the same way
    ``conductor.core.runstate`` refuses one before building a state path."""
    if not _safe_slug(run_key):
        raise ValueError(
            f"unsafe run key {run_key!r}; refusing to build a gate path from it"
        )
    return os.path.join(repo_root, "assertions", run_key)


class GateResolution(NamedTuple):
    """The fully-resolved done-gate location + integrity verdict for one run (see
    ``resolve_gate``). ``fail_closed`` is None when the run may proceed, else the reason
    ``assert run`` / ``gate verify`` must refuse."""

    directory: str  # the gate dir (manifest + baseline + results live here)
    manifest: str  # manifest.yaml path
    baseline: str  # .frozen baseline path
    run_dir: str  # results.json dir
    slug: str | None  # the resolved slug (None = flat gate)
    source: str  # how selected: run_key|gate_dir_env|explicit_slug|run_branch|goal|flat
    fail_closed: str | None  # None = ok; else the §5 refuse reason


def _resolve_gate_by_run_key(
    root: str, run_key: str, run: dict | None
) -> GateResolution:
    """Gate resolution when the invocation carries a run key. See ``resolve_gate``."""
    if run is None:
        raise ValueError(
            "resolve_gate(run_key=...) needs the run document; load it with "
            "conductor.core.runstate.load(state_root, run_key) or call "
            "conductor.core.resolve.gate_for_run(resolution)"
        )
    recorded_key = run.get("run_key")
    recorded_dir = str(run.get("gate_dir") or "")
    recorded_branch = str(run.get("integration_branch") or "")
    scheme = run.get("identity_scheme")
    prefix = "assertions/"
    segment = recorded_dir[len(prefix) :] if recorded_dir.startswith(prefix) else ""
    fail: str | None = None
    if recorded_key != run_key:
        # The record must BE this run's. A legacy-slug-v1 record is exempt from the derived-name
        # cross-check below and so its recorded names are trusted verbatim; without this check a
        # mis-paired load would hand this key ANOTHER run's gate directory — exactly the
        # "validate some other run's already-green gate" outcome run-key mode exists to prevent.
        fail = (
            f"run.json declares run_key {recorded_key!r} but the gate was resolved for "
            f"{run_key!r}; the wrong run's record was loaded"
        )
    elif not segment or not _safe_slug(segment):
        fail = (
            f"run {run_key!r} records gate_dir={recorded_dir!r}, which is not "
            "'assertions/<single-safe-segment>' — repair run.json"
        )
    elif scheme == "path-hash-v2":
        # names.derived_names is THE definition of both formats — never re-write the literals
        # here. conductor/branches.py:1-15 records what happened the last time two callers each
        # derived `conductor/run-<...>` independently: they drifted.
        want_dir, want_branch = derived_names(run_key)
        if recorded_dir != want_dir:
            fail = (
                f"run {run_key!r} records gate_dir={recorded_dir!r}, expected {want_dir!r}; the "
                "run key is the single source of the gate dir and the integration branch — "
                "repair run.json"
            )
        elif recorded_branch != want_branch:
            fail = (
                f"run {run_key!r} records integration_branch={recorded_branch!r}, expected "
                f"{want_branch!r}; the run key is the single source of both — repair run.json"
            )
    elif scheme != "legacy-slug-v1":
        fail = (
            f"run {run_key!r} records unknown identity_scheme {scheme!r}; expected "
            "'path-hash-v2' or 'legacy-slug-v1' — repair run.json"
        )
    if fail is None and segment and _safe_slug(segment):
        directory = os.path.join(root, "assertions", segment)
    else:
        # ANY identity failure lands here, not just a missing segment. A well-formed segment is
        # not evidence the record is the right one: the mismatch branch above fires precisely
        # when run.json belongs to a DIFFERENT run, and that run's gate_dir is a perfectly safe
        # slug naming its real, frozen, green gate. Trusting the segment there would hand a
        # caller that ignored fail_closed someone else's passing gate — the same class of bug as
        # dodging onto the flat gate, one directory over.
        #
        # The failure must also not point at the flat gate: in a repo that has one, `assertions/`
        # is itself a real, frozen, green gate. Note this is the OPPOSITE of legacy mode, which
        # never redirects on failure — it keeps whatever it already resolved, and reaches flat
        # only when fail_closed is None. `__unresolved__` cannot collide with any run key (keys
        # must start with [a-z0-9]), so manifest/baseline/run_dir all land on a path that does
        # not exist, and a caller that ignores fail_closed still fails closed.
        directory = os.path.join(root, "assertions", "__unresolved__")
    return GateResolution(
        directory,
        os.path.join(directory, "manifest.yaml"),
        os.path.join(directory, ".frozen"),
        os.path.join(directory, "run"),
        run_key,
        "run_key",
        fail,
    )


def resolve_gate(
    repo_root: str | None = None,
    *,
    run_key: str | None = None,
    run: dict | None = None,
) -> GateResolution:
    """THE gate-resolution policy — the single decision function for WHERE this run's done-gate
    lives and WHETHER it is dodging a frozen gate. ``gate_dir`` / ``manifest_path`` /
    ``baseline_path`` / ``run_dir`` / ``unresolved_frozen_gate`` all delegate here, and the
    runner + ``gate freeze|verify`` call it directly, so the policy cannot drift across callers.

    RUN-KEY MODE (``run_key`` given, ``source == "run_key"``). The key alone determines the gate:
    legacy ``.conductor/run_branch``, legacy ``.conductor/goal.md``, and the ambient
    ``CONDUCTOR_GATE_DIR`` / ``CONDUCTOR_GATE_SLUG`` / ``CONDUCTOR_MANIFEST`` /
    ``CONDUCTOR_FREEZE_BASELINE`` variables are IGNORED rather than consulted as fallback
    (design §"Project and run identity"). ``run`` is the loaded ``run.json``; the resolver
    verifies from it that the record is this run's and that the recorded ``gate_dir`` and
    ``integration_branch`` agree with the key, so a hand-edited or half-migrated record fails
    closed instead of validating some other run's already-green gate. A ``legacy-slug-v1`` run
    keeps the names migration recorded. ``resolve_gate`` never loads the document itself — that
    would make ``paths`` import ``conductor.core.runstate``, whose ``runkey`` already imports
    ``paths.spec_slug``; ``conductor.core.resolve.gate_for_run`` is the one place that pairs a
    loaded record with this resolver. On refusal the gate NEVER collapses onto the flat
    ``assertions/`` — it keeps the segment the record claimed while that is safe, else
    ``assertions/__unresolved__`` — because in a repo that has one the flat gate is real, frozen
    and green, and a caller reading ``directory`` without checking ``fail_closed`` would then
    validate (or write results into) exactly the wrong gate.

    LEGACY MODE (no ``run_key``) is everything below and is unchanged. Plan 03 retires it once
    every run is migrated.

    DIRECTORY precedence (``source``):
      1. ``$CONDUCTOR_GATE_DIR``  -> that dir                              (``gate_dir_env``)
      2. ``$CONDUCTOR_GATE_SLUG`` -> ``assertions/<slug>``, FORCED         (``explicit_slug``)
           A deliberate "this run is namespaced" signal (start exports it for the step-3
           build/lint/freeze, before run_branch/goal exist). No flat fallback: a not-yet-written
           or mis-written manifest fails closed rather than silently using the legacy flat gate.
      3. AMBIENT slug (``.conductor/run_branch`` then ``goal.md``)         (``run_branch`` / ``goal``)
           a. ``assertions/<slug>`` when it holds ``manifest.yaml`` OR ``.frozen`` (built/frozen)
           b. ``assertions/<slug>`` (unbuilt) when ANY namespaced ``.frozen`` exists — so a
              stale/corrupt slug fails closed instead of dodging to the flat gate
           c. else flat ``assertions/`` (legacy fallback; repo hasn't adopted per-spec gates)
      4. no slug -> flat ``assertions/``                                   (``flat``)

    ``manifest`` = ``$CONDUCTOR_MANIFEST`` or ``<dir>/manifest.yaml``;
    ``baseline`` = ``$CONDUCTOR_FREEZE_BASELINE`` or ``<dir>/.frozen``;
    ``run_dir``  = ``<dir-of-manifest>/run``.

    FAIL_CLOSED (§5 ambient-dodge guard) is set — and the runner + ``gate verify`` must refuse
    — on either signature of repointed ambient run metadata dodging a real frozen baseline
    (ANY explicit override — slug / gate-dir / manifest / freeze-baseline — is a deliberate
    selection and is exempt):
      (i)  the resolved gate is UNFROZEN (baseline absent) while a frozen gate exists
           ELSEWHERE — a namespaced ``assertions/<slug>/.frozen`` OR the flat ``assertions/
           .frozen`` (an edited ``run_branch`` or a planted unfrozen manifest); or
      (ii) the ``run_branch`` slug and the ``goal.md`` spec DISAGREE — ``run_branch`` was
           repointed onto a DIFFERENT (possibly already-green, frozen) gate than the one this
           run declared. ``/conductor:start`` writes the two together, so at run time they
           agree; a mismatch is repointed metadata; or
      (iii) ``goal.md`` names SEVERAL specs and no ``spec:`` line picks one
           (``AmbiguousSpecReference``) — the run declares no single spec, so clause (ii) has
           nothing to check against and the slug would be whichever path the prose mentions
           first. The verdict names the candidates so a ``spec:`` line resolves it.
    A repo with no frozen gate at all, and a run whose run_branch/goal.md agree, is never
    affected."""
    root = repo_root or project_root()
    if run_key is not None:
        return _resolve_gate_by_run_key(root, run_key, run)
    flat = os.path.join(root, "assertions")
    env_dir = os.environ.get("CONDUCTOR_GATE_DIR")
    env_slug = os.environ.get("CONDUCTOR_GATE_SLUG")
    env_manifest = os.environ.get("CONDUCTOR_MANIFEST")
    env_baseline = os.environ.get("CONDUCTOR_FREEZE_BASELINE")
    explicit = bool(env_dir or env_slug or env_manifest or env_baseline)

    if env_dir:  # (1)
        directory, slug, source = env_dir, None, "gate_dir_env"
    elif env_slug:  # (2) forced, no fallback
        directory, slug, source = (
            os.path.join(flat, env_slug),
            env_slug,
            "explicit_slug",
        )
    else:
        slug, source = _ambient_slug(root)  # (3) / (4)
        if slug:
            nsdir = os.path.join(flat, slug)
            built = os.path.isfile(
                os.path.join(nsdir, "manifest.yaml")
            ) or os.path.isfile(os.path.join(nsdir, ".frozen"))
            if built or has_namespaced_frozen_gate(root):  # (3a) / (3b)
                directory = nsdir
            else:  # (3c) legacy fallback
                directory, slug, source = flat, None, "flat"
        else:  # (4)
            directory, source = flat, "flat"

    manifest = env_manifest or os.path.join(directory, "manifest.yaml")
    baseline = env_baseline or os.path.join(directory, ".frozen")
    rundir = os.path.join(os.path.dirname(manifest), "run")

    fail_closed = None
    if not explicit:
        try:
            spec_from_goal(root)
        except AmbiguousSpecReference as exc:
            # (iii) the goal declares no single spec, so neither the gate slug nor the
            # cross-check against run_branch can be derived — refuse rather than run against
            # whichever candidate happens to come first in the prose.
            return GateResolution(
                directory, manifest, baseline, rundir, slug, source, str(exc)
            )
        if not os.path.exists(baseline):
            # (i) dodge onto an UNFROZEN gate while a frozen gate exists elsewhere.
            flat_frozen = os.path.isfile(os.path.join(flat, ".frozen"))
            if flat_frozen or has_namespaced_frozen_gate(root):
                fail_closed = (
                    "run resolves to an unfrozen gate but a frozen gate exists — check "
                    ".conductor/run_branch or CONDUCTOR_GATE_SLUG"
                )
        elif source == "run_branch":
            # (ii) dodge onto a DIFFERENT, already-FROZEN gate by repointing run_branch:
            # run_branch and goal.md are two independent declarations of the run's spec, and
            # /conductor:start writes them together. If goal.md names a DIFFERENT spec, the
            # run_branch was repointed to validate an alternate baseline (e.g. another spec's
            # green gate) instead of this run's — fail closed (§5).
            goal = _goal_slug(root)
            if goal is not None and goal != slug:
                fail_closed = (
                    f".conductor/run_branch names {slug!r} but goal.md names {goal!r} — "
                    "repointed run metadata; check .conductor/run_branch"
                )
    return GateResolution(
        directory, manifest, baseline, rundir, slug, source, fail_closed
    )


def gate_slug(repo_root: str | None = None) -> str | None:
    """The slug that names this run's gate, or None for the flat gate: ``$CONDUCTOR_GATE_SLUG``,
    else the ambient ``.conductor/run_branch`` slug, else the ``goal.md`` spec's slug."""
    env = os.environ.get("CONDUCTOR_GATE_SLUG")
    if env:
        return env
    return _ambient_slug(repo_root or project_root())[0]


def gate_dir(repo_root: str | None = None) -> str:
    """The directory holding this run's done-gate. Thin wrapper over ``resolve_gate``."""
    return resolve_gate(repo_root).directory


def manifest_path(repo_root: str | None = None) -> str:
    """The done-gate manifest path. Thin wrapper over ``resolve_gate``."""
    return resolve_gate(repo_root).manifest


def baseline_path(repo_root: str | None = None) -> str:
    """The freeze baseline path. Thin wrapper over ``resolve_gate``."""
    return resolve_gate(repo_root).baseline


def run_dir(repo_root: str | None = None) -> str:
    """Where the runner writes ``results.json``. Thin wrapper over ``resolve_gate``."""
    return resolve_gate(repo_root).run_dir


def unresolved_frozen_gate(repo_root: str | None = None) -> bool:
    """Whether this run is ambiently dodging a frozen gate (§5). Thin wrapper over
    ``resolve_gate`` — True iff ``fail_closed`` is set."""
    return resolve_gate(repo_root).fail_closed is not None
