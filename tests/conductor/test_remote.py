"""`conductor remote`: derive the repo's git remote, never assume `origin` (repos are often
`github`). Reuses merge_gate's URL-matching resolver so prose and gate agree."""

from conductor import remote


def test_resolve_uses_merge_gate_resolver(monkeypatch):
    monkeypatch.setattr(remote, "_resolve_repo", lambda: "owner/repo")
    monkeypatch.setattr(remote, "_remote_for", lambda repo: "github" if repo == "owner/repo" else "x")
    assert remote.resolve() == "github"


def test_main_prints_resolved_remote(monkeypatch, capsys):
    monkeypatch.setattr(remote, "resolve", lambda: "github")
    assert remote.main() == 0
    assert capsys.readouterr().out.strip() == "github"


def test_main_fails_open_to_origin(monkeypatch, capsys):
    """A discovery failure must degrade to the historical default, never emit an empty remote
    (which would make `git fetch "" main` fail)."""
    def boom():
        raise RuntimeError("no repo")

    monkeypatch.setattr(remote, "resolve", boom)
    assert remote.main() == 0
    out = capsys.readouterr()
    assert out.out.strip() == "origin"
    assert "fell back to origin" in out.err
