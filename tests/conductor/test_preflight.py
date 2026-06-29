from conductor import preflight

_ALL = {
    "spec-craft:expectations",
    "spec-craft:executable-assertions",
    "conductor:assertions-to-tests",
    "superpowers:subagent-driven-development",
    "superpowers:requesting-code-review",
    "superpowers:receiving-code-review",
    "superpowers:writing-plans",
    "gstack:code-review",
    "gstack:codex",
    "gstack:document-release",
}


def test_missing_command_fails_closed():
    out = preflight.check(
        available={"spec-craft:expectations", "superpowers:writing-plans"}
    )
    assert not out["ok"] and "/codex" in out["missing"]


def test_all_present_ok():
    assert preflight.check(available=_ALL)[
        "ok"
    ]  # bare /code-review matches gstack:code-review
