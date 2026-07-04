import io
import json
from types import SimpleNamespace
from typing import Any

from conductor import run_packet

RUN = "conductor/run-widget"


def _pr(number: int, title: str, merged: str, url: str) -> dict[str, Any]:
    return {"number": number, "title": title, "mergedAt": merged, "url": url}


_PRS = [
    _pr(
        12,
        "Phase 1: scaffold",
        "2026-07-01T10:00:00Z",
        "https://github.com/o/r/pull/12",
    ),
    _pr(13, "Phase 2: gate", "2026-07-02T11:30:00Z", "https://github.com/o/r/pull/13"),
]

_DIFF = " conductor/run_packet.py | 40 ++++++++\n 1 file changed, 40 insertions(+)"


def _fake_runner(*, prs: Any = None, diff: Any = _DIFF, issues: Any = None):
    """A subprocess.run stand-in dispatching on argv, in the injectable-fake style
    of test_merge_gate.py. prs/issues may be a list (JSON payload), an Exception
    to raise, or a SimpleNamespace to return verbatim."""

    def run(args, **kwargs):
        assert kwargs.get("timeout") is not None  # every call must be time-bounded
        if args[:3] == ["gh", "pr", "list"]:
            payload = [] if prs is None else prs
            if isinstance(payload, Exception):
                raise payload
            if isinstance(payload, SimpleNamespace):
                return payload
            return SimpleNamespace(stdout=json.dumps(payload), returncode=0)
        if args[:2] == ["git", "diff"]:
            if isinstance(diff, Exception):
                raise diff
            return SimpleNamespace(stdout=diff, returncode=0)
        if args[:3] == ["gh", "issue", "list"]:
            payload = [] if issues is None else issues
            if isinstance(payload, Exception):
                raise payload
            return SimpleNamespace(stdout=json.dumps(payload), returncode=0)
        raise AssertionError(f"unexpected subprocess call: {args}")

    return run


# ---- happy path: every section present, in order ----


def test_happy_path_has_all_sections_in_order():
    out = run_packet.build_packet(
        RUN,
        runner=_fake_runner(prs=_PRS),
        gate_output="[GATE] PASS 5/5",
        gate_exit=0,
        deferrals=["#5 flaky screenshot assertion"],
    )
    headings = [
        f"# Conductor run review packet — {RUN} → main",
        f"## Phase PRs merged into {RUN}",
        "## Changed files vs main",
        "## Done-gate evidence",
        "## Known deferrals / open items",
        "## Verification",
    ]
    positions = [out.index(h) for h in headings]
    assert positions == sorted(positions)


def test_header_says_owner_merges_never_conductor():
    out = run_packet.build_packet(RUN, runner=_fake_runner())
    header = out.splitlines()[:5]
    joined = "\n".join(header).lower()
    assert "owner" in joined and "conductor never merges" in joined


def test_phase_pr_bullets_render_number_title_date_url():
    out = run_packet.build_packet(RUN, runner=_fake_runner(prs=_PRS))
    assert (
        "- #12 Phase 1: scaffold (merged 2026-07-01) — https://github.com/o/r/pull/12"
        in out
    )
    assert (
        "- #13 Phase 2: gate (merged 2026-07-02) — https://github.com/o/r/pull/13"
        in out
    )


def test_custom_base_appears_in_header_and_diff_section():
    out = run_packet.build_packet(RUN, base="develop", runner=_fake_runner())
    assert f"# Conductor run review packet — {RUN} → develop" in out
    assert "## Changed files vs develop" in out


def test_diff_stat_rendered_in_fenced_block():
    out = run_packet.build_packet(RUN, runner=_fake_runner())
    assert f"```\n{_DIFF}\n```" in out


# ---- empty / missing inputs never crash ----


def test_empty_pr_list_says_none_recorded():
    out = run_packet.build_packet(RUN, runner=_fake_runner(prs=[]))
    assert "None recorded" in out


def test_gate_output_none_renders_attach_instruction():
    out = run_packet.build_packet(RUN, runner=_fake_runner())
    assert (
        "Gate output not supplied — run `conductor assert run --level spec` and attach."
        in out
    )


def test_gate_output_empty_string_renders_no_output_marker():
    # Codex round-1 LOW: gate exiting 0 with empty output must NOT read as
    # "not supplied" next to a Verification section saying exit status 0.
    out = run_packet.build_packet(
        RUN, runner=_fake_runner(), gate_output="", gate_exit=0
    )
    assert "(no gate output)" in out
    assert "Gate output not supplied" not in out
    assert "exit status: 0" in out


