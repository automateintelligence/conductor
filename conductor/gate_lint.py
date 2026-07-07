"""`conductor gate lint` — frozen-gate quality + integrity (spec Phase 4, review B-4).

Fails closed on the MECHANICALLY-DETECTABLE weak-frozen-test patterns — the subset a
linter can catch without judgment:

- a manifest command that could load an unfrozen conftest or an autoloaded plugin
  (anything short of the exact pinned standalone form);
- an assertion test file with no negative ("must not contain" / assertNot-style) clause;
- an assertion test with a trivially-true assertion (`assert True`, `assert 1`, a bare
  non-empty literal) — a tautology that passes any implementation.

Boundary (deliberate): the judgment-requiring weaknesses — a hard-coded value tracking
no source of truth, a `property` tested on one case, an "X is used" check that only
proves X exists — belong to the red-team step in `/conductor:assertions-to-tests`,
not here. Prose for what needs a mind; mechanism for what a machine can see.

The pinned-command rule validates an EXACT argv shape, not token presence: optional
leading env assignments from a known-safe allowlist (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
required among them), then exactly `python3 -m pytest`, with `--noconftest` and
`-p no:cacheprovider` present, and every remaining token a pytest flag or a test path.
Token-presence matching would pass `<pinned form> && python3 evil.py` — the manifest
runner executes commands through a shell, so shell compounds/wrappers reopen the bypass.
"""

from __future__ import annotations

import ast
import glob
import os
import shlex
import sys

from conductor import freeze
from conductor.paths import project_root

# Env assignments a pinned command may carry. PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 is
# REQUIRED; the rest are harmless determinism knobs. Anything else is rejected.
ENV_ALLOWLIST = frozenset(
    {
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
        "PYTHONDONTWRITEBYTECODE=1",
        "PYTHONHASHSEED=0",
    }
)
REQUIRED_ENV = "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1"
RUNNER_ARGV = ("python3", "-m", "pytest")
REQUIRED_FLAG = "--noconftest"
REQUIRED_PLUGIN_DISABLE = "no:cacheprovider"

# The runner executes commands with shell=True: any of these in the RAW string can
# splice in a second command, a redirect, or a substitution. Reject outright.
_SHELL_META = ("`", "$(", ";", "|", "&", ">", "<", "\n")

# Fail-closed flag policy: a pytest flag is allowed only when its ARITY is known —
# a value-taking flag (`--ignore <path>`, `-cpytest.ini`, `-o addopts=...`) can
# consume what looks like the test path or reload configuration, so anything not
# recognized as a no-arg flag (or a safe `=`-form) is rejected as unpinned.
_NOARG_FLAGS = frozenset(
    {
        "-q",
        "-qq",
        "-v",
        "-vv",
        "-s",
        "-x",
        "--exitfirst",
        "--noconftest",
        "--strict-markers",
        "--strict-config",
        "--no-header",
        "--no-summary",
    }
)
_SAFE_EQ_PREFIXES = ("--tb=", "--maxfail=", "--timeout=", "--color=")

_SAFE_PATH_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/:-*?[]"
)


def _is_env_assignment(tok: str) -> bool:
    name, eq, _ = tok.partition("=")
    return bool(eq) and name.replace("_", "a").isalnum() and not name[:1].isdigit()


def _check_command(raw: str, repo_root: str) -> tuple[list[str], list[str]]:
    """Validate one manifest command against the exact pinned argv shape.

    Returns (findings, test_path_tokens). Findings are reasons; each caller-emitted
    line must contain the offending command verbatim.
    """

    def unpinned(why: str) -> tuple[list[str], list[str]]:
        return [f"unpinned command ({why}): {raw}"], []

    for meta in _SHELL_META:
        if meta in raw:
            return unpinned(f"shell metacharacter {meta!r}")
    try:
        tokens = shlex.split(raw)
    except ValueError:
        return [f"unpinned unparseable-command: {raw}"], []
    if not tokens:
        return unpinned("empty command")

    i = 0
    envs = []
    while i < len(tokens) and _is_env_assignment(tokens[i]):
        envs.append(tokens[i])
        i += 1
    for env_tok in envs:
        if env_tok not in ENV_ALLOWLIST:
            return unpinned(
                f"env assignment {env_tok!r} not in the known-safe allowlist"
            )
    if REQUIRED_ENV not in envs:
        return unpinned(f"missing {REQUIRED_ENV}")

    if tuple(tokens[i : i + 3]) != RUNNER_ARGV:
        return unpinned("runner is not exactly 'python3 -m pytest'")
    rest = tokens[i + 3 :]

    paths: list[str] = []
    saw_noconftest = False
    saw_cache_disable = False
    j = 0
    while j < len(rest):
        tok = rest[j]
        if tok == "-p":
            if j + 1 >= len(rest) or not rest[j + 1].startswith("no:"):
                return unpinned("-p may only disable plugins ('-p no:<plugin>')")
            if rest[j + 1] == REQUIRED_PLUGIN_DISABLE:
                saw_cache_disable = True
            j += 2
            continue
        if tok.startswith("-"):
            if tok == REQUIRED_FLAG:
                saw_noconftest = True
            elif tok in _NOARG_FLAGS or tok.startswith(_SAFE_EQ_PREFIXES):
                pass
            else:
                return unpinned(
                    f"flag {tok!r} is not a recognized no-arg pytest flag "
                    f"(unknown arity/config reload — fail-closed)"
                )
            j += 1
            continue
        # a non-flag token must be a test path (validated to exist by the file rules)
        if not tok or not set(tok) <= _SAFE_PATH_CHARS:
            return unpinned(f"token {tok!r} is neither a pytest flag nor a test path")
        base = tok.split("::", 1)[0]
        is_glob = any(c in base for c in "*?[")
        as_dir = base if os.path.isabs(base) else os.path.join(repo_root, base)
        if not (base.endswith(".py") or is_glob or os.path.isdir(as_dir)):
            return unpinned(f"token {tok!r} is neither a pytest flag nor a test path")
        paths.append(tok)
        j += 1

    if not saw_noconftest:
        return unpinned(f"missing {REQUIRED_FLAG}")
    if not saw_cache_disable:
        return unpinned(f"missing -p {REQUIRED_PLUGIN_DISABLE}")
    if not paths:
        return unpinned("no test path")
    return [], paths


