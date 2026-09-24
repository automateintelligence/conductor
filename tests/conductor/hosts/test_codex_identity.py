"""A Codex session identity answers for the ``CODEX_HOME`` it was minted under.

The liveness proof for ``codex:<thread>:<boot>`` is a kernel lock on
``<CODEX_HOME>/thread-writer-locks/<thread>.lock``. The session that registers and the process
that later asks (a cron driver, ``conductor status``, a relocation scan) need not share a
``CODEX_HOME``, so the root the lock lives under is recorded at registration time and the
reader's own environment is never substituted for it.
"""

from __future__ import annotations

import fcntl
import os

import pytest

from conductor.hosts import base, codex, proc

THREAD = "019feab3-05f0-7081-90fb-18b96bc27db3"


@pytest.fixture
def adapter():
    return base.load("codex")


def _lock_file(home) -> str:
    directory = home / codex.THREAD_LOCK_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{THREAD}.lock"
    path.write_text("", encoding="utf-8")
    return str(path)


def test_liveness_reads_the_codex_home_recorded_at_registration(
    adapter, tmp_path, monkeypatch
):
    if proc.boot_id() is None:
        pytest.skip(
            "this platform exposes no boot id, so no codex identity can be minted"
        )
    session_home = tmp_path / "session-codex-home"
    lock = _lock_file(session_home)
    identity = adapter.session_identity(
        {codex.CONFIG_DIR_ENV: str(session_home), codex.SESSION_THREAD_ENV: THREAD}
    )
    assert identity is not None
    # The reader runs under a DIFFERENT CODEX_HOME, one with no lock directory at all.
    monkeypatch.setenv(codex.CONFIG_DIR_ENV, str(tmp_path / "reader-codex-home"))
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert adapter.process_alive(identity) is True
    finally:
        os.close(fd)
    # The kernel released the lock: a positive exit proof, read from the session's home.
    assert adapter.process_alive(identity) is False


def test_a_codex_home_containing_a_colon_survives_the_round_trip(
    adapter, tmp_path, monkeypatch
):
    if proc.boot_id() is None:
        pytest.skip(
            "this platform exposes no boot id, so no codex identity can be minted"
        )
    session_home = tmp_path / "odd:home %41"
    lock = _lock_file(session_home)
    identity = adapter.session_identity(
        {codex.CONFIG_DIR_ENV: str(session_home), codex.SESSION_THREAD_ENV: THREAD}
    )
    assert identity is not None
    monkeypatch.delenv(codex.CONFIG_DIR_ENV, raising=False)
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert adapter.process_alive(identity) is True
    finally:
        os.close(fd)


def test_an_identity_recorded_before_the_home_was_recorded_still_resolves(
    adapter, tmp_path, monkeypatch
):
    """Three-field identities on disk predate the recorded root; they keep the reader's
    ``CODEX_HOME`` — the only answer they ever had — rather than turning unreadable."""
    boot = proc.boot_id()
    if boot is None:
        pytest.skip("this platform exposes no boot id")
    home = tmp_path / "codex-home"
    lock = _lock_file(home)
    monkeypatch.setenv(codex.CONFIG_DIR_ENV, str(home))
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        assert adapter.process_alive(f"codex:{THREAD}:{boot}") is True
    finally:
        os.close(fd)


def test_a_recorded_home_that_is_not_absolute_cannot_be_interpreted(adapter):
    boot = proc.boot_id()
    if boot is None:
        pytest.skip("this platform exposes no boot id")
    assert adapter.process_alive(f"codex:{THREAD}:{boot}:relative%2Fhome") is None
