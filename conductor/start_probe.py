def manifest_ids(manifest_path: str) -> list[str]:
    """Reuse the runner's loader/parser so manifest parsing stays single-sourced."""
    from assertions import run as runner

    return [str(a["id"]) for a in runner.load_assertions(manifest_path)]


def assertions_ready(
    expected_ids: list[str], manifest_path: str, runner_exit: int
) -> bool:
    """§3 step-3 idempotency probe (Codex #3): the done-gate is fully built iff the manifest
    covers EVERY expected assertion id AND `conductor assert run --level spec` returned a
    DETERMINATE result (0 or 1) — never 2/3/4/5 (missing/unparseable/timeout/no-match)."""
    try:
        present = set(manifest_ids(manifest_path))
    except Exception:
        return False
    return bool(expected_ids) and set(expected_ids) <= present and runner_exit in (0, 1)
