"""Session-mode-aware unattended authority (spec Phase 1): the declared privileged-op set,
the fail-closed posture resolver, and the 0600 resume-env writer. Frozen assertions A1-A3
pin the public interface; these unit tests cover the same contract plus the edges."""

import os
import stat

from conductor import authority

# ---- RECIPE_PRIVILEGED_OPS: one declared set, each recipe verb a DISTINCT entry ----

# The seven privileged verb categories the autodev recipe performs (spec Phase 1).
CATEGORIES = {
    "branch": ("branch",),
    "push": ("push",),
    "gh-pr": ("gh pr",),
    "merge": ("merge",),
    "docker": ("docker", "conductor_merge_verify"),
    "subagent": ("subagent",),
    "writes": ("write",),
}


def test_ops_is_a_nonempty_frozenset_of_strings():
    ops = authority.RECIPE_PRIVILEGED_OPS
    assert isinstance(ops, frozenset)
    assert ops
    assert all(isinstance(o, str) and o for o in ops)


def test_every_category_is_covered_by_a_distinct_op():
    """No mega-string entry: each of the seven recipe verbs must have its own
    representative op (matching the frozen A1 distinct-representative check)."""
    ops = {op.lower() for op in authority.RECIPE_PRIVILEGED_OPS}
    matches = {
        cat: {op for op in ops if any(n in op for n in needles)}
        for cat, needles in CATEGORIES.items()
    }
    used: set = set()
    for cat in sorted(matches, key=lambda c: len(matches[c])):
        pick = next((op for op in sorted(matches[cat]) if op not in used), None)
        assert pick is not None, f"no distinct declared op covers: {cat}"
        used.add(pick)
    assert len(authority.RECIPE_PRIVILEGED_OPS) >= len(CATEGORIES)


# ---- resolve_posture: fail-closed, closed vocabulary ----

GARBAGE = [
    "",
    "wibble",
    "bypas",
    "bypassPermission",  # singular near-miss
    "permissions",
    "full",
    "allow-all",
    "0",
    "null",
    "None",
    "sudo",
    "trust",
    "yes",
    "plan?",
    "bypassPermissions extra",  # embedded token: ambiguous, must not over-grant
    "BYPASSPERMISSIONS",  # case near-miss: not the affirmative token
]


def test_recognized_modes_resolve_to_their_posture():
    assert authority.resolve_posture("bypassPermissions") == "full-bypass"
    assert authority.resolve_posture("default") == "supervised"
    assert authority.resolve_posture("plan") == "supervised"
    assert authority.resolve_posture("acceptEdits") == "scoped"


def test_surrounding_whitespace_is_tolerated_but_never_ambiguity():
    # a stripped exact token still resolves; anything with EXTRA tokens never does
    assert authority.resolve_posture("  bypassPermissions  ") == "full-bypass"
    assert authority.resolve_posture("bypassPermissions extra") == "supervised"


def test_unknown_or_unreadable_modes_fail_closed_to_supervised():
    for mode in GARBAGE + [None]:
        p = authority.resolve_posture(mode)
        assert p == "supervised", (mode, p)
        assert "bypass" not in p


def test_non_string_inputs_fail_closed_to_supervised():
    for mode in (0, 1, True, False, [], {}, object()):
        assert authority.resolve_posture(mode) == "supervised"  # type: ignore[arg-type]


def test_posture_vocabulary_is_closed():
    for mode in ["bypassPermissions", "default", "acceptEdits", "plan"] + GARBAGE:
        assert authority.resolve_posture(mode) in (
            "full-bypass",
            "scoped",
            "supervised",
        )


# ---- write_resume_env: canonical path, 0600 in every case, safe serialization ----


def _mode(path: str) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_fresh_file_is_0600_at_canonical_path(tmp_path):
    p = authority.write_resume_env(
        str(tmp_path),
        {"CONDUCTOR_RESUME_CLAUDE_FLAGS": "--dangerously-skip-permissions"},
    )
    assert p == str(tmp_path / ".conductor" / "resume-env.sh")
    assert os.path.isfile(p)
    assert _mode(p) == 0o600
    assert _mode(p) & 0o077 == 0


def test_preexisting_loose_file_is_tightened_to_0600(tmp_path):
    pre = tmp_path / ".conductor" / "resume-env.sh"
    pre.parent.mkdir(parents=True)
    pre.write_text("# pre-existing\n")
    os.chmod(pre, 0o644)
    p = authority.write_resume_env(str(tmp_path), {"CONDUCTOR_MERGE_VERIFY": "true"})
    assert p == str(pre)
    assert _mode(p) == 0o600


def test_empty_env_still_writes_0600(tmp_path):
    p = authority.write_resume_env(str(tmp_path), {})
    assert os.path.isfile(p)
    assert _mode(p) == 0o600


def test_values_are_shell_quoted_but_never_double_wrapped(tmp_path):
    """Serialization contract: KEY={shlex.quote(value)} — wrapping the quoted value in
    EXTRA double quotes would preserve the quote characters through the driver's unquoted
    ${CONDUCTOR_RESUME_CLAUDE_FLAGS:-} expansion and break flags with spaces."""
    p = authority.write_resume_env(
        str(tmp_path),
        {"CONDUCTOR_RESUME_CLAUDE_FLAGS": "--settings /path/with space"},
    )
    text = open(p).read()
    assert "CONDUCTOR_RESUME_CLAUDE_FLAGS='--settings /path/with space'\n" in text
    assert "\"'" not in text  # never KEY="'...'"


def test_simple_value_round_trips_through_sh(tmp_path):
    import subprocess

    p = authority.write_resume_env(
        str(tmp_path),
        {
            "CONDUCTOR_MERGE_VERIFY": "cd backend && pytest -q",
            "DOCKER_HOST": "unix:///s",
        },
    )
    proc = subprocess.run(
        ["sh", "-c", f'. "{p}" && printf %s "$CONDUCTOR_MERGE_VERIFY"'],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "cd backend && pytest -q"


def test_invalid_key_names_are_rejected(tmp_path):
    import pytest

    for bad in ("lower", "1START", "HAS-DASH", "HAS SPACE", "", "PATH=X"):
        with pytest.raises(ValueError):
            authority.write_resume_env(str(tmp_path), {bad: "v"})
    # a rejected env must not leave a partial file behind
    assert not (tmp_path / ".conductor" / "resume-env.sh").exists()
