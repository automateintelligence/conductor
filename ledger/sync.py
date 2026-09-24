import re
from collections.abc import Iterator
from typing import Any

from ledger import gate_link

_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_H2_ANY = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_STATUS_SUFFIX = re.compile(r"\s+\[([^\]]+)\]\s*$")
_PHASE_PREFIX = re.compile(r"^phase\b", re.IGNORECASE)
_TRAILING_PARENS = re.compile(r"\(([^()]*)\)\s*$")
_TASK = re.compile(r"^- \[ \] (.+)$", re.MULTILINE)
_CHECKLIST = re.compile(r"- \[[ xX]\] #(\d+)")


def _assertion_tokens(title: str) -> list[str]:
    """Assertion ids named by a phase heading's TRAILING parens — ``(A3, A4, A5)`` or
    ``(A8/A16/A19)``. Every token must be whitespace-free AND contain a digit (assertion
    ids are numbered; ``(optional)`` is prose — codex #4); one bad token rejects the whole
    group, never half-parsed."""
    m = _TRAILING_PARENS.search(title)
    if not m:
        return []
    tokens = [t.strip() for t in re.split(r"[,/]", m.group(1)) if t.strip()]
    if (
        not tokens
        or any(re.search(r"\s", t) for t in tokens)
        or not all(re.search(r"\d", t) for t in tokens)
    ):
        return []
    return tokens


def _phase_heading(raw: str) -> tuple[str, str, list[str]] | None:
    """(title, status, assertion-tokens) if this H2 is a phase heading, else None.
    A phase is an H2 with a trailing ``[status]`` (conductor dialect) OR one starting
    ``Phase`` (the dialect real plan-writing skills emit — dogfood finding: demanding
    ``[status]`` made ``convert`` unusable on real plans, so the task layer got dropped).
    Status defaults to ``ready`` when the bracket is absent."""
    m = _STATUS_SUFFIX.search(raw)
    if m:
        title, status = raw[: m.start()].rstrip(), m.group(1).strip()
    elif _PHASE_PREFIX.match(raw):
        title, status = raw, "ready"
    else:
        return None
    return title, status, _assertion_tokens(title)


# CommonMark fence: 3+ backticks or tildes, indented up to 3 spaces, optional info string.
_FENCE = re.compile(r"^ {0,3}(?P<f>`{3,}|~{3,})(?P<info>.*)$")


def _opens_fence(m: re.Match[str]) -> bool:
    """Whether a fence-shaped line actually OPENS a fenced block (CommonMark §4.5).

    A backtick fence's info string may not contain a backtick — ```` ```md`x ```` is inline
    code in a paragraph, not a fence. A tilde fence's info string may, and that asymmetry is
    the spec's, not a shortcut here. Accepting any info text let a line that opens no fence in
    any renderer open one in this parser, which then SUPPRESSED every marker finding to the end
    of the phase: a false negative, and the quietest possible failure for a hard check."""
    return not (m.group("f")[0] == "`" and "`" in m.group("info"))


def _fence_scan(text: str) -> Iterator[tuple[int, str, bool, str | None]]:
    """(line-start offset, line without its newline, inside-a-fenced-block, fence still open
    after this line) per line.

    THE fence state machine for plan parsing. The section splitter (`_h2_headings`, used by
    this module, `phase_done` and plan-lint) and plan-lint's per-line marker check all read
    it, so "what is fenced" cannot mean two things between the lint and the ledger. A fence's
    own delimiter lines count as inside: they are markup, never plan content.

    A fence closes only on the SAME character, run at least as long, and nothing but
    whitespace after it — so a ``` inside a ~~~ block, and a ``` inside a ```` block, are
    both content. Plans document markdown, so nested fences are ordinary here.

    An UNTERMINATED fence runs to the end of `text`, which is what CommonMark does with an
    unclosed fence at the end of its container. It is also the safe direction for the marker
    check: that check is a hard failure, so a missed finding costs a warning nobody got, while
    a false one costs a legitimate plan its exit 0. The fourth element is what lets the
    splitter take the OPPOSITE reading — see `_closed_fence_offsets`.
    """
    fence: str | None = None
    pos = 0
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is None:
            if m is not None and _opens_fence(m):
                inside = True
                fence = m.group("f")
            else:
                inside = False
        else:
            inside = True
            if (
                m is not None
                and m.group("f")[0] == fence[0]
                and len(m.group("f")) >= len(fence)
                and not m.group("info").strip()
            ):
                fence = None
        yield pos, line, inside, fence
        pos += len(line) + 1


