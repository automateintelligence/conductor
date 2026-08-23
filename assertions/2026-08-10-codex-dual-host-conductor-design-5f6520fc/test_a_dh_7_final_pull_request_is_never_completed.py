"""A-DH-7 — Conductor never completes the final default-branch pull request (property).

Source: docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.assertions.md §3.

Claim: no Conductor code path performs, requests, or enables any action that could cause the
final integration-to-default pull request to complete.

THE VERB LIST IS PARSED FROM `bin/conductor`, NOT HAND-LISTED. The CLI is a bash `case`
statement, so its top-level arm labels ARE its registered verb list; `_registered_verbs` reads
them (tracking `case`/`esac` nesting so the `goal` sub-case's arms are not mistaken for verbs).
A verb added to the dispatcher joins this sweep with no edit here.

WHAT IS HAND-LISTED, EXPLICITLY: the ARGUMENT VECTOR each verb is invoked with. Nothing in a
bash `case` statement declares a verb's arguments, so they cannot be derived. `_INVOCATIONS`
carries one or more argv per verb, and `test_every_registered_verb_is_swept` FAILS when the
parsed verb list contains a verb with no entry — a new verb stops the sweep loudly instead of
being silently skipped. The verbs themselves are enumerated; their arguments are not.

"SWEPT" MEANS THE VERB RAN, NOT THAT AN ATTEMPT WAS RECORDED. Because the argument table is
hand-written it can be WRONG, and a wrong vector is the quietest way to hollow this assertion
out: `conductor ledger` with no subcommand, `conductor authority preview` with no plan and
`conductor plan-lint` with no plan all exited in argparse before reaching a line of the verb's
own code, and every prohibition below was satisfied by a verb that did nothing at all. A
prohibition that never executes the prohibited path is not a prohibition. So every invocation
is checked for having reached its own logic — `test_no_swept_invocation_died_in_argument_parsing`
fails on any argument-parsing death — and a table entry that stops parsing now fails LOUDLY
instead of counting as a sweep.

THE FLAG SURFACE IS SWEPT TOO, AND IT IS DERIVED. A verb's prohibited path can sit behind an
OPTION rather than behind the verb, and a hand-written base vector never reaches it. So after
every base vector has run, each invocation is asked for its own usage line (`--help`) and every
VALUELESS long option it declares is swept on top of the base vector — the option surface comes
from the verb, exactly as the verb list comes from the dispatcher. Two passes, in that order,
because a flag may legitimately change the run's state (`resume --reactivate` does) and the
must-contain clauses below are read off the base pass.

KNOWN RESIDUAL OF THE FLAG SWEEP (`_FLAG_OPAQUE`, and options that take a value). A verb that
hand-rolls its arguments publishes no machine-readable option surface, and an option whose value
cannot be synthesised is swept only as far as the base vector carries it. Both are recorded
rather than assumed: `test_the_flag_surface_probe_answered_for_every_verb_that_can_answer` pins
the opaque set, so a verb that stops answering joins it loudly.

FORCE-PUSH IS CHECKED THROUGH GIT REFS, NOT THE GH LOG. Seven of the spec's eight prohibited
actions are `gh` invocations and are read out of the recorded `gh` log. A force-push is a git
action that no `gh` log would show, so the bare remote's refs are snapshotted before and after
the sweep: the run integration branch and the default branch must be byte-identical afterwards.

KNOWN RESIDUAL (recorded in the assertion spec §4): the spec's prohibition ends with "or any
equivalent action". This checks the eight enumerated actions, because a gate can only check a
finite list. An unlisted GitHub capability that completes a pull request by another route would
pass. Not closable by assertion alone.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONDUCTOR_BIN = ROOT / "bin" / "conductor"

REPO = "acme/widget"

#: The repository default branch. Named neither `main` nor `master` so "the final pull request's
#: base" is unambiguous in the recorded argv.
DEFAULT_BRANCH = "trunk"

#: The phase pull request: eligible, based on the run integration branch. Merging it is the
#: PERMITTED action whose absence would make the prohibition vacuous.
PHASE_PR = 101

#: The final pull request: run integration branch -> default branch, open and unmerged. Nothing
#: Conductor does may complete it.
FINAL_PR = 202

#: The phase issue the phase pull request closes. `conductor ledger` reconciles an ISSUE, so
#: this is the argument that gets that verb past its own argument parser and into its logic.
PHASE_ISSUE = 5

#: The verbs the assertion spec names for the sweep. Absent ones are a finding, not a skip.
SPEC_NAMED_VERBS = ("merge", "resume", "finish", "heartbeat", "status", "gate")

#: Which roadmap plan owns each verb the CLI does not register yet.
VERB_OWNER = {
    "resume": "roadmap Plan 05 (conductor heartbeat|resume|finish)",
    "finish": "roadmap Plan 05 (conductor heartbeat|resume|finish)",
    "heartbeat": "roadmap Plan 05 (conductor heartbeat|resume|finish)",
    "status": "roadmap Plan 05 (per-run status reporting)",
}

#: A verb died in ARGUMENT PARSING rather than reaching its own logic. argparse writes its usage
#: block plus an `error:` line to stderr and exits 2; `bin/conductor`'s hand-rolled arms echo
#: `usage: ...` and exit 64. Either shape means the verb did nothing, so it must not count as
#: swept. Anchored to a LINE START so a verb that merely prints the word in prose while doing
#: real work is not mistaken for one that refused to parse.
_USAGE_BLOCK = re.compile(r"^usage:", re.MULTILINE | re.IGNORECASE)

#: One VALUELESS long option, as a verb's own usage line declares it. `[--strict]` matches;
#: `[--run RUN]` deliberately does not — a value this test would have to invent is not derived
#: from anything, and inventing one is how a sweep starts measuring its own fixture.
_VALUELESS_OPTION = re.compile(r"\[(--[A-Za-z0-9][A-Za-z0-9-]*)\]")

#: Verbs that hand-roll their argument handling and therefore publish no machine-readable option
#: surface: asked for `--help`, they print a one-line usage with no bracketed options, or run
#: anyway. Their flags are unswept and this is the honest record of it. Pinned by
#: `test_the_flag_surface_probe_answered_for_every_verb_that_can_answer` so a verb that stops
#: answering joins this list deliberately rather than silently losing its flag sweep.
_FLAG_OPAQUE = frozenset(
    {
        "default-branch",
        "gate",
        "gate-dir",
        "goal",
        "merge-gate",
        "preflight",
    }
)

_GH_FAKE = r'''#!/usr/bin/env python3
"""Recording `gh` fake: logs EVERY invocation, answers the ones the merge path makes."""
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
        print(CONFIG["default_branch"])
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


def _registered_verbs() -> list[str]:
    """The CLI's own registered verb list: the top-level `case` arm labels of `bin/conductor`.

    Nesting is tracked so the arms of the `goal` sub-case (`set`, `get`) are not counted as
    top-level verbs, and the catch-all `*)` is excluded by the label pattern."""
    depth = 0
    verbs: list[str] = []
    for raw in CONDUCTOR_BIN.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if re.match(r"^case\b.*\bin$", line):
            depth += 1
            continue
        if line.startswith("esac"):
            depth -= 1
            continue
        if depth != 1:
            continue
        match = re.match(r"^([A-Za-z0-9_|\-\s]+)\)", line)
        if not match:
            continue
        for label in match.group(1).split("|"):
            label = label.strip()
            if label and label not in verbs:
                verbs.append(label)
    return verbs


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


class Sweep:
    """One temporary project, one run at `awaiting-team-merge`, every CLI verb invoked once."""

    def __init__(self) -> None:
        self.workdir = pathlib.Path()
        self.work = pathlib.Path()
        self.bare = pathlib.Path()
        self.gh_log = pathlib.Path()
        self.merged_file = pathlib.Path()
        self.env: dict[str, str] = {}
        self.run_branch = ""
        self.run_key = ""
        self.spec_relpath = "docs/fixture-spec.md"
        self.plan_relpath = "docs/fixture-plan.md"
        self.results: dict[str, list[dict]] = {}
        self.flag_results: dict[str, list[dict]] = {}
        self.flag_opaque: list[str] = []
        self.refs_before = ""
        self.refs_after = ""
        self.missing_verbs: list[str] = []
        self.unswept_verbs: list[str] = []

    @property
    def every_result(self) -> list[tuple[str, dict]]:
        """Both passes, base first, as (verb, result) pairs."""
        return [
            (verb, result)
            for table in (self.results, self.flag_results)
            for verb, results in table.items()
            for result in results
        ]

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


def _invocations(sweep: Sweep) -> dict[str, list[list[str]]]:
    """One or more argument vectors per registered verb. HAND-WRITTEN — see the module
    docstring: a bash `case` statement declares no arguments, so these cannot be derived. The
    verbs are; a verb missing from this table fails `test_every_registered_verb_is_swept`."""
    work = str(sweep.work)
    driver = str(sweep.work / ".conductor" / "resume-autodev.sh")
    return {
        "assert": [["run", "--level", "spec"]],
        # Each of these three carries the argument its verb REQUIRES. Without it the verb dies
        # in argparse and sweeps nothing, which is what
        # `test_no_swept_invocation_died_in_argument_parsing` now refuses.
        "ledger": [["reconcile", str(PHASE_ISSUE)]],
        "goal": [["get"]],
        "authority": [["preview", sweep.plan_relpath]],
        "preflight": [[]],
        "plan-lint": [[sweep.plan_relpath]],
        # Both pull requests: the phase merge is the PERMITTED action, the final one is the
        # prohibited target. Ordering matters only in that both must be attempted.
        "merge": [[str(PHASE_PR)], [str(FINAL_PR)]],
        "merge-gate": [[str(FINAL_PR)]],
        "run-packet": [[sweep.run_branch]],
        "resume-script": [
            ["write", "--project", work, "--worktree", work, "--out", driver, "--force"]
        ],
        "driver": [["status"]],
        "remote": [[]],
        "run-branch": [["name", sweep.spec_relpath]],
        "default-branch": [[]],
        "run": [["list"], ["show", "--run", sweep.run_key]],
        "gate-dir": [[sweep.spec_relpath]],
        "gate": [["verify"], ["lint"]],
        # Read-only relocation scan. It is swept for the same reason every other verb is:
        # the prohibition is on ANY registered verb completing the final pull request, and
        # a verb nobody sweeps is a hole whatever its intent. `--checkout` is the sweep's
        # own work tree, so the scan never reads the real one.
        "doctor": [["relocation", "--checkout", work]],
        # The four verbs the assertion spec names by name. Their argument shape is the design
        # spec's own (`conductor finish --run <run-key>`, `conductor resume --run <run-key>`).
        # Their OPTIONS are not listed here: pass two derives those from each verb's own usage
        # line, which is what reaches a prohibited action parked behind a flag.
        "resume": [["--run", sweep.run_key]],
        "finish": [["--run", sweep.run_key]],
        "heartbeat": [["--run", sweep.run_key]],
        "status": [["--run", sweep.run_key]],
    }


def _invoke(sweep: Sweep, verb: str, argv: list[str]) -> dict:
    """One CLI invocation against the fixture run, recorded."""
    proc = subprocess.run(
        [str(CONDUCTOR_BIN), verb, *argv],
        cwd=str(sweep.work),
        env=sweep.env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    return {
        "argv": argv,
        "rc": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _flag_surface(sweep: Sweep, verb: str, argv: list[str]) -> tuple[list[str], bool]:
    """``(valueless long options this invocation declares, did it declare anything)``.

    DERIVED, like the verb list: the invocation is asked for its own usage line, so an option
    added to a verb joins the sweep with no edit here. `--help` is appended to the FULL base
    vector rather than to the bare verb, because a verb with subcommands answers for whichever
    subcommand the vector names — `conductor run show --help` and `conductor run list --help`
    have different option surfaces and only the vector says which one is being swept.
    """
    probe = _invoke(sweep, verb, [*argv, "--help"])
    text = f"{probe['stdout']}\n{probe['stderr']}"
    if not _USAGE_BLOCK.search(text):
        return [], False
    # Whitespace-normalized first: argparse WRAPS its usage line, and an option split across two
    # lines would otherwise be silently dropped from the sweep. Normalizing cannot turn a
    # value-taking option into a valueless one — `[--run RUN]` still carries its metavar.
    flat = " ".join(text.split())
    return sorted(set(_VALUELESS_OPTION.findall(flat)) - {"--help"}), True


def _build() -> Sweep:
    sweep = Sweep()
    sweep.workdir = pathlib.Path(tempfile.mkdtemp(prefix="a-dh-7-")).resolve()
    sweep.work = sweep.workdir / "work"
    sweep.bare = sweep.workdir / "remote.git"
    home = sweep.workdir / "home"
    bindir = home / ".local" / "bin"
    for directory in (sweep.work, home, bindir):
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
        sweep.workdir,
        "init",
        "--bare",
        "-b",
        DEFAULT_BRANCH,
        str(sweep.bare),
        env=git_env,
    )
    _git(sweep.work, "init", "-b", DEFAULT_BRANCH, env=git_env)
    spec = sweep.work / sweep.spec_relpath
    spec.parent.mkdir(parents=True)
    spec.write_text("# fixture spec\n", encoding="utf-8")
    # The plan `authority preview` and `plan-lint` each REQUIRE as their positional argument.
    # Without it both verbs stop in argparse and sweep nothing.
    plan = sweep.work / sweep.plan_relpath
    plan.write_text(
        "# fixture plan\n\n## Phase 1 — seed\n\n- [ ] do the thing\n", encoding="utf-8"
    )
    _git(sweep.work, "add", sweep.spec_relpath, sweep.plan_relpath, env=git_env)
    _git(sweep.work, "commit", "-m", "seed", env=git_env)
    _git(sweep.work, "remote", "add", "origin", str(sweep.bare), env=git_env)
    _git(sweep.work, "push", "-u", "origin", DEFAULT_BRANCH, env=git_env)
    _git(
        sweep.bare, "symbolic-ref", "HEAD", f"refs/heads/{DEFAULT_BRANCH}", env=git_env
    )
    _git(sweep.work, "remote", "set-head", "origin", "-a", env=git_env)

    run_branch = subprocess.run(
        [str(CONDUCTOR_BIN), "run-branch", "name", sweep.spec_relpath],
        cwd=str(sweep.work),
        env={**git_env, "CONDUCTOR_HOME": str(sweep.work)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert run_branch.returncode == 0, (
        f"the product's own run-branch resolver failed: {run_branch.stderr}"
    )
    sweep.run_branch = run_branch.stdout.strip()

    _git(sweep.work, "checkout", "-b", sweep.run_branch, env=git_env)
    _git(sweep.work, "push", "-u", "origin", sweep.run_branch, env=git_env)
    _git(sweep.work, "checkout", "-b", "phase-1", env=git_env)
    (sweep.work / "phase.md").write_text("phase work\n", encoding="utf-8")
    _git(sweep.work, "add", "phase.md", env=git_env)
    _git(sweep.work, "commit", "-m", "phase 1", env=git_env)
    _git(sweep.work, "push", "-u", "origin", "phase-1", env=git_env)
    phase_head = _git(sweep.work, "rev-parse", "HEAD", env=git_env).strip()

    for number, source in ((PHASE_PR, "phase-1"), (FINAL_PR, sweep.run_branch)):
        base = sweep.run_branch if number == PHASE_PR else DEFAULT_BRANCH
        _git(sweep.work, "checkout", "-B", "scratch-merge", base, env=git_env)
        _git(sweep.work, "merge", "--no-ff", "-m", f"PR #{number}", source, env=git_env)
        _git(
            sweep.work, "push", "origin", f"HEAD:refs/pull/{number}/merge", env=git_env
        )
    _git(sweep.work, "checkout", sweep.run_branch, env=git_env)
    _git(sweep.work, "branch", "-D", "scratch-merge", env=git_env)
    run_head = _git(sweep.work, "rev-parse", "HEAD", env=git_env).strip()

    (sweep.work / ".conductor").mkdir()
    (sweep.work / ".conductor" / "run_branch").write_text(
        sweep.run_branch + "\n", encoding="utf-8"
    )

    gh_config = sweep.workdir / "gh-config.json"
    sweep.gh_log = sweep.workdir / "gh-calls.jsonl"
    sweep.merged_file = sweep.workdir / "merged.json"
    sweep.gh_log.write_text("", encoding="utf-8")
    sweep.merged_file.write_text("[]", encoding="utf-8")
    reviews = [
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
    ]
    gh_config.write_text(
        json.dumps(
            {
                "repo": REPO,
                "default_branch": DEFAULT_BRANCH,
                "newest_commit": {
                    "committedDate": "2026-01-01T00:00:00Z",
                    "pushedDate": None,
                },
                "prs": {
                    str(PHASE_PR): {
                        "baseRefName": sweep.run_branch,
                        "headRefOid": phase_head,
                        "mergeStateStatus": "CLEAN",
                        "mergeable": "MERGEABLE",
                        "reviewDecision": "APPROVED",
                        "isDraft": False,
                        "body": "Closes #5\n\nphase 1",
                        "comments": reviews,
                        "state": "OPEN",
                        "url": f"https://github.com/{REPO}/pull/{PHASE_PR}",
                    },
                    str(FINAL_PR): {
                        "baseRefName": DEFAULT_BRANCH,
                        "headRefOid": run_head,
                        "mergeStateStatus": "CLEAN",
                        "mergeable": "MERGEABLE",
                        "reviewDecision": "APPROVED",
                        "isDraft": False,
                        "body": "Closes #9\n\nfinal run packet",
                        "comments": reviews,
                        "state": "OPEN",
                        "url": f"https://github.com/{REPO}/pull/{FINAL_PR}",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    fake = bindir / "gh"
    fake.write_text(
        _GH_FAKE.replace("@@CONFIG@@", repr(str(gh_config)))
        .replace("@@LOG@@", repr(str(sweep.gh_log)))
        .replace("@@MERGED@@", repr(str(sweep.merged_file))),
        encoding="utf-8",
    )
    fake.chmod(0o755)

    sweep.env = {
        **git_env,
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "CONDUCTOR_HOME": str(sweep.work),
        "CONDUCTOR_REPO": REPO,
        "CONDUCTOR_MERGE_VERIFY": "exit 0",
        "CONDUCTOR_REVIEW_MARKER": "Codex review",
        "CONDUCTOR_MIN_REVIEWS": "2",
        "TMPDIR": str(sweep.workdir),
    }

    # The run itself, driven to awaiting-team-merge through the product's own state module.
    created = subprocess.run(
        [str(CONDUCTOR_BIN), "run", "new", sweep.spec_relpath],
        cwd=str(sweep.work),
        env=sweep.env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert created.returncode == 0, (
        f"the product could not create the fixture run (rc={created.returncode}):\n"
        f"{created.stdout}\n{created.stderr}"
    )
    listed = subprocess.run(
        [str(CONDUCTOR_BIN), "run", "list", "--all", "--json"],
        cwd=str(sweep.work),
        env=sweep.env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert listed.returncode == 0, f"conductor run list failed:\n{listed.stderr}"
    records = json.loads(listed.stdout)
    keys = (
        [record["run_key"] for record in records] if isinstance(records, list) else []
    )
    assert len(keys) == 1, f"expected exactly one fixture run, got {records}"
    sweep.run_key = keys[0]

    promote = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from conductor.core import runstate; "
            "runstate.set_status(sys.argv[1], sys.argv[2], 'awaiting-team-merge')",
            str(sweep.work / ".conductor"),
            sweep.run_key,
        ],
        cwd=str(sweep.work),
        env={**sweep.env, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert promote.returncode == 0, (
        "the fixture run could not be driven to awaiting-team-merge "
        f"(rc={promote.returncode}):\n{promote.stdout}\n{promote.stderr}"
    )

    # --- the sweep -----------------------------------------------------------------
    sweep.refs_before = _git(sweep.bare, "for-each-ref", env=git_env)
    verbs = _registered_verbs()
    table = _invocations(sweep)
    sweep.unswept_verbs = [verb for verb in verbs if verb not in table]
    sweep.missing_verbs = [verb for verb in SPEC_NAMED_VERBS if verb not in verbs]

    # Pass one: the hand-written base vector for every registered verb.
    for verb in verbs:
        for argv in table.get(verb, []):
            sweep.results.setdefault(verb, []).append(_invoke(sweep, verb, argv))

    # Pass two: every VALUELESS option each of those invocations declares about itself. After
    # pass one, because an option may legitimately change the run's state and the must-contain
    # clauses are read off the base pass.
    opaque: set[str] = set()
    for verb in verbs:
        for argv in table.get(verb, []):
            flags, answered = _flag_surface(sweep, verb, argv)
            if not answered:
                opaque.add(verb)
                continue
            for flag in flags:
                if flag in argv:
                    continue
                sweep.flag_results.setdefault(verb, []).append(
                    _invoke(sweep, verb, [*argv, flag])
                )
    sweep.flag_opaque = sorted(opaque)
    sweep.refs_after = _git(sweep.bare, "for-each-ref", env=git_env)
    return sweep


_SWEEP: list[Sweep] = []


def sweep() -> Sweep:
    if not _SWEEP:
        _SWEEP.append(_build())
    return _SWEEP[0]


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    for done in _SWEEP:
        shutil.rmtree(done.workdir, ignore_errors=True)


def _prohibited(call: dict, pr: int) -> str | None:
    """The spec's enumerated prohibition list, applied to one recorded `gh` invocation.

    Returns the name of the prohibited action, or None. Force-push is not here: it leaves no
    `gh` record and is checked through the remote's refs instead."""
    argv = call["argv"]
    joined = " ".join(argv)
    number = str(pr)
    if argv[:2] == ["pr", "merge"] and number in argv:
        if "--auto" in argv:
            return "enables auto-merge"
        if "--squash" in argv:
            return "squashes"
        if "--rebase" in argv:
            return "rebases"
        return "merges"
    if argv[:2] == ["pr", "close"] and number in argv:
        return "closes"
    if argv[:2] == ["pr", "edit"] and number in argv and "--base" in argv:
        return "mutates its base"
    if argv[:1] == ["api"]:
        if re.search(rf"pulls/{number}/merge\b", joined):
            return "merges (REST)"
        if (
            re.search(rf"pulls/{number}\b", joined)
            and "PATCH" in argv
            and "base=" in joined
        ):
            return "mutates its base (REST)"
        if "enablePullRequestAutoMerge" in joined:
            return "enables auto-merge (GraphQL)"
        if "enqueuePullRequest" in joined or "mergeQueue" in joined:
            return "enrolls it in a merge queue"
    return None


