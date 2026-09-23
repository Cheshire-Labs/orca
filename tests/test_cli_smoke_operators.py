"""CliRunner smoke tests for operator-override verbs added in phase/implement-all-deferred.

The purpose of these tests is to catch regressions in the three specific
layers route-level tests don't cover:

1. typer argument parsing (positional vs option, --reason gating, etc.).
2. --json emission when --json is set at root.
3. CLI -> LocalDaemonClient -> HTTP wiring for every new verb.

These do NOT retest the daemon-side behavior; the route tests in
tests/daemon/test_routes_*.py handle that. Here we only assert the CLI
surfaces the right inputs to LocalDaemonClient and renders the right outputs
for each mode.

All tests run against the `loaded_daemon` fixture (isolated subprocess
daemon + fixture system). The fixture system provides:
- device "shaker1" (UniversalMockDevice, implements IShaker +
  IGenericExecutable among others -- enough to exercise send/invoke).
- transporter "robot1".
- labware template "plate_96".
- locations "shaker1", "pad1".
- workflow "simple_workflow".
"""

import json
import re

from typer.testing import CliRunner

from orca.cli.app import app


# Click 8.1.x defaults CliRunner to mix_stderr=True, which folds
# output.info() status text into result.stdout and corrupts --json
# payloads. Click 8.2+ dropped the kwarg (stderr is always separate).
# Try the explicit arg first so 8.1.x keeps streams split.
try:
    runner = CliRunner(mix_stderr=False)
except TypeError:
    runner = CliRunner()


# -- Labware operator verbs ------------------------------------------------


def test_labware_register_table_output(loaded_daemon) -> None:
    """Register a plate; table mode prints an info line with the short id.

    Register is OPERATOR-level (adds a new instance, failure is
    visible at first move). No --reason required; differs from the
    PHYSICAL-level edit-location and reset-location verbs.

    The human info line is written via output.info -> stderr. Asserting
    its content (short id + template + barcode) proves the table-mode
    render reflects the registered instance, not just a zero exit. The
    --json snapshot shape is covered by test_labware_register_json_output.
    """
    result = runner.invoke(
        app, ["labware", "register", "plate_96", "--barcode", "SMOKE-1"],
    )
    assert result.exit_code == 0, (
        f"exit={result.exit_code} stderr={result.stderr!r}"
    )
    line = result.stderr
    match = re.search(
        r"registered labware ([0-9a-f]{8}) \(template=plate_96, barcode=SMOKE-1\)",
        line,
    )
    assert match is not None, f"info line missing/wrong shape: {line!r}"
    short_id = match.group(1)
    # Cross-check the short id against `labware list` so a render that printed
    # a constant or the template name instead of the real id fails.
    listing = runner.invoke(app, ["--json", "labware", "list"])
    assert listing.exit_code == 0, listing.stdout
    ids = [item["id"] for item in json.loads(listing.stdout)]
    assert any(full_id.startswith(short_id) for full_id in ids), (
        f"short id {short_id!r} matches no registered instance in {ids!r}"
    )


