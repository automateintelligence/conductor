"""A3 — resume-env-mode-0600 (property).

Contract pinned: `conductor.authority.write_resume_env(project_root, env)` writes the
canonical `<project>/.conductor/resume-env.sh`, returns its path, and the written file is
mode 0600 in EVERY case — fresh file, pre-existing looser file, and empty env — because
the file can carry the bypass flag and a shell-executed CONDUCTOR_MERGE_VERIFY command.

Red-team notes: asserting only the fresh-file case would pass a writer that never chmods
an existing file; the exact-IMODE check plus the explicit group/other-bits-are-zero check
close the "0644 but umask happened to be tight on the dev box" hole.
"""

import importlib
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _write_resume_env():
    # importlib (not a static import) so pyright stays green while the module is
    # unimplemented; at runtime a missing module still fails this test (RED).
    return importlib.import_module("conductor.authority").write_resume_env


def _assert_0600(path: Path):
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, oct(mode)
    # must-not: no group/other bit (read, write, or execute) may be set
    assert mode & 0o077 == 0, oct(mode)


def test_fresh_file_is_0600_at_canonical_path(tmp_path):
    write_resume_env = _write_resume_env()
    p = Path(
        write_resume_env(
            str(tmp_path),
            {"CONDUCTOR_RESUME_CLAUDE_FLAGS": "--dangerously-skip-permissions"},
        )
    )
    assert p.name == "resume-env.sh"
    assert p.parent == tmp_path / ".conductor"
    assert p.is_file()
    _assert_0600(p)


def test_preexisting_loose_file_is_tightened_to_0600(tmp_path):
    write_resume_env = _write_resume_env()
    pre = tmp_path / ".conductor" / "resume-env.sh"
    pre.parent.mkdir(parents=True)
    pre.write_text("# pre-existing\n")
    os.chmod(pre, 0o644)
    p = Path(write_resume_env(str(tmp_path), {"CONDUCTOR_MERGE_VERIFY": "true"}))
    assert p == pre
    _assert_0600(p)


def test_empty_env_still_writes_0600(tmp_path):
    write_resume_env = _write_resume_env()
    p = Path(write_resume_env(str(tmp_path), {}))
    assert p.is_file()
    _assert_0600(p)