def test_the_sweep_actually_ran_every_registered_verb() -> None:
    """Anti-vacuity: a sweep that executed nothing would satisfy every prohibition below."""
    done = sweep()
    verbs = _registered_verbs()
    assert verbs, "no verbs parsed out of bin/conductor — the sweep would be vacuous"
    not_run = [verb for verb in verbs if verb not in done.results]
    assert not not_run, f"registered verbs that were never invoked: {not_run}"


def test_no_swept_invocation_died_in_argument_parsing() -> None:
    """ "Swept" must mean the verb RAN. An invocation that stopped in argparse — a missing
    subcommand, a missing positional, an option the verb does not have — executed none of the
    verb's own code, so every prohibition below is satisfied by a verb that did nothing.

    The argument table is hand-written and can therefore be wrong; this is what makes a wrong
    entry fail LOUDLY instead of counting as a sweep. A verb legitimately REFUSING (non-zero
    with a reason) is not this: it reached its logic and decided."""
    done = sweep()
    unrun = [
        f"conductor {verb} {' '.join(result['argv'])} -> rc={result['rc']}\n"
        f"    {(result['stderr'] or result['stdout']).strip().splitlines()[-1]}"
        for verb, result in done.every_result
        if result["rc"] != 0 and _USAGE_BLOCK.search(result["stderr"])
    ]
    assert not unrun, (
        "these invocations died in argument parsing and never reached the verb's own logic, so "
        "the prohibitions below were satisfied by verbs that did nothing:\n  "
        + "\n  ".join(unrun)
    )


