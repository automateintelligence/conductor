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


# ---- step 0: preflight records the host, from the skill's own invocation -----------------
#
# Preflight and plan-lint run long before step 6's `driver install --host`. With nothing
# recorded yet they answered the legacy `claude` default, so a fresh Codex start checked
# Claude's skill roots and demanded the `codex` reviewer. The step-0 invocation is read out of
# the skill for the same reason the driver-install one is: a restated argv keeps passing after
# the skill drops `--host`.

_PREFLIGHT_RE = re.compile(r"conductor preflight[^`\n]*")


def documented_preflight_argv(host: str) -> list[str]:
    """The FIRST `conductor preflight ...` in `skills/start/SKILL.md` (step 0), as argv after
    the verb — what `bin/conductor` hands `preflight.main`."""
    with open(START_SKILL, encoding="utf-8") as f:
        body = f.read()
    found = _PREFLIGHT_RE.findall(body)
    assert found, f"{START_SKILL} documents no `conductor preflight` invocation"
    argv = shlex.split(found[0])
    assert argv[:2] == ["conductor", "preflight"], argv
    out = []
    for word in argv[2:]:
        if word.startswith("<"):
            assert word == "<this-host>", (
                f"unknown placeholder {word!r} in {found[0]!r}"
            )
            out.append(host)
        else:
            out.append(word)
    return out


def _fresh_repo(tmp_path):
    proj = tmp_path / "project"
    proj.mkdir()
    subprocess.run(["git", "init", "-q", str(proj)], check=True, timeout=30)
    return proj


def test_the_documented_preflight_declares_the_host(tmp_path):
    argv = documented_preflight_argv("codex")
    assert argv == ["--host", "codex"], argv


def test_a_fresh_codex_start_preflights_the_codex_host(tmp_path, monkeypatch):
    """Nothing planted: a Codex machine missing only the `claude` reviewer, a fresh repo, no
    `$CONDUCTOR_HOST`. Step 0's documented invocation must check CODEX's skill set — so it
    asks for `$claude` — and never Claude's."""
    from conductor import preflight
    from tests.conductor.test_preflight import _codex_install

    _codex_install(tmp_path, monkeypatch, review_wrapper="codex", pin_host=False)
    proj = _fresh_repo(tmp_path)
    monkeypatch.chdir(proj)

    seen = {}
    real_check = preflight.check

    def _spy(**kw):
        seen["out"] = real_check(**kw)
        return seen["out"]

    monkeypatch.setattr(preflight, "check", _spy)
    assert preflight.main(documented_preflight_argv("codex")) == 1
    assert seen["out"]["missing"] == ["$claude"], seen["out"]
    assert runhost.recorded(str(proj)) == "codex"


def test_a_fresh_codex_start_passes_preflight_on_a_complete_codex_machine(
    tmp_path, monkeypatch, capsys
):
    from conductor import preflight
    from tests.conductor.test_preflight import _codex_install

    _codex_install(tmp_path, monkeypatch, pin_host=False)
    proj = _fresh_repo(tmp_path)
    monkeypatch.chdir(proj)
    assert preflight.main(documented_preflight_argv("codex")) == 0
    assert "on host codex" in capsys.readouterr().out


def test_step_six_install_agrees_with_the_step_zero_recording(
    tmp_path, monkeypatch, clean_host_env
):
    """Step 0 records, step 6 passes the same id: the second leaves the recording as it is,
    and the driver it writes spawns the recorded host."""
    proj, root, wt = _project(tmp_path)
    written = _stub_crontab(tmp_path, monkeypatch)
    runhost.declare(str(proj), "codex")
    argv = documented_install_argv(str(wt), "codex")
    assert driver.main([*argv, "--project", str(proj)]) == 0
    assert runhost.recorded(root) == "codex"
    assert '"$CODEX_BIN" exec' in _fired_script(written.read_text(), root)


def test_resuming_a_run_from_the_other_host_is_refused_at_step_zero(
    tmp_path, monkeypatch, clean_host_env
):
    """A live run's host is never rewritten by a start. The refusal happens at step 0, before
    any check — so step 6's `driver install --host` is never reached with the wrong id."""
    from conductor import preflight

    proj, root, _ = _project(tmp_path)
    runhost.record(root, "claude")
    monkeypatch.chdir(proj)
    rc = preflight.main(documented_preflight_argv("codex"))
    assert rc == preflight.HOST_REFUSED
    assert runhost.recorded(root) == "claude"