def test_gate_output_whitespace_only_renders_no_output_marker():
    out = run_packet.build_packet(RUN, runner=_fake_runner(), gate_output="\n\n")
    assert "(no gate output)" in out
    assert "Gate output not supplied" not in out


def test_deferrals_none_and_empty_render_none():
    for deferrals in (None, []):
        out = run_packet.build_packet(RUN, runner=_fake_runner(), deferrals=deferrals)
        idx = out.index("## Known deferrals / open items")
        section = out[idx:].split("##")[1]
        assert "None" in section


def test_deferrals_present_render_as_bullets():
    out = run_packet.build_packet(
        RUN, runner=_fake_runner(), deferrals=["#5 flaky test", "#9 docs debt"]
    )
    assert "- #5 flaky test" in out
    assert "- #9 docs debt" in out


# ---- hostile/markdown titles stay inside their bullet line (Codex round-1) ----


def test_pr_title_with_markdown_and_newlines_stays_on_one_bullet_line():
    evil = "] breakout **bold** \n# heading\r\ninjected"
    prs = [_pr(21, evil, "2026-07-03T00:00:00Z", "https://github.com/o/r/pull/21")]
    out = run_packet.build_packet(RUN, runner=_fake_runner(prs=prs))
    bullets = [ln for ln in out.splitlines() if ln.startswith("- #21 ")]
    assert bullets == [
        "- #21 ] breakout **bold** # heading injected (merged 2026-07-03)"
        " — https://github.com/o/r/pull/21"
    ]
    # the embedded newline must not mint a new markdown heading line
    assert not any(ln.startswith("# heading") for ln in out.splitlines())


def test_deferral_issue_title_control_chars_collapsed_to_one_line():
    issues = [{"number": 5, "title": "bad\r\n# breakout\t**bold** title"}]
    got = run_packet._collect_deferrals(None, runner=_fake_runner(issues=issues))
    assert got == ["#5 bad # breakout **bold** title"]


# ---- subprocess failure -> explicit unavailable line, packet still renders ----


def test_gh_pr_list_exception_renders_unavailable():
    out = run_packet.build_packet(
        RUN, runner=_fake_runner(prs=RuntimeError("gh timed out"))
    )
    idx = out.index(f"## Phase PRs merged into {RUN}")
    section = out[idx:].split("##")[1]
    assert "unavailable: gh timed out" in section
    assert "## Verification" in out  # the rest of the packet still renders


def test_gh_pr_list_nonzero_renders_unavailable():
    failed = SimpleNamespace(stdout="", stderr="HTTP 502", returncode=1)
    out = run_packet.build_packet(RUN, runner=_fake_runner(prs=failed))
    assert "unavailable: HTTP 502" in out


def test_git_diff_exception_renders_unavailable():
    out = run_packet.build_packet(
        RUN, runner=_fake_runner(diff=RuntimeError("not a git repo"))
    )
    idx = out.index("## Changed files vs main")
    section = out[idx:].split("##")[1]
    assert "unavailable: not a git repo" in section


def test_malformed_pr_json_renders_unavailable():
    bogus = SimpleNamespace(stdout="not json", stderr="", returncode=0)
    out = run_packet.build_packet(RUN, runner=_fake_runner(prs=bogus))
    idx = out.index(f"## Phase PRs merged into {RUN}")
    assert "unavailable:" in out[idx:].split("##")[1]


# ---- diff truncation at 200 lines ----


def test_diff_stat_truncated_at_200_lines():
    big = "\n".join(f" file{i}.py | 1 +" for i in range(250))
    out = run_packet.build_packet(RUN, runner=_fake_runner(diff=big))
    assert " file199.py | 1 +" in out
    assert " file200.py | 1 +" not in out
    assert "… truncated (50 more lines)" in out


def test_diff_stat_exactly_200_lines_not_truncated():
    exact = "\n".join(f" file{i}.py | 1 +" for i in range(200))
    out = run_packet.build_packet(RUN, runner=_fake_runner(diff=exact))
    assert "truncated" not in out


# ---- verification section ----


def test_verification_echoes_command_and_exit_status():
    out = run_packet.build_packet(RUN, runner=_fake_runner(), gate_exit=0)
    idx = out.index("## Verification")
    section = out[idx:]
    assert "$ conductor assert run --level spec" in section
    assert "exit status: 0" in section


def test_verification_without_gate_run_says_not_run():
    out = run_packet.build_packet(RUN, runner=_fake_runner())
    idx = out.index("## Verification")
    assert "exit status: not run" in out[idx:]


