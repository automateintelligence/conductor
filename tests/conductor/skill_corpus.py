"""A small generated corpus of SKILL.md frontmatter for the namer's subset property.

Every combination of a `name` line, a `description` line and one extra line, drawn from values
inside and outside the subset `conductor.hosts.codex.codex_skill_names` reads. Deterministic, so
the hermetic suite and the opt-in live Codex test see the same files.
"""

from __future__ import annotations

import itertools

NAMES = (
    "name: g{i}",
    'name: "g{i}"',
    "name: 'g{i}'",
    "name: g{i} # c",
    "name: &a g{i}",
    "name: true",
    'name:\u00a0"g{i}"',
    None,
)
DESCRIPTIONS = (
    "description: d",
    'description: "a: b"',
    "description: 'it''s'",
    "description: a: b",
    "description: [x]",
    "description: [",
    "description: null",
    "description: ~",
    "description: 42",
    "description: x # y",
    'description: "esc\\n"',
    "description: |",
    "description:  d",
    "description: d\u001f",
    "description: a\u00a0b",
    "description: a\u2028b",
    "description: a\u0085b",
    "description: a\u001cb",
    "description: a\tb",
    "description: a\rb",
    "description: a\u200bb",
    "description: a\u3000b",
    "description: naïve — «ok» ✓ 日本",
)
EXTRAS = (
    None,
    "license: MIT",
    "metadata: null",
    "- item",
    "foo: *missing",
    "",
    "name: dup{i}",
    "  continued",
    "\u0001",
)


def corpus() -> dict[str, str]:
    """``{directory name: SKILL.md text}``."""
    files = {}
    combos = itertools.product(NAMES, DESCRIPTIONS, EXTRAS)
    for i, (name, description, extra) in enumerate(combos):
        lines = [
            line.format(i=i) for line in (name, description, extra) if line is not None
        ]
        files[f"c{i:04d}"] = "---\n" + "\n".join(lines) + "\n---\nbody\n"
    return files
