"""A7 — gate-lint-flags-missing-negative-clause (property).

Contract pinned: `conductor gate lint` flags (non-zero, named) an assertion test file that
contains no negative check — nothing of the "must not contain" / `not in` / `assertNot`
family — and does NOT flag a file that has one. A positive-only frozen test certifies a
hollow gate: any stub that prints the happy string passes it.
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
POSITIVE_ONLY_BODY = """import subprocess


def test_sample_behavior():
    out = subprocess.run(["echo", "ok"], capture_output=True, text=True).stdout
    assert "ok" in out
"""
WITH_NEGATIVE_BODY = """import subprocess


def test_sample_behavior():
    out = subprocess.run(["echo", "ok"], capture_output=True, text=True).stdout
    assert "ok" in out
    assert "ERROR" not in out
"""


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


def test_positive_only_assertion_file_is_flagged(tmp_path):
    proj = _mk_project(tmp_path, POSITIVE_ONLY_BODY)
    proc = _lint(proj)
    assert proc.returncode != 0, (proc.stdout, proc.stderr)
    out = proc.stdout + proc.stderr
    assert "test_sample.py" in out, out  # names the offending file


def test_file_with_a_negative_clause_is_not_flagged(tmp_path):
    proj = _mk_project(tmp_path, WITH_NEGATIVE_BODY)
    proc = _lint(proj)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    # must-not: a compliant file is not named as an offender
    assert "no negative" not in out.lower(), out
