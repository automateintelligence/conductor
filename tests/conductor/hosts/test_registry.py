"""The host registry and the closed vocabularies (design §"System architecture").

Two hosts, two postures projections, one interface. The vocabularies are closed sets for the
same reason Plan 01's status vocabulary is: a typo'd host id must fail at the boundary, not
resolve to a silently different launch.
"""

from __future__ import annotations

import dataclasses

import pytest

from conductor.hosts import base


def test_host_ids_are_exactly_the_two_supported_hosts():
    assert base.HOST_IDS == ("claude", "codex")


def test_posture_vocabulary_is_closed_and_ordered_least_to_most_privileged():
    assert base.POSTURES == ("supervised", "scoped", "full-bypass")


@pytest.mark.parametrize("host_id", base.HOST_IDS)
def test_load_returns_an_adapter_whose_id_matches_the_request(host_id):
    adapter = base.load(host_id)
    assert adapter.id == host_id


def test_load_refuses_an_unknown_host_and_names_the_supported_set():
    with pytest.raises(base.UnknownHost) as excinfo:
        base.load("gemini")
    assert "gemini" in str(excinfo.value)
    assert "claude" in str(excinfo.value) and "codex" in str(excinfo.value)


def test_opposite_is_an_involution_over_the_host_set():
    for host_id in base.HOST_IDS:
        assert base.opposite(base.opposite(host_id)) == host_id
        assert base.opposite(host_id) != host_id


def test_opposite_refuses_an_unknown_host():
    with pytest.raises(base.UnknownHost):
        base.opposite("gemini")


def test_dispatch_result_is_frozen_and_carries_the_named_result_file():
    result = base.DispatchResult(
        host="codex",
        argv=("codex", "exec"),
        returncode=0,
        result_path="/tmp/out.txt",
        result_text="done",
        truncated=False,
        duration_s=1.5,
    )
    assert dataclasses.is_dataclass(result)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.returncode = 1  # type: ignore[misc]


def test_every_error_is_distinguishable_and_none_is_a_bare_exception():
    for exc in (
        base.UnknownHost,
        base.HostUnavailable,
        base.HostVersionTooOld,
        base.PermissionProfileError,
        base.HookContractUnverified,
        base.DispatchTimeout,
    ):
        assert issubclass(exc, Exception)
        assert exc is not Exception
    # UnknownHost is a ValueError so a caller validating input can catch it with the rest of
    # its argument validation; the runtime failures are RuntimeErrors and must not be caught
    # by that same handler.
    assert issubclass(base.UnknownHost, ValueError)
    assert issubclass(base.PermissionProfileError, ValueError)
    assert not issubclass(base.HostUnavailable, ValueError)
    assert not issubclass(base.HookContractUnverified, ValueError)


def test_the_protocol_declares_every_member_the_adapters_must_implement():
    """The interface is fixed in Task 1 so later tasks fill it in rather than grow it."""
    expected = {
        "id",
        "executable",
        "source_root",
        "version",
        "minimum_version",
        "upgrade_hint",
        "native_invocation",
        "launch_prompt",
        "worker_argv",
        "worker_env",
        "reviewer_argv",
        "permission_profile",
        "validate_permissions",
        "process_identity",
        "process_alive",
        "processes_under",
        "install_hooks",
        "hook_installed",
        "dispatch_implementation",
    }
    declared = {n for n in base.HostAdapter.__annotations__} | {
        n for n in vars(base.HostAdapter) if not n.startswith("_")
    }
    assert expected <= declared, expected - declared