def _closed_fence_offsets(text: str) -> set[int]:
    """Line-start offsets of lines inside a CLOSED fenced block, delimiters included.

    Closed only, and that asymmetry against plan-lint's `_unfenced_lines` is the point. The
    splitter reads the WHOLE plan, so an unterminated fence would run past every following
    heading and merge
    the rest of the file into one phase — a forgotten ``` costing the plan its remaining
    phases, their pointers reported as duplicates of the phase that swallowed them. A heading
    is markup strong enough to bound the damage: an unterminated fence keeps suppressing the
    marker check to the end of ITS OWN section (`_unfenced_lines`, which never sees more than
    one section) while the headings after it stay headings."""
    out: set[int] = set()
    pending: list[int] = []
    for pos, _line, inside, still_open in _fence_scan(text):
        if inside:
            pending.append(pos)
        # Nothing open after this line, so any pending fence CLOSED on it.
        if still_open is None:
            out.update(pending)
            pending.clear()
    # A leftover `pending` is an unterminated fence — deliberately left unfenced.
    return out


def _h2_headings(text: str) -> list[re.Match[str]]:
    """Every H2 in ``text`` that is a real heading: an H2 inside a CLOSED fenced block is
    example text and is skipped. THE section splitter's heading list — ``parse_plan_md``,
    ``phase_done`` and plan-lint all split on it, so the phases plan-lint grades are exactly
    the phases ``convert`` creates issues for and the worker ticks. A skipped heading does not
    end the enclosing section either: the fence it sits in is part of that section's body.
    An UNTERMINATED fence hides nothing (see ``_closed_fence_offsets``)."""
    fenced = _closed_fence_offsets(text)
    return [m for m in _H2_ANY.finditer(text) if m.start() not in fenced]


def parse_plan_md(text: str) -> dict[str, Any]:
    title_match = _H1.search(text)
    title = title_match.group(1).strip() if title_match else ""

    phases: list[dict[str, Any]] = []
    headings = _h2_headings(text)
    for i, m in enumerate(headings):
        parsed = _phase_heading(m.group(1))
        if parsed is None:
            continue
        # Section ends at the next H2 of ANY kind (phase or not), so a non-phase
        # section's `- [ ]` lines can never leak into the preceding phase's tasks.
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        section = text[m.end() : end]
        phase_title, status, assertions = parsed
        phases.append(
            {
                "title": phase_title,
                "status": status,
                "tasks": [t.group(1).strip() for t in _TASK.finditer(section)],
                "assertions": assertions,
            }
        )

    return {"title": title, "phases": phases}


def _phase_children(repo: str, parent: int, gh: Any) -> dict[str, int]:
    """task-title -> issue-number for issues already linked under THIS phase, via the sub-issue
    API plus the checklist fallback. Scopes task reconciliation to the phase (the same task
    title in two phases is two distinct issues — review Finding 1) and is link-aware (a task
    created but not yet linked on a failed run is absent here, so the re-run links it)."""
    children: dict[str, int] = {}
    try:
        for c in gh.list_sub_issues(repo, parent):
            children[c["title"]] = c["number"]
    except RuntimeError:
        pass  # sub-issue API unavailable -> rely on the checklist below
    for num in _CHECKLIST.findall(gh.get_body(repo, parent) or ""):
        title = gh.issue_title(repo, int(num))
        if title:
            children[title] = int(num)
    return children


