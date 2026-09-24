"""A stand-in `codex` for tests, answering from responses recorded off codex-cli 0.155.0.

`tests/conftest.py` refuses any test that reaches a real `codex`, so a test that exercises a
Codex code path puts this stub on `PATH` instead. It answers the three things conductor asks
Codex for:

* `codex --version`
* `codex plugin list --json` — `plugin_list`, default `{"installed": []}`
* `codex app-server`'s `skills/list` — `app_server`, a list of JSON-RPC response lines replayed in
  order, one per request carrying an `id`. `None` (the default) means the stub exits without
  answering, which is how Codex behaves when it cannot give a catalog: discovery then falls back
  to its filesystem scan. `hang=True` makes it never answer, for the time-bound paths.

`recorded_app_server()` and `recorded_plugin_list()` load the captured 0.155.0 responses in
`tests/conductor/fixtures/` with their placeholders filled in.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
RECORDED_VERSION = "codex-cli 0.155.0"
SKILLS_LIST_FIXTURE = FIXTURES / "codex-app-server-skills-list-0.155.0.jsonl"
PLUGIN_LIST_FIXTURE = FIXTURES / "codex-plugin-list-0.155.0.json"

_STUB = """#!{python}
import json, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print({version!r})
elif args[:3] == ["plugin", "list", "--json"]:
    sys.stdout.write({plugin_list!r})
elif args[:1] == ["app-server"]:
    if {hang!r}:
        import time
        time.sleep(60)
    replies = {app_server!r}
    if replies is None:
        sys.exit(0)
    for line in sys.stdin:
        if not replies:
            break
        if "id" in json.loads(line):
            sys.stdout.write(replies.pop(0).rstrip("\\n") + "\\n")
            sys.stdout.flush()
"""


def _fill(text: str, *, codex_home: str, cwd: str, home: str) -> str:
    return (
        text.replace("__CODEX_HOME__", codex_home)
        .replace("__CWD__", cwd)
        .replace("__HOME__", home)
    )


def recorded_app_server(*, codex_home: str, cwd: str, home: str) -> list[str]:
    """The recorded `initialize` and `skills/list` responses, one line each."""
    text = SKILLS_LIST_FIXTURE.read_text(encoding="utf-8")
    lines = _fill(text, codex_home=codex_home, cwd=cwd, home=home).splitlines()
    return [line for line in lines if "id" in json.loads(line)]


def recorded_plugin_list(*, codex_home: str) -> str:
    text = PLUGIN_LIST_FIXTURE.read_text(encoding="utf-8")
    return _fill(text, codex_home=codex_home, cwd="", home="")


def install(
    bindir: pathlib.Path,
    *,
    plugin_list: str = '{"installed": []}',
    app_server: list[str] | None = None,
    hang: bool = False,
) -> pathlib.Path:
    """Write the stub `codex` (and a `git` link, which host resolution shells out to)."""
    bindir.mkdir(parents=True, exist_ok=True)
    exe = bindir / "codex"
    exe.write_text(
        _STUB.format(
            python=shutil.which("python3") or "/usr/bin/python3",
            version=RECORDED_VERSION,
            plugin_list=plugin_list,
            app_server=app_server,
            hang=hang,
        ),
        encoding="utf-8",
    )
    os.chmod(exe, 0o755)
    git = shutil.which("git")
    if git and not (bindir / "git").exists():
        (bindir / "git").symlink_to(git)
    return exe


def put_on_path(monkeypatch, bindir: pathlib.Path, **kwargs) -> pathlib.Path:
    """`install` the stub and make it the only `codex` on `PATH`."""
    exe = install(bindir, **kwargs)
    monkeypatch.setenv("PATH", str(bindir))
    return exe
