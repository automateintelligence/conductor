"""Single-sourced branch identifiers (Phase 5, review B-5).

`run_branch_name` must be a pure deterministic function of the spec path so start and
autodev can never derive different names for the same spec; `default_branch` must resolve
the repo's real default (gh, then origin/HEAD) and fail OPEN to `main` — never empty —
because a downstream `git fetch "$R" ""` would operate on the wrong ref. Mirrors the
`conductor remote` precedent: one implementation per cross-skill string contract.
"""

import os
import re
import subprocess
from pathlib import Path

from conductor import branches

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")
NAME_RE = re.compile(r"conductor/run-[a-z0-9][a-z0-9._-]*\Z")


# --- run_branch_name: deterministic, canonical, collision-free ---


def test_same_path_is_byte_identical():
    spec = "docs/specs/2026-07-05-self-enforcement.md"
    assert branches.run_branch_name(spec) == branches.run_branch_name(spec)


def test_slug_carries_the_stem_and_matches_the_ref_format():
    name = branches.run_branch_name("docs/specs/2026-07-05-self-enforcement.md")
    assert name == "conductor/run-2026-07-05-self-enforcement"
    assert NAME_RE.fullmatch(name), name


def test_different_specs_get_different_names():
    a = branches.run_branch_name("docs/specs/2026-07-05-self-enforcement.md")
    b = branches.run_branch_name("docs/specs/2099-01-01-other-thing.md")
    assert a != b


def test_uppercase_stem_is_lowercased():
    name = branches.run_branch_name("Docs/MY-Spec.MD")
    assert name == "conductor/run-my-spec"


def test_spaces_collapse_to_single_hyphens():
    name = branches.run_branch_name("specs/my  spec   file.md")
    assert name == "conductor/run-my-spec-file"
    assert NAME_RE.fullmatch(name), name


def test_unicode_is_replaced_never_emitted():
    name = branches.run_branch_name("specs/spéc—niño.md")
    assert NAME_RE.fullmatch(name), name
    # must-not: no non-ascii byte survives into a ref name
    assert name.isascii()


def test_leading_dots_are_stripped_from_the_slug():
    name = branches.run_branch_name("specs/.hidden-spec.md")
    assert name == "conductor/run-hidden-spec"
    assert NAME_RE.fullmatch(name), name


def test_degenerate_stem_falls_back_to_hash_slug():
    # stem strips to nothing -> deterministic spec-<sha8>, never conductor/run-
    name = branches.run_branch_name("specs/---.md")
    assert NAME_RE.fullmatch(name), name
    assert re.fullmatch(r"conductor/run-spec-[0-9a-f]{8}", name), name
    # deterministic and distinct per path
    assert name == branches.run_branch_name("specs/---.md")
    assert name != branches.run_branch_name("specs/....md")


def test_non_alnum_leading_stem_falls_back_to_hash_slug():
    name = branches.run_branch_name("specs/-x-.md")
    # ".strip('-.')" leaves "x" here; a stem like "__" (strips to nothing after
    # hyphenation) must hash instead
    assert NAME_RE.fullmatch(name), name


# --- default_branch: resolve for real, fail OPEN to main ---


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


def _stub_gh(tmp: Path) -> Path:
    """A `gh` that always fails, prepended to PATH — never the network."""
    stub_bin = tmp / "stub-bin"
    stub_bin.mkdir()
    gh = stub_bin / "gh"
    gh.write_text("#!/bin/sh\nexit 1\n")
    os.chmod(gh, 0o755)
    return stub_bin