def generate(repo: str, plan: dict[str, Any], gh: Any) -> dict[str, Any]:
    # Ensure one label per distinct status
    seen_statuses: set[str] = set()
    for phase in plan["phases"]:
        status = phase["status"]
        if status not in seen_statuses:
            gh.ensure_label(repo, f"status:{status}")
            seen_statuses.add(status)

    # Idempotent (review): reuse an existing milestone/issues so a re-run of /conductor:start
    # (or a retry after a mid-way gh failure) never duplicates the hierarchy.
    milestone: int = gh.find_milestone(repo, plan["title"]) or gh.create_milestone(
        repo, plan["title"]
    )

    # Find-only pre-pass: resolve each EXISTING phase and the tasks already linked under it, and
    # collect every linked issue number. This makes task identity PHASE-scoped (a shared task
    # title across phases stays distinct — Finding 1) and lets an unlinked leftover from a failed
    # run be reused, not duplicated, without ever re-linking another phase's task (Finding 2).
    phase_state: list[tuple[int | None, dict[str, int]]] = []
    linked: set[int] = set()
    for phase in plan["phases"]:
        existing_phase = gh.find_issue(repo, phase["title"], milestone)
        if existing_phase:
            children = _phase_children(repo, existing_phase["number"], gh)
            phase_state.append((existing_phase["number"], children))
            linked.update(children.values())
        else:
            phase_state.append((None, {}))

    result_phases: list[dict[str, Any]] = []
    for phase, (phase_number, children) in zip(plan["phases"], phase_state):
        # The gate-link marker makes the phase->assertion mapping MACHINE-readable, so
        # reconcile --from-gate / phase-done derive test state instead of trusting flags.
        # Key ABSENT (hand-built dict, no assertion info) preserves any existing marker;
        # key present but EMPTY removes a stale one (codex #2 — the plan is the truth).
        raw_assertions = phase.get("assertions")
        tokens = [str(t) for t in (raw_assertions or [])]
        if phase_number is None:
            phase_number = gh.create_issue(
                repo,
                phase["title"],
                body=gate_link.assertions_marker(tokens) if tokens else "",
                milestone=milestone,
                labels=[f"status:{phase['status']}"],
            )["number"]
        elif tokens:
            new_body = gate_link.upsert_marker(gh.get_body(repo, phase_number), tokens)
            if (
                new_body is not None
            ):  # backfill or replace-stale; never rewrite unchanged
                gh.set_body(repo, phase_number, new_body)
        elif raw_assertions is not None:
            new_body = gate_link.remove_marker(gh.get_body(repo, phase_number))
            if new_body is not None:
                gh.set_body(repo, phase_number, new_body)
        sub_issues: list[int] = []
        fallback = False

        for task in phase["tasks"]:
            if task in children:  # already a child of THIS phase
                sub_issues.append(children[task])
                continue
            # Reuse a SPECIFIC unlinked leftover from a failed run, skipping any same-titled
            # issue already linked to another phase (review); else create fresh. Never duplicate.
            orphan = next(
                (
                    c
                    for c in gh.find_issues(repo, task, milestone)
                    if c["number"] not in linked
                ),
                None,
            )
            if orphan:
                task_number = orphan["number"]
                task_db_id = orphan["id"]
            else:
                task_issue = gh.create_issue(repo, task, body="", milestone=milestone)
                task_number = task_issue["number"]
                task_db_id = task_issue["id"]
            linked.add(task_number)
            sub_issues.append(task_number)

            try:
                gh.add_sub_issue(repo, phase_number, task_db_id)
            except RuntimeError:
                fallback = True
                existing = gh.get_body(repo, phase_number) or ""
                line = f"- [ ] #{task_number}"
                if (
                    line not in existing
                ):  # idempotent: don't duplicate the checklist line
                    new_body = (existing + "\n" + line).lstrip("\n")
                    gh.set_body(repo, phase_number, new_body)

        result_phases.append(
            {"number": phase_number, "sub_issues": sub_issues, "fallback": fallback}
        )

    return {"milestone": milestone, "phases": result_phases}


def convert(repo: str, plan_md_path: str, gh: Any) -> dict[str, Any]:
    with open(plan_md_path) as f:
        text = f.read()
    plan = parse_plan_md(text)
    return generate(repo, plan, gh)