def _resolve_test_files(
    path_tokens: list[str], repo_root: str
) -> tuple[list[str], list[str]]:
    """Resolve command path tokens to concrete .py files (the same token->path stance
    as freeze._referenced_files). A referenced file that does not exist is a finding
    (fail-closed). Returns (findings, files)."""
    findings: list[str] = []
    files: list[str] = []
    for tok in path_tokens:
        base = tok.split("::", 1)[0]
        path = base if os.path.isabs(base) else os.path.join(repo_root, base)
        if os.path.isfile(path):
            files.append(path)
        elif os.path.isdir(path):
            files.extend(freeze._collect_test_files(path))
        elif any(c in base for c in "*?["):
            matches = [
                fp
                for fp in glob.glob(path, recursive=True)
                if os.path.isfile(fp) and fp.endswith(".py")
            ]
            if not matches:
                findings.append(f"missing test file (glob matched nothing): {tok}")
            files.extend(matches)
        else:
            findings.append(f"missing test file: {tok}")
    return findings, files


def _is_negative_assert(node: ast.Assert) -> bool:
    for sub in ast.walk(node.test):
        if isinstance(sub, ast.Compare) and any(
            isinstance(op, (ast.NotIn, ast.NotEq, ast.IsNot)) for op in sub.ops
        ):
            return True
        if isinstance(sub, ast.UnaryOp) and isinstance(sub.op, ast.Not):
            return True
    return False


def _has_negative_clause(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and _is_negative_assert(node):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if str(name).lower().startswith("assertnot"):
                return True
    return False


def _static_truth(node: ast.expr) -> bool | None:
    """Statically-known truthiness of a literal expression; None = unknown.
    Covers constants, container literals, and `not <literal>` chains — so
    `assert not False` / `assert not []` count as tautologies too."""
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return bool(node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _static_truth(node.operand)
        return None if inner is None else not inner
    return None


def _is_trivially_true(test: ast.expr) -> bool:
    """A bare statically-true assertion — a tautology that passes any product."""
    return _static_truth(test) is True


def _trivial_assert_lines(tree: ast.AST) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Assert) and _is_trivially_true(node.test)
    ]


def _lint_test_file(path: str, repo_root: str) -> list[str]:
    """The two independent AST rules (missing-negative A7, trivially-true A16), plus
    fail-closed rejection of an unreadable/unparseable file."""
    rel = os.path.relpath(path, repo_root)
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except SyntaxError as exc:
        return [f"unparseable test file (SyntaxError): {rel}: line {exc.lineno}"]
    except Exception as exc:
        return [f"unreadable test file: {rel}: {exc}"]
    findings: list[str] = []
    base = os.path.basename(path)
    if base != "conftest.py":  # a conftest is support code, not an assertion test
        if not _has_negative_clause(tree):
            findings.append(f"{rel}: no negative assertion clause")
        for lineno in _trivial_assert_lines(tree):
            findings.append(f"{rel}: trivially-true assertion at line {lineno}")
    return findings


def lint(manifest_path: str, repo_root: str) -> list[str]:
    """Lint every manifest entry; return findings (empty = clean). Fail-closed: an
    unloadable manifest is itself a finding, never a silent pass."""
    try:
        entries = freeze._load(manifest_path)
    except Exception as exc:
        return [f"manifest-unloadable: {exc}"]
    findings: list[str] = []
    linted: dict[str, list[str]] = {}
    for entry in entries:
        aid = str(entry.get("id", "?"))
        raw = str(entry.get("command", "") or "")
        cmd_findings, path_tokens = _check_command(raw, repo_root)
        findings.extend(f"{aid}: {f}" for f in cmd_findings)
        file_findings, files = _resolve_test_files(path_tokens, repo_root)
        findings.extend(f"{aid}: {f}" for f in file_findings)
        for fp in files:
            if fp not in linted:
                linted[fp] = _lint_test_file(fp, repo_root)
            findings.extend(f"{aid}: {f}" for f in linted[fp])
    return list(dict.fromkeys(findings))


def main(argv: list | None = None) -> int:
    repo_root = project_root()
    manifest = os.path.join(repo_root, "assertions", "manifest.yaml")
    findings = lint(manifest, repo_root)
    if findings:
        for f in findings:
            print(f"[LINT] {f}", file=sys.stderr)
        print(f"[LINT] FAIL: {len(findings)} finding(s)", file=sys.stderr)
        return 1
    print("[LINT] ok: manifest commands pinned; test files pass the mechanical rules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
