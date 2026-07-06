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
