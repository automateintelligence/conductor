"""A-DH-3 — the Codex worker launch resolves using only artifacts Conductor installs (contract).

Source: docs/superpowers/specs/2026-08-10-codex-dual-host-conductor-design.assertions.md §3.

Claim: every artifact the Codex worker launch depends on in order to resolve Conductor's
autodev skill was written by Conductor itself, so the launch does not depend on any
pre-existing third-party convention.

The quietest failure in the spec. ``$conductor:autodev`` is a prompting convention a dispatch
table in ``~/.codex/AGENTS.md`` supplies, and that file is a third-party install — so a launch
that depends on it works on the author's machine and silently does nothing on a stock Codex.
The scratch ``HOME`` and ``CODEX_HOME`` here therefore start EMPTY and are snapshotted before
the fire, so anything the launch resolves through is provably Conductor's own artifact.

DELIBERATELY MECHANISM-NEUTRAL. The ground-truth review *recommends* emitting an explicit
``SKILL.md`` path instead of a bare ``$conductor:autodev`` token, but records that as a
recommendation the plan writer owns, not a decision. Either form passes here: a path named in
the prompt, or a dispatch/convention file Conductor installed that the prompt's token resolves
through. What fails is depending on something Conductor did not install.

The package root is at a path chosen at test time and the whole fire is repeated at a SECOND
test-chosen path. A resolution artifact that does not follow the relocation is a hardcoded
constant that happens to exist on this machine.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conductor.hosts import runhost  # noqa: E402  (needs ROOT on sys.path)

RUN_KEY = "2026-08-10-codex-dual-host-conductor-design-5f6520fc"
SKILL = "autodev"

#: What identifies Conductor's autodev skill. Read out of SKILL.md FRONTMATTER, never off the
#: filename: a launch pointing at any file that happens to be called SKILL.md would otherwise
#: pass while resolving to a stranger's skill.
FRONTMATTER_NAME = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)

_FAKE_CODEX = """#!/usr/bin/env python3
import json, os, sys
record = {{"argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ)}}
with open({log!r}, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\\n")
"""


class Fire:
    def __init__(self) -> None:
        self.package = pathlib.Path()
        self.home = pathlib.Path()
        self.codex_home = pathlib.Path()
        self.before: set[pathlib.Path] = set()
        self.after: set[pathlib.Path] = set()
        self.prompt = ""
        self.argv: list[str] = []

    @property
    def written_by_conductor(self) -> set[pathlib.Path]:
        """Files that appeared under the scratch HOME/CODEX_HOME during the fire."""
        return self.after - self.before

    def is_conductors(self, path: pathlib.Path) -> bool:
        """Conductor shipped it (under the package root) or wrote it during the fire."""
        return path in self.written_by_conductor or self.package in path.parents


def _snapshot(*roots: pathlib.Path) -> set[pathlib.Path]:
    return {p.resolve() for root in roots for p in root.rglob("*") if p.is_file()}


def _seed_package_root(root: pathlib.Path) -> None:
    """Conductor's own shipped tree, copied to a path chosen at test time."""
    shutil.copytree(ROOT / "skills", root / "skills")
    (root / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / "bin" / "conductor", root / "bin" / "conductor")
    (root / "bin" / "conductor").chmod(0o755)


def _fire(label: str) -> Fire:
    fire = Fire()
    workdir = pathlib.Path(tempfile.mkdtemp(prefix=f"a-dh-3-{label}-"))
    fire.package = (workdir / "package").resolve()
    fire.home = (workdir / "home").resolve()
    fire.codex_home = (workdir / "codex-home").resolve()
    project = workdir / "project"
    for directory in (fire.home, fire.codex_home, project):
        directory.mkdir(parents=True)
    _seed_package_root(fire.package)
    fire.before = _snapshot(fire.home, fire.codex_home)

    bindir = workdir / "bin"
    bindir.mkdir()
    log = workdir / "codex.jsonl"
    log.write_text("", encoding="utf-8")
    fake = bindir / "codex"
    fake.write_text(_FAKE_CODEX.format(log=str(log)), encoding="utf-8")
    fake.chmod(0o755)

    environ = {
        key: value
        for key, value in os.environ.items()
        if key not in {runhost.HOST_ENV, "CONDUCTOR_HOME"}
    }
    environ["PATH"] = f"{bindir}{os.pathsep}{environ.get('PATH', '')}"
    environ["HOME"] = str(fire.home)
    environ["CODEX_HOME"] = str(fire.codex_home)
    environ["CODEX_PLUGIN_ROOT"] = str(fire.package)

    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(environ)
    try:
        runhost.record(str(project), "codex")
        adapter = runhost.adapter(str(project))
        argv = adapter.worker_argv(
            state_root=str(project / ".conductor"),
            run_key=RUN_KEY,
            project_root=str(project),
            posture="supervised",
        )
        worker_env = adapter.worker_env(
            state_root=str(project / ".conductor"),
            run_key=RUN_KEY,
            project_root=str(project),
        )
    finally:
        os.environ.clear()
        os.environ.update(previous)

    subprocess.run(
        argv,
        cwd=str(project),
        env={**environ, **worker_env},
        stdin=subprocess.DEVNULL,
        timeout=60,
        check=False,
    )
    records = [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert len(records) == 1, f"expected one codex launch, recorded {len(records)}"
    fire.argv = records[0]["argv"]
    # The prompt is whatever the launch put in front of the model: the trailing positional.
    fire.prompt = fire.argv[-1]
    fire.after = _snapshot(fire.home, fire.codex_home)
    return fire


_FIRES: dict[str, Fire] = {}


def fire_at(label: str) -> Fire:
    if label not in _FIRES:
        _FIRES[label] = _fire(label)
    return _FIRES[label]


def _path_tokens(prompt: str) -> list[pathlib.Path]:
    """Absolute filesystem paths named anywhere in the prompt."""
    return [
        pathlib.Path(match.rstrip(".,;:)\"'"))
        for match in re.findall(r"/[^\s\"']+", prompt)
    ]


def _skill_name(path: pathlib.Path) -> str | None:
    """The skill a file declares about itself, from SKILL.md frontmatter."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    match = FRONTMATTER_NAME.search(text.split("---", 2)[1])
    return match.group(1) if match else None


def _resolution_artifacts(fire: Fire) -> list[pathlib.Path]:
    """Every artifact the prompt resolves the autodev skill THROUGH.

    Branch one: a path named in the prompt that declares itself autodev in frontmatter.
    Branch two (the convention form): a file Conductor installed under the scratch roots that
    names the prompt's dispatch token and points at such a file. Both are accepted — this
    assertion does not settle which mechanism the plan writer chooses.
    """
    direct = [
        path
        for path in _path_tokens(fire.prompt)
        if path.is_file() and _skill_name(path) == SKILL
    ]
    if direct:
        return direct
    tokens = re.findall(r"\$[A-Za-z][\w:-]*", fire.prompt)
    through = []
    for installed in sorted(fire.written_by_conductor):
        try:
            text = installed.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(token.lstrip("$") in text for token in tokens):
            through.extend(
                path
                for path in _path_tokens(text)
                if path.is_file() and _skill_name(path) == SKILL
            )
    return through


def test_the_scratch_host_roots_start_empty() -> None:
    """Setup guarantee, asserted rather than assumed: with no AGENTS.md, no skills/ and no
    dispatch convention present before the fire, nothing the launch resolves through can be a
    pre-seeded third-party file."""
    for label in ("first", "second"):
        fire = fire_at(label)
        assert fire.before == set(), sorted(fire.before)


@pytest.mark.parametrize("label", ["first", "second"])
def test_the_launch_resolves_through_a_conductor_artifact(label: str) -> None:
    """Must-contain: a resolution artifact that exists, is readable, is Conductor's own, and
    identifies the autodev skill by frontmatter rather than by filename."""
    fire = fire_at(label)
    artifacts = _resolution_artifacts(fire)
    assert artifacts, (
        "the Codex launch names no artifact that resolves to Conductor's autodev skill.\n"
        f"prompt={fire.prompt!r}\npackage_root={fire.package}\n"
        f"files Conductor wrote under HOME/CODEX_HOME: {sorted(fire.written_by_conductor)}"
    )
    for artifact in artifacts:
        assert os.access(artifact, os.R_OK), artifact
        assert fire.is_conductors(artifact), (
            f"{artifact} is neither under the package root {fire.package} nor written by "
            "Conductor during the fire — the launch depends on a foreign artifact"
        )


@pytest.mark.parametrize("label", ["first", "second"])
def test_no_prompt_dependency_lives_in_a_file_conductor_did_not_write(
    label: str,
) -> None:
    """Must-not-contain: any dependency on a file under the scratch HOME/CODEX_HOME that
    Conductor did not write."""
    fire = fire_at(label)
    scratch = (fire.home, fire.codex_home)
    foreign = [
        path
        for path in _path_tokens(fire.prompt)
        if any(root in path.parents for root in scratch)
        and not fire.is_conductors(path)
    ]
    assert not foreign, f"prompt depends on non-Conductor artifacts: {foreign}"


def test_the_resolution_artifact_follows_the_package_root() -> None:
    """Two test-chosen package roots, two fires. An artifact that does not move with the
    package root is a hardcoded constant that happens to exist on this machine."""
    first, second = fire_at("first"), fire_at("second")
    assert first.package != second.package
    first_artifacts = _resolution_artifacts(first)
    second_artifacts = _resolution_artifacts(second)
    assert first_artifacts and second_artifacts, (first_artifacts, second_artifacts)
    assert set(first_artifacts).isdisjoint(second_artifacts), (
        "the same artifact path resolved from two different package roots: "
        f"{first_artifacts} vs {second_artifacts}"
    )
    for artifact in second_artifacts:
        assert second.is_conductors(artifact), artifact
