"""The daemon stores audit rows in its activity database, so a restart keeps them."""

from pathlib import Path

from fastapi.testclient import TestClient

from orca.daemon.app import create_app


def test_an_audit_row_written_before_a_restart_is_listed_after_it(tmp_path: Path) -> None:
    with TestClient(create_app(package_root=tmp_path)) as before:
        patched = before.patch(
            "/api/move-defaults/pf400",
            json={"set": {"travel_margin": 25.0}, "reason": "new gripper"},
        )
        assert patched.status_code == 200, patched.text

    with TestClient(create_app(package_root=tmp_path)) as after:
        listed = after.get("/api/audit", params={"action_name": "move_defaults.apply"})

    assert listed.status_code == 200, listed.text
    [row] = listed.json()
    assert row["reason"] == "new gripper"
    assert row["danger_level"] == "PHYSICAL"
    assert row["call_args"]["transporter_name"] == "pf400"
