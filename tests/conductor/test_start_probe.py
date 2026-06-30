from conductor import start_probe


def _manifest(tmp_path, ids):
    body = "assertions:\n" + "".join(
        f'  - id: {i}\n    command: "true"\n    level: spec\n' for i in ids
    )
    p = tmp_path / "manifest.yaml"
    p.write_text(body)
    return str(p)


def test_ready_requires_full_coverage_and_determinate(tmp_path):
    m = _manifest(tmp_path, ["a", "b"])
    assert (
        start_probe.assertions_ready(["a", "b"], m, runner_exit=1) is True
    )  # covered + red ok
    assert (
        start_probe.assertions_ready(["a", "b"], m, runner_exit=0) is True
    )  # covered + green ok
    assert (
        start_probe.assertions_ready(["a", "b", "c"], m, runner_exit=1) is False
    )  # missing c
    assert (
        start_probe.assertions_ready(["a", "b"], m, runner_exit=5) is False
    )  # exit 5 not determinate
    assert (
        start_probe.assertions_ready(["a"], str(tmp_path / "none.yaml"), 1) is False
    )  # no manifest


def test_empty_expected_ids_is_not_ready(tmp_path):  # review: vacuous coverage
    # [] <= present is trivially true; an empty expected-id set must NOT report the gate built,
    # or /conductor:start would skip assertions-to-tests and freeze the wrong (sample) gate.
    m = _manifest(tmp_path, ["x"])
    assert start_probe.assertions_ready([], m, runner_exit=0) is False