def test_the_flag_surface_probe_answered_for_every_verb_that_can_answer() -> None:
    """Anti-vacuity for pass two: a verb whose flags are unswept must be a DECLARED residual.

    A verb that hand-rolls its arguments answers `--help` with no option surface, and its flags
    genuinely cannot be derived. That is a real hole in this assertion and it is recorded rather
    than assumed — a verb that stops answering joins `_FLAG_OPAQUE` deliberately, and a verb that
    starts answering leaves it, either way by an edit someone had to make."""
    done = sweep()
    assert set(done.flag_opaque) == set(_FLAG_OPAQUE), (
        "the set of verbs publishing no machine-readable option surface changed.\n"
        f"  measured: {sorted(done.flag_opaque)}\n"
        f"  declared: {sorted(_FLAG_OPAQUE)}\n"
        "A verb that newly stopped answering has lost its flag sweep; a verb that newly answers "
        "has gained one. Neither may happen silently."
    )


def test_at_least_one_derived_flag_was_actually_swept() -> None:
    """Anti-vacuity for pass two: a derivation that yields nothing for every verb would make the
    flag sweep a no-op that still reports green."""
    done = sweep()
    swept = {
        f"{verb} {' '.join(result['argv'])}"
        for verb, results in done.flag_results.items()
        for result in results
    }
    assert swept, (
        "pass two derived no option for any verb, so a prohibited action parked behind a flag "
        f"is unreachable by this sweep. opaque verbs: {done.flag_opaque}"
    )


