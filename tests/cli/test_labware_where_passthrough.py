"""Follow-up tests for the review regression on `labware where` and missing
prefix support on the operator-override verbs.

`labware where` had the same asymmetry a reviewer flagged for `labware
history`: local-catalog resolution doesn't see labware that lives in
`ILabwareStore` but not in `system.labwares` (the post-runtime-rebuild case).
Here we cover the daemon fall-through plus the prefix gap on `edit-location` /
`edit-barcode` / `reset-location`.
"""

from typing import Any
from unittest.mock import patch

import httpx
import pytest
import typer
from typer.testing import CliRunner

from orca.cli.app import app
from orca.cli.client import LocalDaemonClient
from orca.daemon.schemas import LabwareDTO


runner = CliRunner()


def _labware(**overrides: Any) -> LabwareDTO:
    base: dict[str, Any] = {
        "id": "7faedeec-1234-4abc-9def-000000000001",
        "name": "plate-1",
        "template_name": "plate_96",
        "barcode": None,
        "current_location": "pad1",
    }
    base.update(overrides)
    return LabwareDTO(**base)


# -- where: daemon fall-through for store-only labware -----------------------


class TestLabwareWherePassThrough:
    def test_local_match_does_not_call_daemon(self) -> None:
        plate = _labware()
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [plate]
            result = runner.invoke(app, ["labware", "where", plate.id])
        assert result.exit_code == 0, result.output
        mock_local.return_value.labware_get_by_id_or_none.assert_not_called()
        mock_local.return_value.labware_get_by_barcode_or_none.assert_not_called()

    def test_store_only_full_id_falls_through_to_daemon(self) -> None:
        """Regression for the same shape Copilot flagged on `history`:
        a full UUID that lives in `ILabwareStore` but not in
        `system.labwares` survives a runtime rebuild. Local view (built
        from `list_all`) can't see it; daemon's `_find_by_id` does
        (store-first). The fall-through must reach the daemon.
        """
        store_only = _labware(id="deadbeef-9999-4abc-9def-000000000999")
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = []
            mock_local.return_value.labware_get_by_id_or_none.return_value = (
                store_only
            )
            result = runner.invoke(app, ["labware", "where", store_only.id])
        assert result.exit_code == 0, result.output
        mock_local.return_value.labware_get_by_id_or_none.assert_called_once_with(
            store_only.id,
        )
        assert "deadbeef" in result.output

    def test_store_only_barcode_falls_through_to_daemon(self) -> None:
        """Barcode addressing for store-only labware also reaches the
        daemon. Tries id first; the id-or-none miss must not leak a 404
        to stderr (Bug 3 -- the whole reason `where` resolves locally).
        """
        store_only = _labware(barcode="BC-99")
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = []
            mock_local.return_value.labware_get_by_id_or_none.return_value = None
            mock_local.return_value.labware_get_by_barcode_or_none.return_value = (
                store_only
            )
            result = runner.invoke(app, ["labware", "where", "BC-99"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.labware_get_by_id_or_none.assert_called_once_with(
            "BC-99",
        )
        mock_local.return_value.labware_get_by_barcode_or_none.assert_called_once_with(
            "BC-99",
        )
        assert "BC-99" in result.output
        # Bug 3 regression guard: no 404 / "daemon call failed" / "not found"
        # noise on the barcode-fallback success path.
        assert "404" not in result.output
        assert "daemon call failed" not in result.output
        assert "not found" not in result.output.lower()

    def test_daemon_miss_returns_clean_not_found(self) -> None:
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = []
            mock_local.return_value.labware_get_by_id_or_none.return_value = None
            mock_local.return_value.labware_get_by_barcode_or_none.return_value = (
                None
            )
            result = runner.invoke(app, ["labware", "where", "unknown-xyz"])
        assert result.exit_code != 0
        assert "unknown-xyz" in result.output

    def test_ambiguous_prefix_does_not_fall_through(self) -> None:
        """Ambiguous local prefix means the operator typo'd a short id;
        passing through would just 404 and lose the candidate list.
        """
        a = _labware(id="abcdeeee-1111-4abc-9def-000000000001")
        b = _labware(id="abcdffff-2222-4abc-9def-000000000002")
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [a, b]
            result = runner.invoke(app, ["labware", "where", "abcd"])
        assert result.exit_code != 0
        assert "abcdeeee" in result.output
        assert "abcdffff" in result.output
        mock_local.return_value.labware_get_by_id_or_none.assert_not_called()


# -- edit-location / edit-barcode / reset-location: prefix + pass-through ----


class TestLabwareEditVerbsPrefix:
    def test_edit_location_resolves_prefix(self) -> None:
        plate = _labware()
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [plate]
            result = runner.invoke(app, [
                "labware", "edit-location", "7faedeec", "padX",
                "--reason", "test",
            ])
        assert result.exit_code == 0, result.output
        mock_local.return_value.labware_edit_location.assert_called_once_with(
            plate.id, "padX", "test",
        )

    def test_edit_location_passes_unknown_full_id_through(self) -> None:
        store_only_id = "deadbeef-9999-4abc-9def-000000000999"
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = []
            result = runner.invoke(app, [
                "labware", "edit-location", store_only_id, "padX",
                "--reason", "rebuild-recovery",
            ])
        assert result.exit_code == 0, result.output
        mock_local.return_value.labware_edit_location.assert_called_once_with(
            store_only_id, "padX", "rebuild-recovery",
        )

    def test_edit_barcode_resolves_prefix(self) -> None:
        plate = _labware()
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [plate]
            result = runner.invoke(app, [
                "labware", "edit-barcode", "7faedeec", "BC-NEW",
            ])
        assert result.exit_code == 0, result.output
        mock_local.return_value.labware_edit_barcode.assert_called_once_with(
            plate.id, "BC-NEW",
        )

    def test_reset_location_resolves_prefix(self) -> None:
        plate = _labware()
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [plate]
            result = runner.invoke(app, [
                "labware", "reset-location", "7faedeec", "padY",
                "--reason", "manual reset",
            ])
        assert result.exit_code == 0, result.output
        mock_local.return_value.labware_reset_location.assert_called_once_with(
            plate.id, "padY", "manual reset",
        )

    def test_edit_verbs_fail_on_ambiguous_prefix(self) -> None:
        a = _labware(id="abcdeeee-1111-4abc-9def-000000000001")
        b = _labware(id="abcdffff-2222-4abc-9def-000000000002")
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [a, b]
            result = runner.invoke(app, [
                "labware", "edit-location", "abcd", "padX",
                "--reason", "test",
            ])
        assert result.exit_code != 0
        assert "abcdeeee" in result.output
        mock_local.return_value.labware_edit_location.assert_not_called()


# -- LocalDaemonClient: _or_none variants ------------------------------------


def _transport(
    status_code: int, body: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    payload = body or {}

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status_code, json=payload)

    return httpx.MockTransport(handler)


class _ProbeClient(LocalDaemonClient):
    """Constructs without a live daemon pid; injects a MockTransport."""

    def __init__(self, transport: httpx.MockTransport) -> None:
        self._base_url = "http://daemon.test"
        self._timeout = 1.0
        self._transport = transport

    def _make_http_client(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            base_url=self._base_url,
            timeout=self._timeout,
        )


class TestClientOrNoneVariants:
    def test_get_by_id_or_none_returns_none_on_404(self) -> None:
        client = _ProbeClient(_transport(404, {
            "detail": {"code": "LABWARE_NOT_FOUND", "message": "no", "extras": {}},
        }))
        assert client.labware_get_by_id_or_none("x") is None

    def test_get_by_id_or_none_returns_dto_on_200(self) -> None:
        snap = {
            "id": "7faedeec-1234-4abc-9def-000000000001",
            "name": "p", "template_name": "plate_96",
            "barcode": None, "current_location": None,
        }
        client = _ProbeClient(_transport(200, {"labware": snap}))
        result = client.labware_get_by_id_or_none("x")
        assert result is not None
        assert result.id == snap["id"]

    def test_get_by_id_or_none_still_bails_on_5xx(self) -> None:
        client = _ProbeClient(_transport(500, {
            "detail": {"code": "INTERNAL_ERROR", "message": "boom", "extras": {}},
        }))
        with pytest.raises(typer.Exit):
            client.labware_get_by_id_or_none("x")

    def test_get_by_barcode_or_none_returns_none_on_404(self) -> None:
        client = _ProbeClient(_transport(404, {
            "detail": {"code": "LABWARE_NOT_FOUND", "message": "no", "extras": {}},
        }))
        assert client.labware_get_by_barcode_or_none("BC") is None

    def test_get_by_barcode_or_none_returns_dto_on_200(self) -> None:
        snap = {
            "id": "7faedeec-1234-4abc-9def-000000000001",
            "name": "p", "template_name": "plate_96",
            "barcode": "BC", "current_location": None,
        }
        client = _ProbeClient(_transport(200, {"labware": snap}))
        result = client.labware_get_by_barcode_or_none("BC")
        assert result is not None
        assert result.barcode == "BC"