def test_verification_nonzero_exit_rendered():
    out = run_packet.build_packet(RUN, runner=_fake_runner(), gate_exit=3)
    assert "exit status: 3" in out


# ---- deferral collection (CLI helper) ----


def test_collect_deferrals_from_debt_label():
    issues = [{"number": 5, "title": "flaky test"}, {"number": 9, "title": "docs"}]
    got = run_packet._collect_deferrals(None, runner=_fake_runner(issues=issues))
    assert got == ["#5 flaky test", "#9 docs"]


def test_collect_deferrals_includes_milestone_and_dedupes():
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if "--label" in args:
            payload = [{"number": 5, "title": "flaky test"}]
        else:
            assert "--milestone" in args and "0.5.0" in args
            payload = [
                {"number": 5, "title": "flaky test"},  # duplicate of the label hit
                {"number": 7, "title": "milestone leftover"},
            ]
        return SimpleNamespace(stdout=json.dumps(payload), returncode=0)

    got = run_packet._collect_deferrals("0.5.0", runner=run)
    assert got == ["#5 flaky test", "#7 milestone leftover"]
    assert len(calls) == 2


def test_collect_deferrals_gh_failure_yields_unavailable_line():
    got = run_packet._collect_deferrals(
        None, runner=_fake_runner(issues=RuntimeError("gh down"))
    )
    assert got == ["unavailable: gh down"]


# ---- CLI arg parsing / main (function-level; __main__ stays thin) ----


def _cli_runner(gate_exit: int = 0, gate_lines: int = 3):
    """Fake runner that also answers the CLI's gate invocation."""
    inner = _fake_runner(prs=_PRS, issues=[])

    def run(args, **kwargs):
        if args[0] == "python3" and args[1].endswith("assertions/run.py"):
            assert args[2:] == ["--level", "spec"]
            out = "\n".join(f"gate line {i}" for i in range(gate_lines))
            return SimpleNamespace(stdout=out, stderr="", returncode=gate_exit)
        return inner(args, **kwargs)

    return run


def test_main_prints_packet_with_gate_evidence_and_exit():
    buf = io.StringIO()
    rc = run_packet.main([RUN], runner=_cli_runner(gate_exit=0), stdout=buf)
    out = buf.getvalue()
    assert rc == 0
    assert f"# Conductor run review packet — {RUN} → main" in out
    assert "gate line 2" in out  # gate output captured into Done-gate evidence
    assert "exit status: 0" in out


def test_main_base_flag_overrides_default():
    buf = io.StringIO()
    run_packet.main([RUN, "--base", "release"], runner=_cli_runner(), stdout=buf)
    assert f"# Conductor run review packet — {RUN} → release" in buf.getvalue()


def test_main_gate_output_is_tail_capped_at_30_lines():
    buf = io.StringIO()
    run_packet.main([RUN], runner=_cli_runner(gate_lines=50), stdout=buf)
    out = buf.getvalue()
    assert "gate line 49" in out  # tail keeps the end
    assert "gate line 19" not in out  # 50 - 30 = first 20 dropped


def test_main_milestone_flag_feeds_deferral_collection():
    seen = []
    inner = _cli_runner()

    def run(args, **kwargs):
        seen.append(args)
        return inner(args, **kwargs)

    buf = io.StringIO()
    run_packet.main([RUN, "--milestone", "0.5.0"], runner=run, stdout=buf)
    assert any("--milestone" in a and "0.5.0" in a for a in seen)


def test_main_gate_failure_still_renders_packet():
    inner = _fake_runner(prs=[], issues=[])

    def run(args, **kwargs):
        if args[0] == "python3":
            raise RuntimeError("gate crashed")
        return inner(args, **kwargs)

    buf = io.StringIO()
    rc = run_packet.main([RUN], runner=run, stdout=buf)
    out = buf.getvalue()
    assert rc == 0  # the packet is evidence, not enforcement: always renders
    assert "unavailable: gate crashed" in out
    assert "## Verification" in out


def test_direct_deferrals_param_is_sanitized_at_the_bullet_boundary():
    # codex r2 LOW: the CLI path sanitizes issue titles, but build_packet() must hold
    # the display contract for DIRECT callers too — a newline in a deferral string
    # would otherwise break out of its bullet into a standalone markdown heading.
    out = run_packet.build_packet(
        "conductor/run-x",
        deferrals=["#5 ok\n# injected heading"],
        gate_output="all green",
        gate_exit=0,
        runner=_fake_runner(),
    )
    assert "\n# injected heading" not in out
    assert "- #5 ok # injected heading" in out
