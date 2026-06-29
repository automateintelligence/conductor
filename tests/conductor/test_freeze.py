"""Done-gate freeze guard (§5 integrity).

record() snapshots each assertion's manifest entry + the test files its command references.
verify() fails closed if a frozen assertion was weakened or removed, but ALLOWS new
assertions (legit gap-closing). Referenced product code (imported, not named in the command)
is NOT frozen, so the worker can still implement it.
"""

import os

from conductor import freeze

_MANIFEST = """\
assertions:
  - id: a
    claim: "a holds"
    command: "{cmd}"
    setup: ""
    timeout: 30
    level: spec
    kind: example
"""


def _setup(
    tmp_path,
    cmd="python3 -m pytest -q sub/test_a.py",
    body="def test_a():\n    assert True\n",
):
    (tmp_path / "sub").mkdir(exist_ok=True)
    (tmp_path / "sub" / "test_a.py").write_text(body)
    (tmp_path / "sub" / "a.py").write_text(
        "def a():\n    return 1\n"
    )  # product code (not frozen)
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(_MANIFEST.format(cmd=cmd))
    return str(manifest), str(tmp_path / ".frozen")


def test_verify_no_baseline_is_not_enforced(tmp_path):
    manifest, baseline = _setup(tmp_path)
    res = freeze.verify(manifest, baseline, str(tmp_path))
    assert res["ok"] is True and res["frozen"] is False


def test_record_then_verify_unchanged_ok(tmp_path):
    manifest, baseline = _setup(tmp_path)
    freeze.record(manifest, baseline, str(tmp_path))
    assert os.path.exists(baseline)
    res = freeze.verify(manifest, baseline, str(tmp_path))
    assert res["ok"] is True and res["frozen"] is True and res["tampered"] == []


def test_editing_product_code_is_allowed(tmp_path):
    # a.py is imported by the test but NOT named in the command -> the worker may change it.
    manifest, baseline = _setup(tmp_path)
    freeze.record(manifest, baseline, str(tmp_path))
    (tmp_path / "sub" / "a.py").write_text("def a():\n    return 42\n")
    res = freeze.verify(manifest, baseline, str(tmp_path))
    assert res["ok"] is True and res["tampered"] == []


def test_detects_weakened_command(tmp_path):
    manifest, baseline = _setup(tmp_path)
    freeze.record(manifest, baseline, str(tmp_path))
    (tmp_path / "manifest.yaml").write_text(
        _MANIFEST.format(cmd="true")
    )  # gut the check
    res = freeze.verify(manifest, baseline, str(tmp_path))
    assert res["ok"] is False and any(
        "a:" in t and "entry" in t for t in res["tampered"]
    )


def test_detects_edited_test_file(tmp_path):
    manifest, baseline = _setup(tmp_path)
    freeze.record(manifest, baseline, str(tmp_path))
    (tmp_path / "sub" / "test_a.py").write_text(
        "def test_a():\n    assert True  # gutted\n"
    )
    res = freeze.verify(manifest, baseline, str(tmp_path))
    assert res["ok"] is False and any("test-file-changed" in t for t in res["tampered"])


def test_detects_removed_assertion(tmp_path):
    manifest, baseline = _setup(tmp_path)
    freeze.record(manifest, baseline, str(tmp_path))
    (tmp_path / "manifest.yaml").write_text(
        'assertions:\n  - id: z\n    command: "true"\n    level: spec\n'
    )
    res = freeze.verify(manifest, baseline, str(tmp_path))
    assert res["ok"] is False and any(
        "a:" in t and "removed" in t for t in res["tampered"]
    )


def test_allows_added_assertion(tmp_path):
    # legit gap-closing: add a NEW id, leave the frozen one (a, byte-identical) intact -> ok.
    manifest, baseline = _setup(tmp_path)
    freeze.record(manifest, baseline, str(tmp_path))
    (tmp_path / "manifest.yaml").write_text(
        _MANIFEST.format(cmd="python3 -m pytest -q sub/test_a.py")
        + '  - id: b\n    claim: "b holds"\n    command: "true"\n'
        + '    setup: ""\n    timeout: 30\n    level: spec\n    kind: example\n'
    )
    res = freeze.verify(manifest, baseline, str(tmp_path))
    assert res["ok"] is True and res["tampered"] == []


def test_detects_edited_test_file_under_dir_target(
    tmp_path,
):  # reviewer: directory tokens
    # command targets a DIRECTORY (pytest collects under it); editing a test there must be caught.
    manifest, baseline = _setup(tmp_path, cmd="python3 -m pytest -q sub")
    freeze.record(manifest, baseline, str(tmp_path))
    (tmp_path / "sub" / "test_a.py").write_text(
        "def test_a():\n    assert True  # gutted\n"
    )
    res = freeze.verify(manifest, baseline, str(tmp_path))
    assert res["ok"] is False and any("test-file-changed" in t for t in res["tampered"])


def test_detects_edited_test_file_under_glob_target(tmp_path):  # reviewer: glob tokens
    manifest, baseline = _setup(tmp_path, cmd="python3 -m pytest -q sub/test_*.py")
    freeze.record(manifest, baseline, str(tmp_path))
    (tmp_path / "sub" / "test_a.py").write_text(
        "def test_a():\n    assert True  # gutted\n"
    )
    res = freeze.verify(manifest, baseline, str(tmp_path))
    assert res["ok"] is False and any("test-file-changed" in t for t in res["tampered"])


def test_dir_target_leaves_product_code_editable(tmp_path):
    # a.py (product) lives under sub but is not a test file -> still mutable by the worker.
    manifest, baseline = _setup(tmp_path, cmd="python3 -m pytest -q sub")
    freeze.record(manifest, baseline, str(tmp_path))
    (tmp_path / "sub" / "a.py").write_text("def a():\n    return 42\n")
    res = freeze.verify(manifest, baseline, str(tmp_path))
    assert res["ok"] is True and res["tampered"] == []
