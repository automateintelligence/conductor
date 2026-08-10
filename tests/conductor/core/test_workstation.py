"""The workstation identity (design §"Project and run identity"): a random Conductor installation
ID in host-neutral user configuration, shared by the Claude and Codex adapters.

It must not be derived from personal or hardware data, and it must never be regenerated — Plan 02's
rebind compares a project's recorded workstation against this value to refuse cross-machine
takeover."""

from __future__ import annotations

import getpass
import os
import socket
import stat

from conductor.core import workstation


def test_config_home_prefers_the_conductor_override(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "cfg"))
    assert workstation.config_home() == str(tmp_path / "cfg")


def test_config_home_falls_back_to_xdg_then_dot_config(tmp_path, monkeypatch):
    monkeypatch.delenv("CONDUCTOR_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert workstation.config_home() == str(tmp_path / "xdg" / "conductor")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert workstation.config_home() == str(tmp_path / "home" / ".config" / "conductor")


def test_the_id_is_created_once_and_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "cfg"))
    first = workstation.workstation_id()
    assert first and len(first) == 32
    assert workstation.workstation_id() == first


def test_the_id_is_random_and_not_derived_from_user_or_host(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "a"))
    first = workstation.workstation_id()
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "b"))
    second = workstation.workstation_id()
    assert first != second
    for leak in (getpass.getuser(), socket.gethostname(), os.path.expanduser("~")):
        assert leak.lower() not in first.lower()


def test_the_installation_file_is_owner_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "cfg"))
    workstation.workstation_id()
    mode = stat.S_IMODE(os.stat(workstation.installation_file()).st_mode)
    assert mode == 0o600


def test_a_concurrent_creator_wins_and_both_callers_agree(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_CONFIG_HOME", str(tmp_path / "cfg"))
    os.makedirs(str(tmp_path / "cfg"), exist_ok=True)
    with open(workstation.installation_file(), "w", encoding="utf-8") as handle:
        handle.write('{"schema_version": 1, "workstation_id": "deadbeef" }')
    assert workstation.workstation_id() == "deadbeef"
