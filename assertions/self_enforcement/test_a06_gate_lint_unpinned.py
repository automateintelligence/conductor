"""A6 — gate-lint-fail-closed-on-unpinned (property).

Contract pinned: `conductor gate lint` (run against the project's assertions/manifest.yaml)
exits non-zero and NAMES the offending command for any manifest command that could load an
unfrozen conftest or an autoloaded plugin — i.e. anything short of the full pinned form
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest -p no:cacheprovider <file>`
— and exits zero for the pinned form. An unparseable command is rejected (fail-closed).

Red-team notes: partial pinning (autoload disabled but no --noconftest, or vice versa) is
exercised too — a linter that greps for a single token passes one of those and fails here.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")

TEST_REL = "assertions/sample/test_sample.py"
PINNED = (
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest "
    f"-p no:cacheprovider {TEST_REL}"
)
GOOD_TEST_BODY = """import subprocess


def test_sample_behavior():
    out = subprocess.run(["echo", "ok"], capture_output=True, text=True).stdout
    assert "ok" in out
    assert "ERROR" not in out
"""


def _mk_project(tmp: Path, command: str) -> Path:
    proj = tmp / "proj"
    test_file = proj / TEST_REL
    test_file.parent.mkdir(parents=True)
    test_file.write_text(GOOD_TEST_BODY)
    manifest = proj / "assertions" / "manifest.yaml"
    manifest.write_text(
        "assertions:\n"
        "  - id: sample\n"
        '    claim: "sample behavior holds"\n'
        f'    command: "{command}"\n'
        '    setup: ""\n'
        '    teardown: ""\n'
        "    timeout: 30\n"
        "    level: spec\n"
        "    kind: example\n"
    )
    return proj


def _lint(proj: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CONDUCTOR_HOME"] = str(proj)
    return subprocess.run(
        [CONDUCTOR, "gate", "lint"],
        cwd=str(proj),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_unpinned_and_partially_pinned_commands_are_rejected_and_named(tmp_path):
    unpinned = [
        f"python3 -m pytest -q {TEST_REL}",
        f"pytest {TEST_REL}",
        # partial pinning: each misses one leg of the pinned form
        f"PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider {TEST_REL}",
        f"python3 -m pytest -q --noconftest -p no:cacheprovider {TEST_REL}",
    ]
    for i, cmd in enumerate(unpinned):
        proj = _mk_project(tmp_path / f"case-{i}", cmd)
        proc = _lint(proj)
        assert proc.returncode != 0, (cmd, proc.stdout, proc.stderr)
        out = proc.stdout + proc.stderr
        assert TEST_REL in out, (cmd, out)  # names the offending command


def test_pinned_form_is_accepted(tmp_path):
    proj = _mk_project(tmp_path, PINNED)
    proc = _lint(proj)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    # must-not: a clean gate is not flagged
    assert "unpinned" not in out.lower(), out


def test_unparseable_command_is_rejected_fail_closed(tmp_path):
    # unbalanced quote: shlex cannot tokenize it; fail-closed, never pass
    proj = _mk_project(tmp_path, f"python3 -m pytest '{TEST_REL}")
    proc = _lint(proj)
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
