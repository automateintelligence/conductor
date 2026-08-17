import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_plugin_manifest_schema_and_dependency():
    data = json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))
    assert data.get("name") == "conductor"
    assert re.match(r"^\d+\.\d+\.\d+$", data.get("version", "")), (
        "semver version required"
    )
    assert "spec-craft" in data.get("dependencies", []), "must depend on spec-craft"
    assert set(data) <= {
        "name",
        "version",
        "description",
        "author",
        "dependencies",
        "displayName",
        "homepage",
        "repository",
        "license",
    }
    assert isinstance(data["author"], dict), (
        "author must be an object, not a string (claude plugin validate --strict)"
    )


def _claude_manifest():
    return json.load(open(os.path.join(ROOT, ".claude-plugin", "plugin.json")))


def _codex_manifest():
    return json.load(open(os.path.join(ROOT, ".codex-plugin", "plugin.json")))


# Fields accepted by codex-cli 0.147.0's plugin manifest reader, observed across
# all 180 manifests in the installed `openai-curated` catalog. Codex does not
# reject unknown keys -- an unsupported field is silently ignored -- so this set
# is the only thing standing between us and a manifest that claims something the
# host never reads.
CODEX_MANIFEST_FIELDS = {
    "name",
    "version",
    "description",
    "author",
    "keywords",
    "interface",
    "repository",
    "license",
    "homepage",
    "apps",
    "skills",
    "mcpServers",
}


def test_codex_plugin_manifest_schema():
    data = _codex_manifest()
    assert data.get("name") == "conductor"
    assert re.match(r"^\d+\.\d+\.\d+$", data.get("version", "")), (
        "semver version required"
    )
    assert isinstance(data.get("author"), dict), "author must be an object"
    unknown = set(data) - CODEX_MANIFEST_FIELDS
    assert not unknown, f"codex ignores unknown manifest fields; drop {sorted(unknown)}"


def test_codex_manifest_does_not_claim_dependencies():
    # `dependencies` is a Claude plugin field. codex-cli 0.147.0 has no
    # counterpart and silently ignores it, so declaring it here would assert a
    # spec-craft dependency that no Codex install will ever enforce.
    assert "dependencies" not in _codex_manifest()


def test_codex_and_claude_manifests_do_not_drift():
    claude = _claude_manifest()
    codex = _codex_manifest()
    for field in ("name", "version", "description"):
        assert codex.get(field) == claude.get(field), (
            f"{field} differs between .claude-plugin and .codex-plugin manifests; "
            "a version split ships a plugin one host cannot update"
        )


def test_codex_manifest_skills_pointer_resolves():
    skills = _codex_manifest().get("skills")
    assert skills, "codex manifest must point at the skill directory"
    skills_dir = os.path.normpath(os.path.join(ROOT, skills))
    assert os.path.isdir(skills_dir), f"{skills} is not a directory"
    found = [
        name
        for name in os.listdir(skills_dir)
        if os.path.isfile(os.path.join(skills_dir, name, "SKILL.md"))
    ]
    assert found, f"no <name>/SKILL.md under {skills}"
