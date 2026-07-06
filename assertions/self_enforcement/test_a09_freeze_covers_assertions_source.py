"""A9 — freeze-covers-assertions-source (contract).

Contract pinned: `conductor gate freeze` records a digest of the human-authored
`<spec>.assertions.md`, and `conductor gate verify` fails (tamper) after that file
changes — the done-DEFINITION is tamper-evident, not just the manifest and test files.

Fixture: a throwaway project (CONDUCTOR_HOME override) with a manifest, a referenced test
file, a spec + its `.assertions.md`, and a `.conductor/goal.md` naming the spec, so the
implementation may discover the assertions source either by glob or via the goal.
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


def _mk_project(tmp: Path) -> tuple:
    proj = tmp / "proj"
    test_file = proj / TEST_REL
    test_file.parent.mkdir(parents=True)
    test_file.write_text(GOOD_TEST_BODY)
    (proj / "assertions" / "manifest.yaml").write_text(
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
    specs = proj / "docs" / "specs"
    specs.mkdir(parents=True)
    spec = specs / "fixture-spec.md"
    spec.write_text("# Fixture spec\n\n## Expectations\n\n1. sample behavior holds\n")
    assertions_md = specs / "fixture-spec.md.assertions.md"
    assertions_md.write_text(
        "# Executable assertions\n\n## sample\n- **Claim:** sample behavior holds\n"
    )
    dot = proj / ".conductor"
    dot.mkdir()
    (dot / "goal.md").write_text("Implement docs/specs/fixture-spec.md until done\n")
    return proj, assertions_md


def _gate(proj: Path, sub: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["CONDUCTOR_HOME"] = str(proj)
    return subprocess.run(
        [CONDUCTOR, "gate", sub],
        cwd=str(proj),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_editing_the_assertions_source_trips_verify(tmp_path):
    proj, assertions_md = _mk_project(tmp_path)

    frozen = _gate(proj, "freeze")
    assert frozen.returncode == 0, frozen.stdout + frozen.stderr
    assert (proj / "assertions" / ".frozen").is_file()

    before = _gate(proj, "verify")
    out_before = before.stdout + before.stderr
    assert before.returncode == 0, out_before
    # must-not: an untouched gate is never reported tampered
    assert "tamper" not in out_before.lower(), out_before

    assertions_md.write_text(
        assertions_md.read_text() + "\n- **Claim:** weakened after freeze\n"
    )

    after = _gate(proj, "verify")
    out_after = after.stdout + after.stderr
    assert after.returncode != 0, (
        "gate verify stayed green after <spec>.assertions.md changed\n" + out_after
    )
    assert "tamper" in out_after.lower(), out_after
