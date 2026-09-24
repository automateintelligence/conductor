"""A10 — default-branch-resolved-or-refused (property).

Contract pinned: `conductor default-branch` emits a branch name ONLY when it resolved one
from authoritative remote metadata. In every configuration stdout is either

  * exactly one NON-EMPTY line naming the repository's real default, with exit 0; or
  * nothing at all — not a name, not an empty line — with a non-zero exit and a refusal on
    stderr that names default-branch resolution as the cause.

RE-DERIVED, NOT WEAKENED (2026-08-21). The previous form of A10 pinned the second case as
"prints exactly `main`" — a literal fallback on resolution failure. Design
`docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.md` §"Branch, worktree,
and pull-request model" and assertion A-DH-6 invert that: "If the default branch cannot be
resolved, every automated merge is refused; there is no fallback default." Both halves of the
original claim survive here and one is added:

  1. NEVER AN EMPTY LINE (the original hazard: `D="$(conductor default-branch)"` followed by
     `git fetch "$R" "$D"` operating on the wrong ref). Now enforced more tightly than before
     — on the failure path stdout must be byte-empty, so there is no blank line for a caller
     to mistake for a value, and on the success path the single line must be non-empty.
  2. EXACTLY ONE LINE on the success path, unchanged.
  3. NEW: NEVER A LITERAL FALLBACK. No configuration may emit `main` or `master` unless that
     genuinely IS the repository's resolved default. The unresolvable repository must emit no
     branch name at all, and the resolvable repository's default is deliberately `trunk`, so a
     hard-coded `echo main` fails both legs rather than passing one by luck.

Fixture: throwaway git repos; a stub `gh` that always fails is prepended to PATH so the test
never depends on the network or a real gh auth state.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")

#: The resolvable repository's real default. Deliberately neither `main` nor `master`, so a
#: resolver that substitutes a literal cannot masquerade as having resolved anything.
AUTHORITATIVE = "trunk"

#: Literals a resolver must never substitute for an answer it does not have.
FALLBACKS = ("main", "master")


def _git(repo: Path, *args: str):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _mk_repo(tmp: Path, name: str, with_origin_head: bool) -> Path:
    repo = tmp / name
    repo.mkdir()
    _git(repo.parent, "init", "-q", name)
    _git(
        repo,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "seed",
    )
    _git(repo, "remote", "add", "origin", str(repo))
    if with_origin_head:
        _git(repo, "update-ref", f"refs/remotes/origin/{AUTHORITATIVE}", "HEAD")
        _git(
            repo,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            f"refs/remotes/origin/{AUTHORITATIVE}",
        )
    return repo


def _run_in(repo: Path, stub_bin: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CONDUCTOR_HOME"] = str(repo)
    env["PATH"] = f"{stub_bin}:{env.get('PATH', '')}"
    return subprocess.run(
        [CONDUCTOR, "default-branch"],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _stub_gh(tmp: Path) -> Path:
    stub_bin = tmp / "stub-bin"
    stub_bin.mkdir()
    gh = stub_bin / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n")
    os.chmod(gh, 0o755)
    return stub_bin


def test_resolvable_repo_prints_its_actual_default(tmp_path):
    stub_bin = _stub_gh(tmp_path)
    repo = _mk_repo(tmp_path, "resolvable", with_origin_head=True)
    proc = _run_in(repo, stub_bin)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout.strip()
    assert out == AUTHORITATIVE, proc.stdout
    # must-not: never empty, never multi-line, never a substituted literal
    assert out != ""
    assert "\n" not in out
    assert out not in FALLBACKS


def test_unresolvable_repo_refuses_and_emits_no_branch_name(tmp_path):
    """The inverted half. Neither probe can answer, so the verb must refuse: non-zero, an
    operator-actionable reason on stderr, and NOTHING on stdout."""
    stub_bin = _stub_gh(tmp_path)
    repo = _mk_repo(tmp_path, "unresolvable", with_origin_head=False)
    proc = _run_in(repo, stub_bin)
    assert proc.returncode != 0, (
        "conductor default-branch exited 0 with no resolvable default-branch metadata; "
        f"a caller cannot tell the answer is a guess:\n{proc.stdout}\n{proc.stderr}"
    )
    # must-not: no branch name AND no empty line — stdout is byte-empty
    assert proc.stdout == "", proc.stdout
    # must-not: no literal fallback anywhere in the refusal, on either stream
    emitted = f"{proc.stdout}\n{proc.stderr}".replace(":", " ").replace(",", " ")
    substituted = [f for f in FALLBACKS if f in emitted.split()]
    assert substituted == [], (
        f"the refusal presents a literal fallback {substituted}: {proc.stderr}"
    )
    lowered = proc.stderr.lower()
    assert "default" in lowered and "branch" in lowered, (
        f"the refusal does not name default-branch resolution as the cause: {proc.stderr}"
    )


def test_no_configuration_emits_an_empty_line_or_an_unresolved_name(tmp_path):
    """The property across BOTH configurations: stdout is either empty with a non-zero exit,
    or exactly one non-empty line equal to the repository's real default with exit 0. There is
    no third shape — in particular no blank line, and no name the repository does not have."""
    stub_bin = _stub_gh(tmp_path)
    cases = {
        "resolvable": _mk_repo(tmp_path, "sweep-resolvable", with_origin_head=True),
        "unresolvable": _mk_repo(
            tmp_path, "sweep-unresolvable", with_origin_head=False
        ),
    }
    offenders = []
    for name, repo in cases.items():
        proc = _run_in(repo, stub_bin)
        if proc.returncode == 0:
            lines = proc.stdout.splitlines()
            if lines != [AUTHORITATIVE]:
                offenders.append(
                    f"{name}: exited 0 having printed {proc.stdout!r}; the only permitted "
                    f"success output is the single line {AUTHORITATIVE!r}"
                )
        elif proc.stdout != "":
            offenders.append(
                f"{name}: refused (rc={proc.returncode}) but still wrote {proc.stdout!r} "
                "to stdout"
            )
    assert offenders == [], "\n".join(offenders)