def _cli(repo: Path, stub_bin: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CONDUCTOR_HOME"] = str(repo)
    env["PATH"] = f"{stub_bin}:{env.get('PATH', '')}"
    return subprocess.run(
        [CONDUCTOR, *args],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_default_branch_resolves_origin_head_when_gh_fails(tmp_path, monkeypatch):
    stub_bin = _stub_gh(tmp_path)
    repo = _mk_repo(tmp_path, "resolvable", with_origin_head=True)
    monkeypatch.setenv("PATH", f"{stub_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("CONDUCTOR_HOME", str(repo))
    monkeypatch.chdir(repo)
    assert branches.default_branch() == "trunk"


def test_default_branch_falls_open_to_main(tmp_path, monkeypatch):
    stub_bin = _stub_gh(tmp_path)
    repo = _mk_repo(tmp_path, "unresolvable", with_origin_head=False)
    monkeypatch.setenv("PATH", f"{stub_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("CONDUCTOR_HOME", str(repo))
    monkeypatch.chdir(repo)
    out = branches.default_branch()
    assert out == "main"
    assert out != ""  # must-not: never empty


def test_default_branch_prefers_gh_answer(tmp_path, monkeypatch):
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    gh = stub_bin / "gh"
    gh.write_text("#!/bin/sh\necho develop\n")
    os.chmod(gh, 0o755)
    repo = _mk_repo(tmp_path, "gh-wins", with_origin_head=True)
    monkeypatch.setenv("PATH", f"{stub_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("CONDUCTOR_HOME", str(repo))
    monkeypatch.chdir(repo)
    assert branches.default_branch() == "develop"


def test_gh_probe_answers_for_conductor_home_not_cwd(tmp_path, monkeypatch):
    """Both probes must resolve the SAME repo: $CONDUCTOR_HOME, never the process cwd.

    The stub gh answers from a per-repo marker file in its own cwd, so a gh probe left
    bound to the process cwd would report the WRONG repo's default here."""
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    gh = stub_bin / "gh"
    gh.write_text("#!/bin/sh\ncat .default-branch 2>/dev/null || exit 1\n")
    os.chmod(gh, 0o755)
    home_repo = _mk_repo(tmp_path, "home-repo", with_origin_head=False)
    cwd_repo = _mk_repo(tmp_path, "cwd-repo", with_origin_head=False)
    (home_repo / ".default-branch").write_text("home-default\n")
    (cwd_repo / ".default-branch").write_text("cwd-default\n")
    monkeypatch.setenv("PATH", f"{stub_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("CONDUCTOR_HOME", str(home_repo))
    monkeypatch.chdir(cwd_repo)
    assert branches.default_branch() == "home-default"


def test_git_probe_answers_for_conductor_home_not_cwd(tmp_path, monkeypatch):
    stub_bin = _stub_gh(tmp_path)  # gh always fails -> the git probe decides
    home_repo = _mk_repo(tmp_path, "home-repo2", with_origin_head=True)  # trunk
    cwd_repo = _mk_repo(tmp_path, "cwd-repo2", with_origin_head=False)  # would say main
    monkeypatch.setenv("PATH", f"{stub_bin}:{os.environ['PATH']}")
    monkeypatch.setenv("CONDUCTOR_HOME", str(home_repo))
    monkeypatch.chdir(cwd_repo)
    assert branches.default_branch() == "trunk"


def test_default_branch_swallows_any_probe_exception(monkeypatch):
    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(branches, "_gh_default", boom)
    monkeypatch.setattr(branches, "_git_default", boom)
    assert branches.default_branch() == "main"


# --- CLI dispatch (bin/conductor) ---


def test_cli_run_branch_name_prints_one_line(tmp_path):
    stub_bin = _stub_gh(tmp_path)
    repo = _mk_repo(tmp_path, "cli", with_origin_head=False)
    proc = _cli(repo, stub_bin, "run-branch", "name", "docs/specs/a-b.md")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == "conductor/run-a-b\n"


def test_cli_run_branch_without_name_verb_is_usage_error(tmp_path):
    stub_bin = _stub_gh(tmp_path)
    repo = _mk_repo(tmp_path, "usage", with_origin_head=False)
    proc = _cli(repo, stub_bin, "run-branch")
    assert proc.returncode == 64
    assert "usage" in proc.stderr


def test_cli_run_branch_name_without_spec_is_usage_error(tmp_path):
    stub_bin = _stub_gh(tmp_path)
    repo = _mk_repo(tmp_path, "usage2", with_origin_head=False)
    proc = _cli(repo, stub_bin, "run-branch", "name")
    assert proc.returncode == 64
    assert "usage" in proc.stderr


def test_cli_default_branch_resolvable_trunk(tmp_path):
    stub_bin = _stub_gh(tmp_path)
    repo = _mk_repo(tmp_path, "cli-trunk", with_origin_head=True)
    proc = _cli(repo, stub_bin, "default-branch")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == "trunk\n"


def test_cli_default_branch_fails_open_to_main(tmp_path):
    stub_bin = _stub_gh(tmp_path)
    repo = _mk_repo(tmp_path, "cli-main", with_origin_head=False)
    proc = _cli(repo, stub_bin, "default-branch")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == "main\n"