def test_labware_register_json_output(loaded_daemon) -> None:
    """--json emits a snapshot with id/template_name/barcode/current_location."""
    result = runner.invoke(
        app, [
            "--json", "labware", "register", "plate_96",
            "--barcode", "SMOKE-JSON", "--location", "pad1",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["template_name"] == "plate_96"
    assert body["barcode"] == "SMOKE-JSON"
    assert body["current_location"] == "pad1"
    assert "id" in body


def _register_for_test(barcode: str, location: str | None = None) -> str:
    args = [
        "--json", "labware", "register", "plate_96", "--barcode", barcode,
    ]
    if location is not None:
        args.extend(["--location", location])
    reg = runner.invoke(app, args)
    assert reg.exit_code == 0, reg.stdout
    return json.loads(reg.stdout)["id"]


def test_labware_edit_location_requires_reason(loaded_daemon) -> None:
    """Missing --reason -> non-zero exit; Typer rejects before the HTTP call.

    edit-location is PHYSICAL danger: it restates where a plate is, which
    can send a live thread replanning its move. Reason required.
    """
    labware_id = _register_for_test("SMOKE-NOREASON")

    result = runner.invoke(
        app, ["labware", "edit-location", labware_id, "pad1"],
    )
    assert result.exit_code != 0


def test_labware_edit_location_with_reason_succeeds(loaded_daemon) -> None:
    labware_id = _register_for_test("SMOKE-EDITLOC", location="pad1")

    result = runner.invoke(
        app, [
            "labware", "edit-location", labware_id, "shaker1",
            "--reason", "smoke test",
        ],
    )
    assert result.exit_code == 0, result.stdout


def test_labware_edit_barcode_succeeds(loaded_daemon) -> None:
    """Edit-barcode is OPERATOR-level (scanner mismatch is visible +
    recoverable at next read). No --reason required.
    """
    labware_id = _register_for_test("SMOKE-BARCODE-OLD")

    result = runner.invoke(
        app, ["labware", "edit-barcode", labware_id, "SMOKE-BARCODE-NEW"],
    )
    assert result.exit_code == 0, result.stdout


def test_labware_reset_location_requires_reason(loaded_daemon) -> None:
    """Reset-location is PHYSICAL ("corrupts the owning thread's location
    assumptions" per the engine docstring). Reason required.
    """
    labware_id = _register_for_test("SMOKE-RESET-NOREASON")

    result = runner.invoke(
        app, ["labware", "reset-location", labware_id, "pad1"],
    )
    assert result.exit_code != 0


def test_labware_reset_location_with_reason_succeeds(loaded_daemon) -> None:
    labware_id = _register_for_test("SMOKE-RESET")

    result = runner.invoke(
        app, [
            "labware", "reset-location", labware_id, "pad1",
            "--reason", "smoke reset",
        ],
    )
    assert result.exit_code == 0, result.stdout


# -- Device verbs ----------------------------------------------------------


def test_device_list_table(loaded_daemon) -> None:
    result = runner.invoke(app, ["device", "list"])
    assert result.exit_code == 0
    assert "shaker1" in result.stdout


def test_device_info_with_capabilities_table(loaded_daemon) -> None:
    """--capabilities renders the device's typed capability table to stdout.

    A bare exit==0 would pass even if the table were empty or stubbed.
    Cross-check the rendered rows against the JSON capabilities so a render
    that printed a header with no real rows (or the wrong device) fails.
    """
    snapshot = runner.invoke(
        app, ["--json", "device", "info", "shaker1", "--capabilities"],
    )
    assert snapshot.exit_code == 0, snapshot.stdout
    cap_names = [c["capability"] for c in json.loads(snapshot.stdout)["capabilities"]]
    assert cap_names, "fixture device advertised no capabilities"

    result = runner.invoke(
        app, ["device", "info", "shaker1", "--capabilities"],
    )
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert "Capabilities (shaker1)" in out
    for column in ("capability", "danger", "params"):
        assert column in out, f"missing column header {column!r} in:\n{out}"
    for name in cap_names:
        assert name in out, f"capability {name!r} not rendered in table:\n{out}"
    # The shaker.shake row's danger level + params must render, not just the name.
    assert "shaker.shake" in out
    assert "duration:int" in out
    assert "speed:int" in out
    assert "PHYSICAL" in out


def test_device_info_with_capabilities_json(loaded_daemon) -> None:
    """--json + --capabilities embeds the capabilities list in the snapshot."""
    result = runner.invoke(
        app, ["--json", "device", "info", "shaker1", "--capabilities"],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["name"] == "shaker1"
    assert isinstance(body["capabilities"], list)
    # UniversalMockDevice implements IShaker (among others) -- at least one
    # capability entry should exist. Don't pin the count: the capability
    # registry can grow without this test caring.
    assert len(body["capabilities"]) > 0


def test_device_initialize_no_reason_succeeds(loaded_daemon) -> None:
    """`device initialize` does not require --reason.

    Device verbs are operator commands during a run, not audit-worthy on
    their own. Labware operator-override verbs at PHYSICAL danger level
    still require --reason.
    """
    result = runner.invoke(app, ["device", "initialize", "shaker1"])
    assert result.exit_code == 0, result.stdout


def test_device_send_no_reason_succeeds(loaded_daemon) -> None:
    """`device send` does not require --reason. Mirror of `device initialize`;
    same rationale.
    """
    result = runner.invoke(app, ["device", "send", "shaker1", "noop"])
    assert result.exit_code == 0, result.stdout


def test_device_invoke_accepts_bare_method_name(loaded_daemon) -> None:
    result = runner.invoke(
        app, [
            "device", "invoke", "shaker1", "shake",
            "--params", '{"duration": 1, "speed": 500}',
        ],
    )
    assert result.exit_code == 0, result.stdout


def test_device_invoke_accepts_the_namespace_the_listing_prints(loaded_daemon) -> None:
    """`shaker.shake` dispatches `shake`, and `anything.shake` is refused.

    `shaker` is an orca-side namespace: `device capabilities` prints it in
    front of an interface method, and it is not part of the method name, so it
    is dropped. Nothing else is. A driver's vendor commands are advertised
    under the object they live on (`gripper.ungrip`), so a prefix is a real
    part of a real name; discarding an unrecognised one would trim
    `gripper.move_to` down to `move_to` and reach a pipette mount instead of
    the jaws. An unknown prefix is therefore refused, not trimmed until it
    matches something.
    """
    params = '{"duration": 1, "speed": 500}'

    namespaced = runner.invoke(
        app, ["--json", "device", "invoke", "shaker1", "shaker.shake", "--params", params],
    )
    assert namespaced.exit_code == 0, namespaced.stdout
    body = json.loads(namespaced.stdout)
    assert body["success"] is True
    assert body["device_name"] == "shaker1"

    unknown_prefix = runner.invoke(
        app, ["--json", "device", "invoke", "shaker1", "anything.shake", "--params", params],
    )
    assert unknown_prefix.exit_code != 0, unknown_prefix.stdout

    unknown_method = runner.invoke(
        app, ["device", "invoke", "shaker1", "shaker.notamethod", "--params", "{}"],
    )
    assert unknown_method.exit_code != 0, unknown_method.stdout


def test_device_invoke_kv_pairs_parse_to_typed_dict() -> None:
    """The positional key=value parser coerces scalars to their JSON types.

    This is the load-bearing logic behind `device invoke a=b c=d`: a broken
    split or missing coercion would send strings where the capability method
    wants ints. Asserted directly because the subprocess daemon's invocation
    result does not echo the dispatched kwargs back.
    """
    from orca.cli.device import _parse_kv_pairs

    parsed = _parse_kv_pairs(["duration=1", "speed=500"])
    assert parsed == {"duration": 1, "speed": 500}
    assert all(isinstance(v, int) for v in parsed.values())
    mixed = _parse_kv_pairs(["g=2.5", "enabled=true", "name=plateA"])
    assert mixed == {"g": 2.5, "enabled": True, "name": "plateA"}


def test_device_invoke_with_kv_positionals(loaded_daemon) -> None:
    """`device invoke` with positional key=value pairs (alternative to --params).

    `shake(duration: int, speed: int)` has no default args, so the daemon's
    `method(**kwargs)` dispatch raises TypeError (-> HTTP 400 -> non-zero exit)
    unless both kv positionals parsed into exactly those two keys. The JSON
    result confirms the device actually ran the parsed command, not just that
    typer accepted the args.
    """
    result = runner.invoke(
        app, [
            "--json", "device", "invoke", "shaker1", "shake",
            "duration=1", "speed=500",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["success"] is True
    assert body["command_or_capability"] == "shake"
    assert body["device_name"] == "shaker1"


# -- Submission verbs ------------------------------------------------------


def test_submission_submit_groupless_force_accepts(loaded_daemon) -> None:
    """Groupless submit via root --yes skips the typer.confirm prompt AND lands.

    Without --yes, typer.confirm blocks on stdin; CliRunner would hang. The
    accept is asserted by its side-effect (the submission appears in
    `submission list`), so a force path that exits 0 without submitting fails
    here. This runs in table mode on purpose -- `test_submission_submit_json_output`
    covers the --json snapshot shape; here we prove the human force path commits.
    """
    before = runner.invoke(app, ["--json", "submission", "list"])
    assert before.exit_code == 0, before.stdout
    count_before = len(json.loads(before.stdout))

    result = runner.invoke(
        app, ["--yes", "submission", "submit", "simple_workflow", "--run-mode", "PURE_SIM"],
    )
    assert result.exit_code == 0, result.stdout

    after = runner.invoke(app, ["--json", "submission", "list"])
    assert after.exit_code == 0, after.stdout
    items = json.loads(after.stdout)
    assert len(items) == count_before + 1
    groupless = [
        s for s in items
        if s["workflow_name"] == "simple_workflow"
        and s["group_count"] == 0
        and s["batch_mode"] == "STANDALONE"
    ]
    assert groupless, items
    assert all(s["execution_id"] for s in groupless)


def test_submission_submit_json_output(loaded_daemon) -> None:
    result = runner.invoke(
        app, [
            "--yes", "--json",
            "submission", "submit", "simple_workflow",
            "--run-mode", "PURE_SIM",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["workflow_name"] == "simple_workflow"
    assert body["group_count"] == 0
    assert body["batch_mode"] == "STANDALONE"
    assert "id" in body
    assert "execution_id" in body


def test_submission_list_after_submit(loaded_daemon) -> None:
    submit = runner.invoke(
        app, [
            "--yes", "--json",
            "submission", "submit", "simple_workflow",
            "--run-mode", "PURE_SIM",
        ],
    )
    assert submit.exit_code == 0
    submission_id = json.loads(submit.stdout)["id"]

    listing = runner.invoke(app, ["--json", "submission", "list"])
    assert listing.exit_code == 0
    items = json.loads(listing.stdout)
    assert any(s["id"] == submission_id for s in items)


def test_submission_detail_returns_the_snapshot(loaded_daemon) -> None:
    submit = runner.invoke(
        app, [
            "--yes", "--json",
            "submission", "submit", "simple_workflow",
            "--run-mode", "PURE_SIM",
        ],
    )
    submission_id = json.loads(submit.stdout)["id"]

    result = runner.invoke(
        app, ["--json", "submission", "detail", submission_id],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["id"] == submission_id


# -- Execution close + thread spawn ---------------------------------------


def test_execution_close_transitions_to_draining(loaded_daemon) -> None:
    """Submit, then close the execution; JSON shows phase=draining. Uses the
    slow-window workflow so the execution cannot complete before the close
    lands under parallel-suite load (a completed execution is never
    draining)."""
    import httpx

    load = httpx.post(
        f"http://127.0.0.1:{loaded_daemon.port}/workflows",
        json={"spec": "tests.daemon.daemon_test_fixture_topology:build_workflow_slow"},
        timeout=30.0,
    )
    assert load.status_code == 200, load.text

    submit = runner.invoke(
        app, ["--yes", "--json", "run", "slow_window_workflow", "--run-mode", "PURE_SIM"],
    )
    assert submit.exit_code == 0
    execution_id = json.loads(submit.stdout)["id"]

    result = runner.invoke(
        app, ["--yes", "--json", "execution", "close", execution_id],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert body["execution_id"] == execution_id
    assert body["phase"] == "draining"


def test_execution_thread_spawn_returns_snapshot(loaded_daemon) -> None:
    """`orca execution thread spawn` against a live execution returns a
    thread snapshot. Uses the slow-window workflow: `simple_workflow`'s
    2.5s mid-action window loses the wall-clock race against the CLI
    round-trip chain under parallel-suite load; the 60s window cannot."""
    import time

    import httpx

    load = httpx.post(
        f"http://127.0.0.1:{loaded_daemon.port}/workflows",
        json={"spec": "tests.daemon.daemon_test_fixture_topology:build_workflow_slow"},
        timeout=30.0,
    )
    assert load.status_code == 200, load.text

    submit = runner.invoke(
        app, ["--yes", "--json", "run", "slow_window_workflow", "--run-mode", "PURE_SIM"],
    )
    assert submit.exit_code == 0
    execution_id = json.loads(submit.stdout)["id"]

    # Poll until the workflow task has attached and its entry thread exists
    # (spawn awaits workflow_attached), rather than guessing with a fixed sleep.
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        threads = runner.invoke(
            app, ["--json", "execution", "threads", execution_id],
        )
        if threads.exit_code == 0 and json.loads(threads.stdout):
            break
        time.sleep(0.05)

    result = runner.invoke(
        app, [
            "--yes", "--json",
            "execution", "thread", "spawn", execution_id, "plate_96_slow",
        ],
    )
    assert result.exit_code == 0, result.stdout
    body = json.loads(result.stdout)
    assert "id" in body
    assert "name" in body
    assert "status" in body
