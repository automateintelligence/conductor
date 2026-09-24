"""A-DH-6 — unresolvable default-branch metadata refuses every automated merge (property).

Source: docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.assertions.md §3.

Claim: when the repository's default branch cannot be resolved from authoritative remote
metadata, every automated merge is refused, and no code path substitutes a literal fallback
branch name.

RED ON PURPOSE, AND FOR A NAMED REASON. ``conductor/branches.py:91`` ``default_branch()``
tries ``gh repo view`` then the ``origin/HEAD`` symbolic ref and, on any failure, RETURNS THE
LITERAL ``"main"``. That is today's documented behaviour — the 2026-07-05 gate pins it as
``a10-default-branch-never-empty`` ("on resolution failure exactly 'main'"). Design line 220
inverts it, and roadmap Plan 06 owns the inversion; a10 must be re-derived there, not deleted.
Until Plan 06 lands, ``test_no_default_branch_resolver_substitutes_a_literal_fallback`` fails
naming the resolver and the literal it substituted. Do not make this pass by editing
``conductor/branches.py`` outside Plan 06.

THE RESOLVER SET IS DERIVED, NOT HAND-LISTED. Every module-level callable in the installed
``conductor`` package whose name contains ``default_branch`` is a default-branch resolver by
construction, and each one is exercised under BOTH configurations. A hand-listed pair would
stop covering the third resolver somebody adds — which is exactly how a fallback gets
reintroduced in a module nobody thought to re-check. The derivation is by NAME, which is a
heuristic: a resolver that spells its purpose some other way would escape it. The end-to-end
``conductor merge`` leg is what covers the merge decision regardless of how its resolver is
spelled, so the two legs together are not redundant.

BOTH CONFIGURATIONS ARE EXERCISED, AND THE SECOND IS THE POINT. Checking only the refusal is
satisfied by a merge path that never works at all — so the resolvable configuration runs the
SAME otherwise-eligible phase pull request through the same command and requires it to MERGE,
with its authoritative default branch deliberately named ``trunk``: neither ``main`` nor
``master``, so a resolver that happens to return a fallback cannot masquerade as correct.

NO REAL HOST, NO REAL GITHUB. A fake ``gh`` on ``PATH`` answers every query the merge path
makes and records every invocation; the repository is a temporary bare remote plus a working
clone. ``refs/pull/101/merge`` is created in the bare remote so the gate's merge-ref
re-verification runs for real against a real merge tree.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONDUCTOR_BIN = ROOT / "bin" / "conductor"

#: The repository the merge gate runs against; `CONDUCTOR_REPO` short-circuits gh discovery.
REPO = "acme/widget"

#: The authoritative default branch of the RESOLVABLE configuration. Deliberately neither
#: `main` nor `master`: the anti-stub leg has to distinguish "resolved correctly" from
#: "returned the fallback and got lucky".
AUTHORITATIVE_DEFAULT = "trunk"

#: The literals no resolver may substitute, and no state write may carry as the resolved value.
FALLBACKS = ("main", "master")

#: The phase pull request: fully eligible to merge, based on the run's integration branch.
PHASE_PR = 101

#: The spec used only to obtain a canonical run-branch name from the product's own resolver.
SPEC = "docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.md"

_GH_FAKE = r'''#!/usr/bin/env python3
"""Recording `gh` fake: answers exactly the queries the merge path makes, logs every call."""
import json
import os
import sys

with open(@@CONFIG@@, encoding="utf-8") as handle:
    CONFIG = json.load(handle)

argv = sys.argv[1:]
with open(@@LOG@@, "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")


def jq_expr(args):
    for flag in ("-q", "--jq"):
        if flag in args:
            return args[args.index(flag) + 1]
    return None


def json_fields(args):
    if "--json" not in args:
        return []
    return args[args.index("--json") + 1].split(",")


def die(message):
    sys.stderr.write(message + "\n")
    raise SystemExit(1)


if argv[:2] == ["repo", "view"]:
    fields = json_fields(argv)
    if "defaultBranchRef" in fields:
        name = CONFIG["default_branch"]
        if name is None:
            die("could not resolve the default branch for " + CONFIG["repo"])
        print(name)
        raise SystemExit(0)
    if "nameWithOwner" in fields:
        print(CONFIG["repo"])
        raise SystemExit(0)
    die("unsupported repo view: " + " ".join(argv))

if argv[:2] == ["pr", "view"]:
    pr = CONFIG["prs"].get(argv[2])
    if pr is None:
        die("no pull request " + argv[2])
    expr = jq_expr(argv)
    if expr and expr.startswith("."):
        print(pr[expr[1:]])
        raise SystemExit(0)
    print(json.dumps({field: pr[field] for field in json_fields(argv)}))
    raise SystemExit(0)

if argv[:2] == ["pr", "merge"]:
    with open(@@MERGED@@, encoding="utf-8") as handle:
        merged = json.load(handle)
    merged.append(argv[2])
    with open(@@MERGED@@, "w", encoding="utf-8") as handle:
        json.dump(merged, handle)
    print("merged " + argv[2])
    raise SystemExit(0)

if argv[:2] == ["api", "graphql"]:
    query = " ".join(argv)
    if "reviewThreads" in query:
        raise SystemExit(0)  # no threads at all -> nothing unresolved
    if "commits(last:1)" in query:
        print(json.dumps(CONFIG["newest_commit"]))
        raise SystemExit(0)
    die("unsupported graphql query")

die("unsupported gh invocation: " + " ".join(argv))
'''

#: Invokes ONE enumerated resolver in a fresh interpreter under the fixture's environment and
#: reports what it did. A resolver whose required parameters this harness cannot supply reports
#: `unsupplied` rather than being quietly dropped from the sweep.
_CALL_RESOLVER = r"""#!/usr/bin/env python3
import importlib
import inspect
import json
import sys

