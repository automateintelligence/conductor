import json
import os
import re

import pytest

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


# Fields codex-cli 0.147.0 actually READS, taken from the legacy manifest
# parser's `RawPluginManifest` struct in codex-rs/core-plugins/src/manifest.rs
# (lines 45-68, commit be6e8eac029b183056b7e4402879f15d2c85f61b). The struct
# carries `#[serde(rename_all = "camelCase")]`, so its `mcp_servers` field is
# read from JSON as `mcpServers`. The destructuring in
# `resolve_raw_plugin_manifest` (same file, lines 295-305) is exhaustive, which
# makes this the complete set.
#
# Verified against a scratch CODEX_HOME install of this plugin: a wrong-typed
# value for a field in this set fails the install with "missing or invalid
# plugin.json", while a wrong-typed value for a field outside it installs
# cleanly -- `RawPluginManifest` has no `deny_unknown_fields`, so anything not
# listed here is silently discarded by the host.
CODEX_MANIFEST_FIELDS = {
    "name",
    "version",
    "description",
    "keywords",
    "skills",
    "mcpServers",
    "apps",
    "hooks",
    "interface",
}

# Fields we keep in .codex-plugin/plugin.json only to mirror
# .claude-plugin/plugin.json. Codex discards these. They are listed separately
# from CODEX_MANIFEST_FIELDS so the schema test records them as inert rather
# than claiming the host honours them.
CODEX_INERT_MIRROR_FIELDS = {"author"}


def _unsupported_codex_fields(manifest):
    """Manifest keys codex neither reads nor that we knowingly carry as inert."""
    return set(manifest) - CODEX_MANIFEST_FIELDS - CODEX_INERT_MIRROR_FIELDS


def test_codex_plugin_manifest_schema():
    data = _codex_manifest()
    assert data.get("name") == "conductor"
    assert re.match(r"^\d+\.\d+\.\d+$", data.get("version", "")), (
        "semver version required"
    )
    assert isinstance(data.get("author"), dict), "author must be an object"
    unknown = _unsupported_codex_fields(data)
    assert not unknown, f"codex ignores unknown manifest fields; drop {sorted(unknown)}"


def test_codex_schema_accepts_hooks_field():
    # codex reads `hooks` (manifest.rs line 65) and accepts both the string and
    # the array form. An earlier whitelist omitted it, so this suite rejected a
    # manifest the host installs without complaint.
    for hooks in ("./hooks.json", ["./hooks.json"]):
        manifest = {"name": "conductor", "version": "0.9.3", "hooks": hooks}
        assert _unsupported_codex_fields(manifest) == set(), (
            f"hooks={hooks!r} is valid for codex but this suite rejected it"
        )


def test_codex_schema_does_not_claim_codex_reads_claude_only_fields():
    # These four are absent from `RawPluginManifest`; codex silently discards
    # them. Whitelisting them as read-by-codex is what this test guards against.
    for field in ("author", "repository", "license", "homepage"):
        assert field not in CODEX_MANIFEST_FIELDS, (
            f"codex-cli 0.147.0 does not read {field!r}; it must not be listed "
            "as a field the host honours"
        )


def _codex_skills_pointers(manifest):
    """The `skills` entry as a list of pointers.

    codex accepts either a single string or an array of strings
    (RawPluginManifestPaths, manifest.rs lines 129-135). Anything else is the
    enum's `Invalid` arm and yields no pointers.
    """
    skills = manifest.get("skills")
    if isinstance(skills, str):
        return [skills]
    if isinstance(skills, list):
        return [entry for entry in skills if isinstance(entry, str)]
    return []


def _assert_skills_pointers_resolve(pointers):
    assert pointers, "codex manifest must point at the skill directory"
    for pointer in pointers:
        skills_dir = os.path.normpath(os.path.join(ROOT, pointer))
        assert os.path.isdir(skills_dir), f"{pointer} is not a directory"
        found = [
            name
            for name in os.listdir(skills_dir)
            if os.path.isfile(os.path.join(skills_dir, name, "SKILL.md"))
        ]
        assert found, f"no <name>/SKILL.md under {pointer}"


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
    _assert_skills_pointers_resolve(_codex_skills_pointers(_codex_manifest()))


@pytest.mark.parametrize(
    "skills_value",
    ["./skills/", ["./skills/"]],
    ids=["string-form", "array-form"],
)
def test_codex_manifest_skills_pointer_forms_resolve(skills_value):
    # codex reads `skills` as RawPluginManifestPaths, an untagged enum of
    # Path(String) | Paths(Vec<String>) (manifest.rs lines 129-135), so both
    # forms are valid manifests. Confirmed by installing each form into a
    # scratch CODEX_HOME. The array form used to crash this check with
    # "TypeError: join() argument must be str ... not 'list'".
    _assert_skills_pointers_resolve(_codex_skills_pointers({"skills": skills_value}))
