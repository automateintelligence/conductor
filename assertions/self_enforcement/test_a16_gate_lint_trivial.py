"""A16 — gate-lint-flags-trivially-true-assertion (property).

Contract pinned: `conductor gate lint` flags (non-zero, named) an assertion test file
containing a trivially-true assertion — `assert True`, `assert 1`, or a bare non-empty
literal — and does not flag a file that only asserts against real behavior. An
unparseable (syntax-error) test file is rejected fail-closed, never silently passed.

The trivial fixtures deliberately ALSO contain a negative clause, so a lint that only
implements the A7 (missing-negative) rule cannot pass this test by accident.
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
TRIVIAL_ASSERT_TRUE = """import subprocess


def test_sample_behavior():
    out = subprocess.run(["echo", "ok"], capture_output=True, text=True).stdout
    assert "ERROR" not in out
    assert True
"""
TRIVIAL_ASSERT_ONE = """import subprocess


def test_sample_behavior():
    out = subprocess.run(["echo", "ok"], capture_output=True, text=True).stdout
    assert "ERROR" not in out
    assert 1
"""
REAL_BEHAVIOR_BODY = """import subprocess


def test_sample_behavior():
    out = subprocess.run(["echo", "ok"], capture_output=True, text=True).stdout
    assert "ok" in out
    assert "ERROR" not in out
"""
UNPARSEABLE_BODY = "def test_sample_behavior(:\n    assert ???\n"


def _mk_project(tmp: Path, body: str) -> Path:
    proj = tmp / "proj"
    test_file = proj / TEST_REL
    test_file.parent.mkdir(parents=True)
    test_file.write_text(body)
    manifest = proj / "assertions" / "manifest.yaml"
    manifest.write_text(
        "assertions:\n"
        "  - id: sample\n"
        '    claim: "sample behavior holds"\n'
        f'    command: "{PINNED}"\n'
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


def test_trivially_true_assertions_are_flagged(tmp_path):
    for name, body in (("true", TRIVIAL_ASSERT_TRUE), ("one", TRIVIAL_ASSERT_ONE)):
        proj = _mk_project(tmp_path / name, body)
        proc = _lint(proj)
        assert proc.returncode != 0, (name, proc.stdout, proc.stderr)
        out = proc.stdout + proc.stderr
        assert "test_sample.py" in out, (name, out)  # names the offending file


def test_real_behavior_file_is_not_flagged(tmp_path):
    proj = _mk_project(tmp_path, REAL_BEHAVIOR_BODY)
    proc = _lint(proj)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    # must-not: a real-behavior file is not named as trivially true
    assert "trivial" not in out.lower(), out


def test_unparseable_test_file_is_rejected_fail_closed(tmp_path):
    proj = _mk_project(tmp_path, UNPARSEABLE_BODY)
    proc = _lint(proj)
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
