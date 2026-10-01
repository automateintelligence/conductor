"""README claims that must match the code they describe (codex review of PR #100).

Each check reads its expected value from the module that owns it where one exists, so the
README cannot be edited into agreement with a stale copy.
"""

from __future__ import annotations

import json
import os
import re

from conductor import preflight
from conductor.hosts import base

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _readme() -> str:
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        return f.read()


def _section(text: str, heading: str) -> str:
    """From `heading` to the next heading of the same or higher level."""
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    nxt = re.compile(rf"(?m)^#{{1,{level}}} ")
    m = nxt.search(text, start + len(heading))
    return text[start : m.start() if m else len(text)]


def test_stopping_a_run_removes_the_tier_b_crontab_lines():
    """`CronDelete` removes the in-session cron only; the `@reboot` and `*/20` lines that
    `driver install` writes keep firing until `resume-script uninstall-cron` removes them."""
    stop = _section(_readme(), "### 4. Check in, resume, or stop")
    assert "conductor resume-script uninstall-cron --project" in stop


def test_unattended_authority_names_each_hosts_own_flags_variable():
    section = _section(_readme(), "#### Unattended authority")
    for host_id in base.HOST_IDS:
        adapter = base.load(host_id)
        assert adapter.FLAGS_VAR in section, adapter.FLAGS_VAR
        assert adapter.POSTURE_EXAMPLES["full-bypass"] in section


def test_the_default_branch_row_documents_the_fail_closed_refusal():
    row = next(
        ln
        for ln in _readme().splitlines()
        if ln.startswith("| `conductor default-branch`")
    )
    assert "falls open" not in row and "never empty" not in row
    assert "exits `1`" in row and "stdout empty" in row


def test_spec_kit_is_not_listed_as_a_required_skill():
    """Preflight's required set has no spec-kit skill, so the prerequisites must not claim it."""
    assert not any(
        "speckit" in name or "spec-kit" in name for name in preflight.REQUIRED_SKILLS
    )
    prereqs = _section(_readme(), "### Prerequisites")
    required_part = prereqs.split("Optional", 1)[0]
    assert "spec-kit" not in required_part


def test_the_review_step_is_host_neutral():
    """The loop diagram used to prescribe `/codex review` on every host."""
    assert "/codex review" not in _readme()


def test_cli_reference_documents_the_review_and_usage_verbs():
    ref = _section(_readme(), "## CLI reference")
    assert "`conductor review <pr> --brief <file>" in ref
    assert "`conductor usage ingest" in ref


def test_cli_reference_states_the_review_default_timeout():
    from conductor import review_cmd

    ref = _section(_readme(), "## CLI reference")
    assert f"default {review_cmd._DEFAULT_REVIEW_TIMEOUT_S:g} s" in ref


def test_install_tells_an_upgrader_to_regenerate_the_driver():
    from conductor import resume_script

    install = _section(_readme(), "## Install")
    assert "**Upgrading to 0.11.0:**" in install
    assert "re-run `/conductor:start`" in install
    assert "`conductor driver install --worktree <path>`" in install
    assert f"(template {resume_script.TEMPLATE_VERSION})" in install
    assert "records worker usage" in install


def test_plugin_manifests_carry_the_same_version():
    versions = set()
    for rel in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            versions.add(json.load(f)["version"])
    assert len(versions) == 1, versions
