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
