"""`conductor gate lint` — frozen-gate quality + integrity (spec Phase 4, review B-4).

Pinned-command rule: a manifest command passes ONLY in the exact pinned argv shape —
optional allowlisted env assignments (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 required), then
exactly `python3 -m pytest`, with `--noconftest` and `-p no:cacheprovider` present, and
every remaining token a pytest flag or a test path. Shell compounds/wrappers, unknown
env prefixes, and unparseable commands are rejected fail-closed with a line containing
`unpinned` and the offending command verbatim.
"""

from conductor import gate_lint

TEST_REL = "assertions/sample/test_sample.py"
PINNED = (
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest "
    f"-p no:cacheprovider {TEST_REL}"
)
# has a real check, a negative clause, and no trivially-true assert (rules 2+3 clean)
GOOD_TEST_BODY = """import subprocess


def test_sample_behavior():
    out = subprocess.run(["echo", "ok"], capture_output=True, text=True).stdout
    assert "ok" in out
    assert "ERROR" not in out
"""

_MANIFEST = """\
assertions:
  - id: sample
    claim: "sample behavior holds"
    command: "{cmd}"
    setup: ""
    teardown: ""
    timeout: 30
    level: spec
    kind: example
"""


def _mk_project(tmp_path, command, body=GOOD_TEST_BODY, test_rel=TEST_REL):
    proj = tmp_path / "proj"
    test_file = proj / test_rel
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text(body)
    manifest = proj / "assertions" / "manifest.yaml"
    manifest.write_text(_MANIFEST.format(cmd=command))
    return proj


def _lint(proj):
    """Run the lint in-process against a fixture project (CONDUCTOR_HOME stance)."""
    return gate_lint.lint(str(proj / "assertions" / "manifest.yaml"), str(proj))


# ---------------------------------------------------------------- pinned command rule


def test_pinned_form_is_accepted(tmp_path):
    proj = _mk_project(tmp_path, PINNED)
    findings = _lint(proj)
    assert findings == [], findings


def test_pinned_form_with_reordered_flags_is_accepted(tmp_path):
    cmd = (
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest --noconftest "
        f"-p no:cacheprovider -q {TEST_REL}"
    )
    proj = _mk_project(tmp_path, cmd)
    assert _lint(proj) == []


def test_bare_pytest_is_rejected_and_named(tmp_path):
    proj = _mk_project(tmp_path, f"pytest {TEST_REL}")
    findings = _lint(proj)
    assert findings, "bare pytest must be rejected"
    joined = "\n".join(findings)
    assert "unpinned" in joined.lower()
    assert TEST_REL in joined  # offending command verbatim


def test_missing_noconftest_is_rejected(tmp_path):
    cmd = f"PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q -p no:cacheprovider {TEST_REL}"
    proj = _mk_project(tmp_path, cmd)
    joined = "\n".join(_lint(proj))
    assert "unpinned" in joined.lower() and TEST_REL in joined


def test_missing_autoload_env_is_rejected(tmp_path):
    cmd = f"python3 -m pytest -q --noconftest -p no:cacheprovider {TEST_REL}"
    proj = _mk_project(tmp_path, cmd)
    joined = "\n".join(_lint(proj))
    assert "unpinned" in joined.lower() and TEST_REL in joined


def test_missing_cacheprovider_disable_is_rejected(tmp_path):
    cmd = (
        f"PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest {TEST_REL}"
    )
    proj = _mk_project(tmp_path, cmd)
    joined = "\n".join(_lint(proj))
    assert "unpinned" in joined.lower() and TEST_REL in joined


def test_shell_compound_after_pinned_form_is_rejected(tmp_path):
    # token-presence matching would pass this; exact argv shape must not
    cmd = f"{PINNED} && python3 evil.py"
    proj = _mk_project(tmp_path, cmd)
    joined = "\n".join(_lint(proj))
    assert "unpinned" in joined.lower()


def test_shell_wrappers_and_compounds_are_rejected(tmp_path):
    for i, cmd in enumerate(
        [
            f"{PINNED}; true",
            f"{PINNED} | cat",
            f"bash -c '{PINNED}'",
            f"env PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest -p no:cacheprovider {TEST_REL}",
            f"`true` {PINNED}",
            f"$(true) {PINNED}",
            f"{PINNED} > /tmp/out",
        ]
    ):
        proj = _mk_project(tmp_path / f"case-{i}", cmd)
        joined = "\n".join(_lint(proj))
        assert "unpinned" in joined.lower(), cmd


def test_unknown_env_prefix_is_rejected(tmp_path):
    cmd = f"EVIL=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest -p no:cacheprovider {TEST_REL}"
    proj = _mk_project(tmp_path, cmd)
    joined = "\n".join(_lint(proj))
    assert "unpinned" in joined.lower()


def test_p_flag_may_only_disable_plugins(tmp_path):
    # `-p someplugin` LOADS a plugin — that reopens the bypass the pin closes
    cmd = (
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest "
        f"-p no:cacheprovider -p evilplugin {TEST_REL}"
    )
    proj = _mk_project(tmp_path, cmd)
    joined = "\n".join(_lint(proj))
    assert "unpinned" in joined.lower()


def test_trailing_second_command_without_separator_is_rejected(tmp_path):
    cmd = f"{PINNED} python3"
    proj = _mk_project(tmp_path, cmd)
    assert _lint(proj), "a trailing non-path token must be rejected"


def test_unparseable_command_is_rejected_fail_closed(tmp_path):
    proj = _mk_project(tmp_path, f"python3 -m pytest '{TEST_REL}")
    findings = _lint(proj)
    assert findings
    assert any("unparseable-command" in f for f in findings)


