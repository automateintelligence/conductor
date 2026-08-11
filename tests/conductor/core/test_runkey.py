"""The deterministic run key (design §"Project and run identity"):

    <spec-slug>-<short-hash-of-normalized-repository-relative-spec-path>[-g<N>]

The relative-path hash is what stops two specs with the same filename in different directories
from colliding, and what keeps the key stable when the repository or worktree moves. Generation 1
omits the suffix; generation 2 and later carry -g2, -g3, ... so branches, worktrees, gate
directories, and run directories are generation-distinct."""

from __future__ import annotations

import os
import platform

import pytest

from conductor import paths
from conductor.core import runkey


def test_key_is_the_slug_plus_an_eight_character_path_hash():
    rel = "docs/specs/2026-08-10-codex-dual-host-conductor-design.md"
    key = runkey.run_key(rel)
    slug = paths.spec_slug(rel)
    assert key.startswith(f"{slug}-")
    assert len(key) == len(slug) + 1 + runkey.HASH_LEN
    assert runkey.HASH_LEN == 8


def test_key_is_deterministic():
    rel = "docs/specs/alpha.md"
    assert runkey.run_key(rel) == runkey.run_key(rel)


def test_same_filename_in_different_directories_does_not_collide():
    assert runkey.run_key("docs/specs/alpha.md") != runkey.run_key(
        "other/specs/alpha.md"
    )


def test_generation_one_omits_the_suffix_and_later_generations_carry_it():
    rel = "docs/specs/alpha.md"
    base = runkey.run_key(rel)
    assert runkey.run_key(rel, 1) == base
    assert runkey.run_key(rel, 2) == f"{base}-g2"
    assert runkey.run_key(rel, 11) == f"{base}-g11"


def test_parse_generation_round_trips():
    rel = "docs/specs/alpha.md"
    for generation in (1, 2, 3, 17):
        assert runkey.parse_generation(runkey.run_key(rel, generation)) == generation


def test_generation_below_one_is_refused():
    with pytest.raises(ValueError):
        runkey.run_key("docs/specs/alpha.md", 0)


def test_normalize_is_repository_relative_and_survives_relocation(tmp_path):
    first = tmp_path / "checkout-a"
    second = tmp_path / "checkout-b"
    for root in (first, second):
        (root / "docs" / "specs").mkdir(parents=True)
        (root / "docs" / "specs" / "alpha.md").write_text("# alpha\n")
    rel_a = runkey.normalize_spec_path(str(first), str(first / "docs/specs/alpha.md"))
    rel_b = runkey.normalize_spec_path(str(second), "docs/specs/alpha.md")
    assert rel_a == rel_b == "docs/specs/alpha.md"
    assert runkey.run_key(rel_a) == runkey.run_key(rel_b)


def test_normalize_collapses_redundant_path_segments(tmp_path):
    root = tmp_path / "repo"
    (root / "docs" / "specs").mkdir(parents=True)
    assert (
        runkey.normalize_spec_path(str(root), "./docs/../docs/specs/alpha.md")
        == "docs/specs/alpha.md"
    )


def test_normalize_refuses_a_path_outside_the_repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(ValueError) as excinfo:
        runkey.normalize_spec_path(str(root), "../elsewhere/alpha.md")
    assert "outside the repository" in str(excinfo.value)


def test_is_safe_run_key_accepts_generated_keys_and_rejects_traversal():
    assert runkey.is_safe_run_key(runkey.run_key("docs/specs/alpha.md", 3))
    assert not runkey.is_safe_run_key("../outside")
    assert not runkey.is_safe_run_key("a/b")
    assert not runkey.is_safe_run_key("")
    assert not runkey.is_safe_run_key("-leading")
    assert not runkey.is_safe_run_key("alpha.lock")


@pytest.mark.skipif(
    platform.system() == "Windows",
    reason="symlink creation may require admin privileges on Windows",
)
def test_normalize_rescues_repository_accessed_through_symlink_alias(tmp_path):
    """If the repository is reached through a symlinked alias (e.g., /home/user/repo -> /data/actual-repo),
    an absolute spec path built through that alias should still normalize correctly."""
    actual = tmp_path / "actual"
    alias = tmp_path / "alias"
    (actual / "docs" / "specs").mkdir(parents=True)
    (actual / "docs" / "specs" / "alpha.md").write_text("# alpha\n")
    os.symlink(actual, alias)

    # Normalize using the alias path with an absolute spec path through the alias.
    rel_via_alias = runkey.normalize_spec_path(
        str(alias), str(alias / "docs" / "specs" / "alpha.md")
    )
    # Normalize using the real path for comparison.
    rel_via_actual = runkey.normalize_spec_path(
        str(actual), str(actual / "docs" / "specs" / "alpha.md")
    )

    # Both should produce the same normalized path and the same run key.
    assert rel_via_alias == rel_via_actual == "docs/specs/alpha.md"
    assert runkey.run_key(rel_via_alias) == runkey.run_key(rel_via_actual)


@pytest.mark.skipif(
    platform.system() == "Windows",
    reason="symlink creation may require admin privileges on Windows",
)
def test_normalize_refuses_an_in_repo_symlink_that_points_outside(tmp_path):
    """The reverse of the alias case, and the one deciding containment lexically leaves open: the
    spec path is lexically inside the repository, so a check that only resolves AFTER a refusal
    never resolves this at all. The link would key a run on content no other checkout has."""
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    (repo / "docs" / "specs").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.md").write_text("# not in the repo\n")
    os.symlink(outside / "secret.md", repo / "docs" / "specs" / "alpha.md")

    with pytest.raises(ValueError) as excinfo:
        runkey.normalize_spec_path(str(repo), "docs/specs/alpha.md")
    assert "outside the repository" in str(excinfo.value)


@pytest.mark.skipif(
    platform.system() == "Windows",
    reason="symlink creation may require admin privileges on Windows",
)
def test_normalize_keeps_the_link_path_for_a_symlink_inside_the_repository(tmp_path):
    """Containment is decided on the resolved path, but the KEY still comes from the link path, so
    a spec symlinked to another file in the same repository keeps its own run key rather than
    silently adopting the target's."""
    repo = tmp_path / "repo"
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "docs" / "specs" / "target.md").write_text("# target\n")
    os.symlink(
        repo / "docs" / "specs" / "target.md", repo / "docs" / "specs" / "alias.md"
    )

    assert (
        runkey.normalize_spec_path(str(repo), "docs/specs/alias.md")
        == "docs/specs/alias.md"
    )


def test_normalize_still_refuses_genuinely_outside_paths_after_symlink_retry(tmp_path):
    """Even after the symlink rescue retry, paths genuinely outside the repository should still raise."""
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    (outside / "alpha.md").mkdir(parents=True)

    with pytest.raises(ValueError) as excinfo:
        runkey.normalize_spec_path(str(repo), str(outside / "alpha.md"))
    assert "outside the repository" in str(excinfo.value)
