"""The documented `/conductor:start` driver step, driven with the host input UNSUPPLIED.

Every other host test in this tree hands the resolver its answer: `CONDUCTOR_HOST` in the
environment, a direct `runhost.record()`, or a planted `.conductor/host`. That covers the
resolution MECHANISM and proves nothing about what feeds it — which is how 1076 tests stayed
green on a branch whose entire goal ("a Codex user starts a run and the cron fire spawns
`codex`") did not hold: a fresh Codex start generated a `claude -p` driver.

So this module supplies nothing. It reads the driver-install invocation out of
`skills/start/SKILL.md` — the only entry point a real start goes through — substitutes the
placeholders a worker would substitute, runs exactly that, and then reads the CRONTAB the run
would actually fire and the script that cron line names.

Reading the invocation from the skill rather than restating it is the load-bearing part.
A restated argv is a copy that keeps passing after the skill drops `--host`, which is the
exact failure this file exists to catch.
"""

import os
import re
import shlex
import subprocess

import pytest

from conductor import driver, resume_script
from conductor.hosts import runhost

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
START_SKILL = os.path.join(ROOT, "skills", "start", "SKILL.md")

# The backticked invocation in step 6. Bounded by the closing backtick so a following
# sentence can never be swallowed into the argv.
_INVOCATION_RE = re.compile(r"conductor driver install[^`\n]*")

# What a worker substitutes before running it. Anything else left in angle brackets means the
# skill grew a placeholder this test does not know how to fill, and it fails rather than
# passing a literal `<...>` to the CLI.
_PLACEHOLDERS = {"<run-worktree>": None, "<this-host>": "codex"}


def documented_install_argv(worktree: str, host: str) -> list[str]:
    """`conductor driver install ...` exactly as `skills/start/SKILL.md` writes it."""
    with open(START_SKILL, encoding="utf-8") as f:
        body = f.read()
    found = _INVOCATION_RE.findall(body)
    assert found, f"{START_SKILL} documents no `conductor driver install` invocation"
    argv = shlex.split(found[0])
    assert argv[:3] == ["conductor", "driver", "install"], argv
    filled = {**_PLACEHOLDERS, "<run-worktree>": worktree, "<this-host>": host}
    out = []
    # `bin/conductor` shifts both `conductor` and the `driver` verb before dispatching to
    # `driver.main`, so the argv under test starts at `install`.
    for word in argv[2:]:
        if word.startswith("<"):
            assert word in filled, f"unknown placeholder {word!r} in {found[0]!r}"
            out.append(filled[word])
        else:
            out.append(word)
    return out


def _project(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True, timeout=30)
    wt = tmp_path / "run-worktree"
    wt.mkdir()
    return proj, resume_script.main_root(str(proj)), wt


def _stub_crontab(tmp_path, monkeypatch):
    """A `crontab` stub on PATH: `-l` reports no crontab, `-` records the written table."""
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir()
    written = tmp_path / "crontab-written"
    stub = stub_bin / "crontab"
    stub.write_text(
        "#!/bin/sh\n"
        'case "${1:-}" in\n'
        f'  -) cat > "{written}" ;;\n'
        '  *) echo "no crontab for user" >&2; exit 1 ;;\n'
        "esac\n"
    )
    os.chmod(stub, 0o755)
    monkeypatch.setenv("PATH", f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    return written


def _fired_script(crontab_body: str, root: str) -> str:
    """The script the installed cron lines actually run, read off the crontab itself."""
    marker = resume_script.cron_marker(root)
    lines = [ln for ln in crontab_body.splitlines() if marker in ln]
    assert len(lines) == 2, crontab_body
    paths = {w for ln in lines for w in shlex.split(ln) if w.endswith(".sh")}
    assert len(paths) == 1, paths
    with open(paths.pop(), encoding="utf-8") as f:
        return f.read()


@pytest.fixture
def clean_host_env(monkeypatch):
    """No override, no recording, no fixture — the state a fresh start begins in."""
    monkeypatch.delenv(runhost.HOST_ENV, raising=False)


def test_a_fresh_codex_start_generates_a_cron_line_that_spawns_codex(
    tmp_path, monkeypatch, clean_host_env
):
    """The branch's whole goal, checked from the documented entry point with nothing planted.

    No `CONDUCTOR_HOST`, no `runhost.record()`, no pre-written `.conductor/host`: the only
    input is the skill's own invocation, run the way a Codex worker would run it.
    """
    proj, root, wt = _project(tmp_path)
    written = _stub_crontab(tmp_path, monkeypatch)
    assert not os.path.exists(runhost.host_file(root))

    argv = documented_install_argv(str(wt), "codex")
    assert driver.main([*argv, "--project", str(proj)]) == 0

    script = _fired_script(written.read_text(), root)
    assert 'CODEX_BIN="$(command -v codex || true)"' in script
    assert '"$CODEX_BIN" exec' in script
    assert "CLAUDE_BIN" not in script
    assert "command -v claude" not in script
    assert "# HOST: codex" in script


def test_a_fresh_claude_start_still_generates_a_cron_line_that_spawns_claude(
    tmp_path, monkeypatch, clean_host_env
):
    """The same path on the other host: the fix must not make `claude` reachable only by
    accident of being the default."""
    proj, root, wt = _project(tmp_path)
    written = _stub_crontab(tmp_path, monkeypatch)

    argv = documented_install_argv(str(wt), "claude")
    assert driver.main([*argv, "--project", str(proj)]) == 0

    script = _fired_script(written.read_text(), root)
    assert 'CLAUDE_BIN="$(command -v claude || true)"' in script
    assert '"$CLAUDE_BIN" -p "/conductor:autodev"' in script
    assert "CODEX_BIN" not in script


def test_the_started_run_records_its_host_so_the_next_fire_agrees(
    tmp_path, monkeypatch, clean_host_env
):
    """A driver that fires codex while the run resolves claude is the split state that makes
    preflight, plan-lint and the merge gate consult the wrong host. The start path must leave
    the recording and the script agreeing."""
    proj, root, wt = _project(tmp_path)
    _stub_crontab(tmp_path, monkeypatch)

    assert (
        driver.main(
            [*documented_install_argv(str(wt), "codex"), "--project", str(proj)]
        )
        == 0
    )
    assert runhost.resolve(root) == "codex"


def test_the_documented_invocation_carries_the_host(tmp_path):
    """The skill is the only place the host is knowable — no subprocess below it can derive
    it. If this needle goes, the entry point silently reverts to the legacy claude default."""
    argv = documented_install_argv("/tmp/wt", "codex")
    assert "--host" in argv, argv
    assert argv[argv.index("--host") + 1] == "codex"