module_name, function_name, supplied_json = sys.argv[1], sys.argv[2], sys.argv[3]
supplied = json.loads(supplied_json)
function = getattr(importlib.import_module(module_name), function_name)

kwargs, missing = {}, []
for name, parameter in inspect.signature(function).parameters.items():
    if parameter.default is not inspect.Parameter.empty:
        continue
    if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
        continue
    if name in supplied:
        kwargs[name] = supplied[name]
    else:
        missing.append(name)

if missing:
    print(json.dumps({"outcome": "unsupplied", "missing": missing}))
    raise SystemExit(0)

try:
    value = function(**kwargs)
except BaseException as exc:  # a refusal is a legitimate outcome, not a harness error
    print(json.dumps({"outcome": "raised", "error": f"{type(exc).__name__}: {exc}"}))
else:
    print(json.dumps({"outcome": "returned", "value": value}))
"""


def _default_branch_resolvers() -> list[tuple[str, str]]:
    """Every module-level callable in the package whose name names a default-branch resolution.

    Derived from the package source, so a resolver added to a new module joins the sweep
    without editing this assertion."""
    package = ROOT / "conductor"
    resolvers: list[tuple[str, str]] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for node in tree.body:
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and "default_branch" in node.name
            ):
                resolvers.append((module, node.name))
    return resolvers


def _git(cwd: pathlib.Path, *args: str, env: dict[str, str] | None = None) -> str:
    out = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if out.returncode != 0:
        raise AssertionError(
            f"fixture git failed: git {' '.join(args)} (rc={out.returncode})\n{out.stderr}"
        )
    return out.stdout


class Configuration:
    """One temporary repository plus its fake `gh`, in one default-branch configuration."""

    def __init__(self, name: str, resolvable: bool) -> None:
        self.name = name
        self.resolvable = resolvable
        self.workdir = pathlib.Path()
        self.work = pathlib.Path()
        self.bare = pathlib.Path()
        self.gh_log = pathlib.Path()
        self.merged_file = pathlib.Path()
        self.env: dict[str, str] = {}
        self.run_branch = ""

    @property
    def gh_calls(self) -> list[dict]:
        if not self.gh_log.is_file():
            return []
        return [
            json.loads(line)
            for line in self.gh_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @property
    def merged_prs(self) -> list[str]:
        return json.loads(self.merged_file.read_text(encoding="utf-8"))

    def refs(self) -> str:
        """Every ref in both repositories — the evidence for 'no git merge commit'."""
        return _git(self.work, "for-each-ref") + _git(self.bare, "for-each-ref")

    def state_files(self) -> dict[str, str]:
        """Digest of every file under the project's state root, for the state-write clause."""
        root = self.work / ".conductor"
        digests: dict[str, str] = {}
        for path in sorted(root.rglob("*")) if root.is_dir() else []:
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                digests[str(path.relative_to(root))] = digest
        return digests

    def merge(self, pr: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CONDUCTOR_BIN), "merge", str(pr)],
            cwd=str(self.work),
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def call_resolver(self, module: str, function: str) -> dict:
        harness = self.workdir / "call_resolver.py"
        supplied = json.dumps({"repo": REPO, "pr": PHASE_PR})
        out = subprocess.run(
            [sys.executable, str(harness), module, function, supplied],
            cwd=str(self.work),
            env={**self.env, "PYTHONPATH": str(ROOT)},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if out.returncode != 0:
            raise AssertionError(
                f"the resolver harness could not run {module}.{function} "
                f"(rc={out.returncode}):\n{out.stdout}\n{out.stderr}"
            )
        return json.loads(out.stdout.strip().splitlines()[-1])


def _build(name: str, resolvable: bool) -> Configuration:
    config = Configuration(name, resolvable)
    config.workdir = pathlib.Path(tempfile.mkdtemp(prefix=f"a-dh-6-{name}-")).resolve()
    config.work = config.workdir / "work"
    config.bare = config.workdir / "remote.git"
    home = config.workdir / "home"
    bindir = home / ".local" / "bin"
    for directory in (config.work, home, bindir):
        directory.mkdir(parents=True)

    base_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CONDUCTOR_", "GH_", "GITHUB_"))
    }
    git_env = {
        **base_env,
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": str(home / "gitconfig"),
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }

    _git(
        config.workdir,
        "init",
        "--bare",
        "-b",
        AUTHORITATIVE_DEFAULT,
        str(config.bare),
        env=git_env,
    )
    _git(config.work, "init", "-b", AUTHORITATIVE_DEFAULT, env=git_env)
    (config.work / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(config.work, "add", "README.md", env=git_env)
    _git(config.work, "commit", "-m", "seed", env=git_env)
    _git(config.work, "remote", "add", "origin", str(config.bare), env=git_env)
    _git(config.work, "push", "-u", "origin", AUTHORITATIVE_DEFAULT, env=git_env)

    run_branch = subprocess.run(
        [str(CONDUCTOR_BIN), "run-branch", "name", SPEC],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert run_branch.returncode == 0, (
        f"the product's own run-branch resolver failed: {run_branch.stderr}"
    )
    config.run_branch = run_branch.stdout.strip()
    assert config.run_branch, "the product emitted an empty run-branch name"

    _git(config.work, "checkout", "-b", config.run_branch, env=git_env)
    _git(config.work, "push", "-u", "origin", config.run_branch, env=git_env)
    _git(config.work, "checkout", "-b", "phase-1", env=git_env)
    (config.work / "phase.md").write_text("phase work\n", encoding="utf-8")
    _git(config.work, "add", "phase.md", env=git_env)
    _git(config.work, "commit", "-m", "phase 1", env=git_env)
    _git(config.work, "push", "-u", "origin", "phase-1", env=git_env)
    phase_head = _git(config.work, "rev-parse", "HEAD", env=git_env).strip()

    # The real merge tree GitHub would publish at refs/pull/<n>/merge, so the gate's merge-ref
    # re-verification fetches and checks out something that actually exists. Built on a scratch
    # branch that is deleted again: no ref in either repository is left pointing at a merge
    # commit, which is what the must-not-contain clause reads.
    _git(config.work, "checkout", "-b", "scratch-merge", config.run_branch, env=git_env)
    _git(
        config.work, "merge", "--no-ff", "-m", f"PR #{PHASE_PR}", "phase-1", env=git_env
    )
    _git(config.work, "push", "origin", f"HEAD:refs/pull/{PHASE_PR}/merge", env=git_env)
    _git(config.work, "checkout", config.run_branch, env=git_env)
    _git(config.work, "branch", "-D", "scratch-merge", env=git_env)

    if resolvable:
        # Authoritative remote metadata present: origin/HEAD names the real default branch.
        _git(
            config.bare,
            "symbolic-ref",
            "HEAD",
            f"refs/heads/{AUTHORITATIVE_DEFAULT}",
            env=git_env,
        )
        _git(config.work, "remote", "set-head", "origin", "-a", env=git_env)
    else:
        # Unresolvable: no origin/HEAD locally and the remote's own HEAD names nothing that
        # exists, so neither probe can answer.
        _git(
            config.bare,
            "symbolic-ref",
            "HEAD",
            "refs/heads/no-such-branch",
            env=git_env,
        )
        head_ref = config.work / ".git" / "refs" / "remotes" / "origin" / "HEAD"
        assert not head_ref.exists(), f"the unresolvable fixture still has {head_ref}"

    # The run topology the merge gate's base leg reads.
    (config.work / ".conductor").mkdir()
    (config.work / ".conductor" / "run_branch").write_text(
        config.run_branch + "\n", encoding="utf-8"
    )

    gh_config = config.workdir / "gh-config.json"
    config.gh_log = config.workdir / "gh-calls.jsonl"
    config.merged_file = config.workdir / "merged.json"
    config.gh_log.write_text("", encoding="utf-8")
    config.merged_file.write_text("[]", encoding="utf-8")
    gh_config.write_text(
        json.dumps(
            {
                "repo": REPO,
                "default_branch": AUTHORITATIVE_DEFAULT if resolvable else None,
                "newest_commit": {
                    "committedDate": "2026-01-01T00:00:00Z",
                    "pushedDate": None,
                },
                "prs": {
                    str(PHASE_PR): {
                        "baseRefName": config.run_branch,
                        "headRefOid": phase_head,
                        "mergeStateStatus": "CLEAN",
                        "mergeable": "MERGEABLE",
                        "reviewDecision": "APPROVED",
                        "isDraft": False,
                        "body": f"Closes #5\n\nphase {PHASE_PR}",
                        "comments": [
                            {
                                "body": "Codex review: no P1 findings",
                                "createdAt": "2026-01-02T00:00:00Z",
                                "author": {"login": "reviewer-one"},
                            },
                            {
                                "body": "Codex review round two: clean",
                                "createdAt": "2026-01-02T01:00:00Z",
                                "author": {"login": "reviewer-two"},
                            },
                        ],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    fake = bindir / "gh"
    fake.write_text(
        _GH_FAKE.replace("@@CONFIG@@", repr(str(gh_config)))
        .replace("@@LOG@@", repr(str(config.gh_log)))
        .replace("@@MERGED@@", repr(str(config.merged_file))),
        encoding="utf-8",
    )
    fake.chmod(0o755)
    (config.workdir / "call_resolver.py").write_text(_CALL_RESOLVER, encoding="utf-8")

    config.env = {
        **git_env,
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "CONDUCTOR_HOME": str(config.work),
        "CONDUCTOR_REPO": REPO,
        "CONDUCTOR_MERGE_VERIFY": "exit 0",
        "CONDUCTOR_REVIEW_MARKER": "Codex review",
        "CONDUCTOR_MIN_REVIEWS": "2",
        # The gate's merge-ref re-verification makes its own scratch worktree under $TMPDIR.
        # Pointing that at the fixture keeps any leak inside the tree this module deletes.
        "TMPDIR": str(config.workdir),
    }
    return config


_CONFIGURATIONS: dict[str, Configuration] = {}


def configuration(name: str) -> Configuration:
    if name not in _CONFIGURATIONS:
        _CONFIGURATIONS[name] = _build(name, resolvable=(name == "resolvable"))
    return _CONFIGURATIONS[name]


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    for config in _CONFIGURATIONS.values():
        shutil.rmtree(config.workdir, ignore_errors=True)


def _merge_calls(config: Configuration, pr: int) -> list[dict]:
    return [
        call
        for call in config.gh_calls
        if call["argv"][:2] == ["pr", "merge"] and str(pr) in call["argv"]
    ]


def test_the_unresolvable_configuration_refuses_the_automated_merge() -> None:
    """Must-contain: non-zero exit, a refusal naming unresolved default-branch metadata, and
    the pull request still open and unmerged."""
    config = configuration("unresolvable")
    result = config.merge(PHASE_PR)
    report = f"{result.stdout}\n{result.stderr}"
    assert result.returncode != 0, (
        f"conductor merge exited 0 with unresolvable default-branch metadata:\n{report}"
    )
    lowered = report.lower()
    assert "default" in lowered and "branch" in lowered, (
        "the refusal does not name default-branch metadata as the cause; an operator cannot "
        f"tell what to fix:\n{report}"
    )
    assert not config.merged_prs, (
        f"the pull request was merged despite the refusal: {config.merged_prs}"
    )


def test_the_unresolvable_configuration_records_no_merge_and_no_merge_commit() -> None:
    """Must-not-contain: any recorded `gh` merge call, or any git merge commit."""
    config = configuration("unresolvable")
    before = config.refs()
    config.merge(PHASE_PR)
    assert not _merge_calls(config, PHASE_PR), (
        f"a gh merge call was recorded in the unresolvable configuration: "
        f"{_merge_calls(config, PHASE_PR)}"
    )
    assert config.refs() == before, (
        "the refused merge changed a ref — a merge commit landed while the default branch "
        "was unresolvable"
    )


def test_the_unresolvable_configuration_writes_no_literal_fallback_as_the_resolved_value() -> (
    None
):
    """Must-not-contain: `main` or `master` as the RESOLVED default-branch value in the merge
    decision or in any state write.

    Scoped to the resolved value specifically, not to log text in general: the run and phase
    branch names legitimately contain those words, and a broader match would go red for a
    reason this assertion does not govern."""
    config = configuration("unresolvable")
    before = config.state_files()
    result = config.merge(PHASE_PR)
    after = config.state_files()

    changed = {
        name for name in set(before) | set(after) if before.get(name) != after.get(name)
    }
    offenders = []
    for name in sorted(changed):
        text = (config.work / ".conductor" / name).read_text(
            encoding="utf-8", errors="replace"
        )
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "default" in line.lower() and any(f in line for f in FALLBACKS):
                offenders.append(f"{name}:{lineno}: {line.strip()[:120]}")
    assert not offenders, (
        f"a literal fallback was written as the resolved default branch: {offenders}"
    )

    decision = f"{result.stdout}\n{result.stderr}"
    reported = []
    for line in decision.splitlines():
        if "default" not in line.lower():
            continue
        tokens = line.replace(":", " ").replace(",", " ").split()
        if any(token in FALLBACKS for token in tokens):
            reported.append(line.strip()[:120])
    assert not reported, (
        f"the merge decision reports a literal fallback as the resolved default: {reported}"
    )


def test_no_default_branch_resolver_substitutes_a_literal_fallback() -> None:
    """Must-not-contain, swept over every derived resolver: under the unresolvable
    configuration no resolver may RETURN a value — it must refuse.

    This is the leg roadmap Plan 06 owns. `conductor/branches.py:91` returns the literal
    `"main"` here today."""
    config = configuration("unresolvable")
    resolvers = _default_branch_resolvers()
    assert resolvers, (
        "no default-branch resolver found in the conductor package — the sweep would be vacuous"
    )
    offenders = []
    for module, function in resolvers:
        outcome = config.call_resolver(module, function)
        if outcome["outcome"] == "unsupplied":
            offenders.append(
                f"{module}.{function}: the sweep cannot supply {outcome['missing']}; "
                "this resolver went unchecked"
            )
        elif outcome["outcome"] == "returned":
            offenders.append(
                f"{module}.{function}: returned {outcome['value']!r} instead of refusing "
                "with unresolvable default-branch metadata"
            )
    assert not offenders, "\n".join(
        ["a default-branch resolver did not fail closed:", *offenders]
    )


def test_the_resolvable_configuration_merges_the_eligible_phase_pull_request() -> None:
    """Must-contain (anti-stub): the same otherwise-eligible merge succeeds when the
    authoritative default branch resolves — proving the refusal is caused by the unresolved
    metadata and not by a merge path that is simply broken or unreachable."""
    config = configuration("resolvable")
    result = config.merge(PHASE_PR)
    report = f"{result.stdout}\n{result.stderr}"
    assert result.returncode == 0, (
        "the eligible phase pull request did not merge against a resolvable default branch, so "
        f"the refusal leg proves nothing (rc={result.returncode}):\n{report}"
    )
    assert config.merged_prs == [str(PHASE_PR)], (
        f"expected exactly PR {PHASE_PR} merged, recorded {config.merged_prs}"
    )


def test_the_resolvable_configuration_resolves_the_authoritative_name() -> None:
    """Must-contain (anti-stub) for the resolver sweep: every derived resolver returns the
    repository's real default branch, which is deliberately neither `main` nor `master`. A
    resolver that always refuses fails here."""
    config = configuration("resolvable")
    wrong = []
    for module, function in _default_branch_resolvers():
        outcome = config.call_resolver(module, function)
        if outcome.get("value") != AUTHORITATIVE_DEFAULT:
            wrong.append(f"{module}.{function}: {outcome}")
    assert not wrong, "\n".join(
        [
            f"a resolver failed to resolve the authoritative default branch "
            f"{AUTHORITATIVE_DEFAULT!r}:",
            *wrong,
        ]
    )
