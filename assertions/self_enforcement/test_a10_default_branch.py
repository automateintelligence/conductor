"""A10 — default-branch-never-empty (property).

Contract pinned: `conductor default-branch` prints exactly one non-empty line. When the
repo's default is resolvable (origin/HEAD symbolic ref) it prints that branch; when both
`gh` and git resolution fail it prints exactly `main` (fail-open), never an empty string
that would make a downstream `git fetch`/`git merge` operate on the wrong ref.

Fixture: throwaway git repos; a stub `gh` that always fails is prepended to PATH so the
test never depends on network or a real gh auth state. The resolvable repo uses `trunk`
(not `main`) as its default so a hard-coded `echo main` fails it.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")


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
        _git(repo, "update-ref", "refs/remotes/origin/trunk", "HEAD")
        _git(
            repo,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/trunk",
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
    assert out == "trunk", proc.stdout
    # must-not: never empty, never multi-line
    assert out != ""
    assert "\n" not in out


def test_resolution_failure_falls_open_to_main(tmp_path):
    stub_bin = _stub_gh(tmp_path)
    repo = _mk_repo(tmp_path, "unresolvable", with_origin_head=False)
    proc = _run_in(repo, stub_bin)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout.strip()
    assert out == "main", proc.stdout
    # must-not: an empty line is never printed
    assert out != ""
    assert "\n" not in out