def test_unloadable_manifest_is_rejected_fail_closed(tmp_path):
    proj = tmp_path / "proj"
    (proj / "assertions").mkdir(parents=True)
    (proj / "assertions" / "manifest.yaml").write_text("nonsense: true\n")
    assert gate_lint.lint(str(proj / "assertions" / "manifest.yaml"), str(proj))


def test_main_exit_codes_and_clean_output(tmp_path, monkeypatch, capsys):
    proj = _mk_project(tmp_path, PINNED)
    monkeypatch.setenv("CONDUCTOR_HOME", str(proj))
    assert gate_lint.main() == 0
    out = capsys.readouterr()
    assert "unpinned" not in (out.out + out.err).lower()

    bad = _mk_project(tmp_path / "bad", f"pytest {TEST_REL}")
    monkeypatch.setenv("CONDUCTOR_HOME", str(bad))
    assert gate_lint.main() != 0
    out = capsys.readouterr()
    assert "unpinned" in (out.out + out.err).lower()
    assert TEST_REL in out.out + out.err


# ------------------------------------------------- negative-clause rule (A7)


POSITIVE_ONLY_BODY = """import subprocess


def test_sample_behavior():
    out = subprocess.run(["echo", "ok"], capture_output=True, text=True).stdout
    assert "ok" in out
"""


def test_positive_only_file_is_flagged_no_negative(tmp_path):
    proj = _mk_project(tmp_path, PINNED, body=POSITIVE_ONLY_BODY)
    joined = "\n".join(_lint(proj))
    assert "no negative" in joined.lower()
    assert "test_sample.py" in joined


def test_file_with_negative_clause_is_not_flagged(tmp_path):
    proj = _mk_project(tmp_path, PINNED)  # GOOD_TEST_BODY has `not in`
    joined = "\n".join(_lint(proj))
    assert "no negative" not in joined.lower()
    assert _lint(proj) == []


def test_assertnot_method_call_counts_as_negative(tmp_path):
    body = (
        "def test_x(self=None):\n"
        "    import unittest\n"
        "    tc = unittest.TestCase()\n"
        "    tc.assertNotIn('ERROR', 'ok')\n"
    )
    proj = _mk_project(tmp_path, PINNED, body=body)
    joined = "\n".join(_lint(proj))
    assert "no negative" not in joined.lower()


def test_unary_not_counts_as_negative(tmp_path):
    body = "def test_x():\n    assert not False\n    assert 'ok' in 'ok'\n"
    proj = _mk_project(tmp_path, PINNED, body=body)
    joined = "\n".join(_lint(proj))
    assert "no negative" not in joined.lower()


# ------------------------------------------------- trivially-true rule (A16)


TRIVIAL_WITH_NEGATIVE = """import subprocess


def test_sample_behavior():
    out = subprocess.run(["echo", "ok"], capture_output=True, text=True).stdout
    assert "ERROR" not in out
    assert True
"""


def test_trivial_assert_true_is_flagged_even_with_negative_clause(tmp_path):
    # rules are independent: the negative clause must not mask the tautology
    proj = _mk_project(tmp_path, PINNED, body=TRIVIAL_WITH_NEGATIVE)
    joined = "\n".join(_lint(proj))
    assert "trivially-true" in joined.lower()
    assert "test_sample.py" in joined
    assert "no negative" not in joined.lower()


def test_trivial_assert_one_and_bare_literal_are_flagged(tmp_path):
    for i, stmt in enumerate(["assert 1", 'assert "yes"']):
        body = f"def test_x():\n    assert 'ERROR' not in 'ok'\n    {stmt}\n"
        proj = _mk_project(tmp_path / f"case-{i}", PINNED, body=body)
        joined = "\n".join(_lint(proj))
        assert "trivially-true" in joined.lower(), stmt


def test_real_behavior_file_produces_no_trivial_finding(tmp_path):
    proj = _mk_project(tmp_path, PINNED)
    joined = "\n".join(_lint(proj))
    assert "trivial" not in joined.lower()


# ----------------------------------------------------------- fail-closed file rules


def test_syntax_error_test_file_is_rejected_naming_it(tmp_path):
    proj = _mk_project(tmp_path, PINNED, body="def test_x(:\n    assert ???\n")
    findings = _lint(proj)
    assert findings
    assert any("test_sample.py" in f for f in findings)


def test_missing_referenced_test_file_is_rejected(tmp_path):
    proj = _mk_project(tmp_path, PINNED)
    (proj / TEST_REL).unlink()
    findings = _lint(proj)
    assert findings
    assert any(TEST_REL in f for f in findings)


def test_directory_target_lints_collected_test_files(tmp_path):
    cmd = (
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q --noconftest "
        "-p no:cacheprovider assertions/sample"
    )
    proj = _mk_project(tmp_path, cmd, body=POSITIVE_ONLY_BODY)
    joined = "\n".join(_lint(proj))
    assert "no negative" in joined.lower() and "test_sample.py" in joined


# ------------------------------------------------- dispatch through bin/conductor


def test_gate_lint_dispatch_through_bin_conductor(tmp_path):
    import os
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    conductor = os.path.join(root, "bin", "conductor")
    proj = _mk_project(tmp_path, PINNED)
    env = dict(os.environ)
    env["CONDUCTOR_HOME"] = str(proj)
    proc = subprocess.run(
        [conductor, "gate", "lint"],
        cwd=str(proj),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "unpinned" not in (proc.stdout + proc.stderr).lower()

    bad = _mk_project(tmp_path / "bad", f"pytest {TEST_REL}")
    env["CONDUCTOR_HOME"] = str(bad)
    proc = subprocess.run(
        [conductor, "gate", "lint"],
        cwd=str(bad),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "unpinned" in (proc.stdout + proc.stderr).lower()