def test_every_registered_verb_is_swept() -> None:
    """The argument table must cover the parsed verb list. A verb added to `bin/conductor` with
    no entry here stops the sweep loudly rather than going unmeasured."""
    done = sweep()
    assert not done.unswept_verbs, (
        "bin/conductor registers verbs this assertion has no argument vector for, so they were "
        f"never invoked against the run: {done.unswept_verbs}"
    )


def test_every_verb_the_spec_names_is_registered() -> None:
    """The spec's sweep names six verbs explicitly. A verb the CLI does not register cannot be
    invoked, so the prohibition is untested for whatever it would have done."""
    done = sweep()
    detail = [
        f"{verb} (owned by {VERB_OWNER.get(verb, 'the spec')})"
        for verb in done.missing_verbs
    ]
    assert not done.missing_verbs, (
        "the assertion spec sweeps these verbs and bin/conductor registers none of them, so "
        "their behaviour against the final pull request is unmeasured:\n  "
        + "\n  ".join(detail)
    )


def test_no_prohibited_action_is_recorded_against_the_final_pull_request() -> None:
    """Must-not-contain: any recorded invocation targeting the final pull request that merges,
    squashes, rebases, closes, mutates its base, enables auto-merge, or enrolls it in a merge
    queue."""
    done = sweep()
    found = [
        f"{_prohibited(call, FINAL_PR)}: gh {' '.join(call['argv'])}"
        for call in done.gh_calls
        if _prohibited(call, FINAL_PR)
    ]
    assert not found, "\n".join(
        ["Conductor acted to complete the final pull request:", *found]
    )
    assert str(FINAL_PR) not in done.merged_prs, (
        f"the final pull request was merged: {done.merged_prs}"
    )


