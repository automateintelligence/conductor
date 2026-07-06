import os
import subprocess
import sys
import textwrap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_gate_red_then_green(tmp_path):
    (tmp_path / "test_unknown_code.py").write_text(
        textwrap.dedent("""\
        from shortener import lookup
        def test_unknown_code_is_404():
            assert lookup("nope") == 404
    """)
    )
    (tmp_path / "manifest.yaml").write_text(
        textwrap.dedent(f"""\
        assertions:
          - id: unknown-code-404
            claim: "An unknown short code returns 404."
            command: "python3 -m pytest -q {tmp_path / "test_unknown_code.py"}"
            level: spec
            kind: example
    """)
    )
    env = {
        **os.environ,
        "CONDUCTOR_MANIFEST": str(tmp_path / "manifest.yaml"),
        "PYTHONPATH": str(tmp_path),
        # hermetic: run state in tmp; nonexistent baseline = freeze guard off, so the
        # repo's live assertions/.frozen can't tamper-fail this fabricated manifest
        "CONDUCTOR_HOME": str(tmp_path),
        "CONDUCTOR_FREEZE_BASELINE": str(tmp_path / "no-baseline"),
    }
    run = [
        sys.executable,
        os.path.join(ROOT, "assertions", "run.py"),
        "--level",
        "spec",
    ]
    assert (
        subprocess.run(run, env=env, cwd=ROOT).returncode == 1
    )  # no impl -> fail-closed
    (tmp_path / "shortener.py").write_text("def lookup(code):\n    return 404\n")
    assert subprocess.run(run, env=env, cwd=ROOT).returncode == 0  # behavior -> green