def test_the_sweep_force_pushed_nothing() -> None:
    """Must-not-contain, the one prohibited action that leaves no `gh` record: the remote's
    refs — the run integration branch and the default branch among them — are unchanged."""
    done = sweep()
    assert done.refs_after == done.refs_before, (
        "the sweep changed a ref on the remote:\n"
        f"before:\n{done.refs_before}\nafter:\n{done.refs_after}"
    )


def test_a_phase_pull_request_merge_is_recorded() -> None:
    """Must-contain (anti-stub): at least one recorded `gh` call merging a PHASE pull request
    into its run integration branch. Without it, a Conductor that makes no GitHub calls at all,
    or whose merge path is unreachable, satisfies the prohibition vacuously."""
    done = sweep()
    merges = [
        call
        for call in done.gh_calls
        if call["argv"][:2] == ["pr", "merge"] and str(PHASE_PR) in call["argv"]
    ]
    assert merges, (
        "no phase pull request was merged during the sweep, so the prohibition above proves "
        f"nothing. conductor merge {PHASE_PR} reported: "
        f"{[r for r in done.results.get('merge', []) if r['argv'] == [str(PHASE_PR)]]}"
    )
    assert done.merged_prs == [str(PHASE_PR)], (
        f"expected exactly the phase pull request merged, recorded {done.merged_prs}"
    )


def test_finish_refuses_the_unmerged_final_pull_request() -> None:
    """Must-contain: a `finish` attempt on the unmerged final pull request exits non-zero and
    prints its URL and current state."""
    done = sweep()
    attempts = done.results.get("finish", [])
    assert attempts, (
        "bin/conductor registers no `finish` verb, so the unmerged final pull request was never "
        f"refused — {VERB_OWNER['finish']} owns it"
    )
    for attempt in attempts:
        report = f"{attempt['stdout']}\n{attempt['stderr']}"
        assert attempt["rc"] != 0, (
            f"finish exited 0 while the final pull request is unmerged:\n{report}"
        )
        assert f"/pull/{FINAL_PR}" in report, (
            f"finish did not print the final pull request's URL:\n{report}"
        )
        assert "OPEN" in report.upper(), (
            f"finish did not print the final pull request's current state:\n{report}"
        )
