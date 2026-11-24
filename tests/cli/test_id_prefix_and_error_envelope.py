"""CLI tests for ID prefix resolution and the error envelope formatter.

Covers:
- ``orca labware where <8-char-prefix>`` resolves to the full record.
- ``orca labware where <barcode>`` returns the barcode-matched record
  with no stderr 404 noise (Bug #3 regression).
- ``orca labware journey <prefix>`` resolves prefix.
- ``find_id_or_none`` returns None instead of exiting on the failure
  paths that the legacy ``resolve_id`` would have exited on.
- The local-daemon ``_check`` formatter parses the envelope-shaped
  ``{detail: {code, message, extras}}`` body and emits the typed
  message without the ``daemon call failed (N):`` prefix (Bug #6).
"""

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from typer.testing import CliRunner

from orca.cli import resolve
from orca.cli.app import app
from orca.cli.client import OrcaClient
from orca.cli.control_plane import ControlPlaneError
from orca.operations.labware_models import GetLabwareJourneyResponse, JourneyMove, LabwareSnapshotModel


runner = CliRunner()


def _labware(**overrides: Any) -> LabwareSnapshotModel:
    base: dict[str, Any] = {
        "id": "7faedeec-1234-4abc-9def-000000000001",
        "name": "plate-1",
        "template_name": "plate_96",
        "labware_type": "Cor_96_wellplate_360ul_Fb",
        "barcode": None,
        "current_location": "pad1",
    }
    base.update(overrides)
    return LabwareSnapshotModel(**base)


# -- find_id_or_none ----------------------------------------------------------


class TestFindIdOrNone:
    def test_returns_exact_match(self) -> None:
        candidates = ["abc-1", "xyz-2"]
        assert resolve.find_id_or_none("abc-1", candidates) == "abc-1"

    def test_returns_unique_prefix(self) -> None:
        candidates = ["7faedeec-1234", "9beedead-5678"]
        assert (
            resolve.find_id_or_none("7faedeec", candidates) == "7faedeec-1234"
        )

    def test_returns_none_for_too_short(self) -> None:
        assert resolve.find_id_or_none("ab", ["abc-1"]) is None

    def test_returns_none_for_no_match(self) -> None:
        assert resolve.find_id_or_none("zzzz", ["abc-1234"]) is None

    def test_returns_none_for_ambiguous(self) -> None:
        candidates = ["abc-1111", "abc-2222"]
        assert resolve.find_id_or_none("abc-", candidates) is None


# -- labware list: the type column ------------------------------------------


def test_labware_list_shows_each_rows_catalog_type() -> None:
    with patch("orca.cli.labware.get_client") as mock_local:
        mock_local.return_value.labware_list.return_value = [_labware()]
        result = runner.invoke(app, ["labware", "list"])
    assert result.exit_code == 0, result.output
    assert "Cor_96_wellplate_360ul_Fb" in result.output


def test_labware_where_shows_the_catalog_type() -> None:
    with patch("orca.cli.labware.get_client") as mock_local:
        mock_local.return_value.labware_list.return_value = [_labware()]
        result = runner.invoke(app, ["labware", "where", "7faedeec"])
    assert result.exit_code == 0, result.output
    assert "Cor_96_wellplate_360ul_Fb" in result.output


# -- labware where: prefix + barcode + no-leak --------------------------------


class TestLabwareWhereResolution:
    def test_prefix_matches_full_record(self) -> None:
        plate = _labware()
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [plate]
            result = runner.invoke(app, ["labware", "where", "7faedeec"])
        assert result.exit_code == 0, result.output
        # Table view renders the 8-char prefix + template/location of the
        # resolved record; pre-fix this would have 404'd before reaching
        # the success path.
        assert "7faedeec" in result.output
        assert "plate_96" in result.output
        assert "pad1" in result.output

    def test_barcode_lookup_emits_no_stderr_404(self) -> None:
        """Bug #3: pre-fix the id-lookup raised typer.Exit which printed
        a 404 to stderr before barcode-fallback succeeded. Operators saw
        an error AND the successful record. Now there is no error path
        on the barcode-fallback branch.
        """
        plate = _labware(barcode="BC-42")
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [plate]
            result = runner.invoke(app, ["labware", "where", "BC-42"])
        assert result.exit_code == 0, result.output
        assert "BC-42" in result.output
        assert "pad1" in result.output
        # No 404 / daemon-error chatter on the success path.
        assert "404" not in result.output
        assert "not found" not in result.output.lower()

    def test_unknown_ident_returns_clean_not_found(self) -> None:
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [_labware()]
            # `where` now falls through to the daemon for store-only
            # labware (post-rebuild). Stub both fall-through paths as
            # misses so the unknown-ident outcome still reaches the
            # not-found branch.
            mock_local.return_value.labware_get_by_id_or_none.return_value = None
            mock_local.return_value.labware_get_by_barcode_or_none.return_value = (
                None
            )
            result = runner.invoke(app, ["labware", "where", "no-such-thing"])
        assert result.exit_code != 0
        assert "no-such-thing" in result.output

    def test_ambiguous_prefix_surfaces_candidate_list(self) -> None:
        """Two labware ids that share a prefix should NOT collapse to
        a misleading "not found" -- the operator needs to see the
        candidate list so they can disambiguate. Mirrors the existing
        ``execution`` / ``thread`` behaviour.
        """
        a = _labware(id="abcdeeee-1111-4abc-9def-000000000001")
        b = _labware(id="abcdffff-2222-4abc-9def-000000000002")
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [a, b]
            result = runner.invoke(app, ["labware", "where", "abcd"])
        assert result.exit_code != 0, result.output
        assert "not found" not in result.output.lower()
        assert "abcdeeee" in result.output
        assert "abcdffff" in result.output


# -- labware journey: prefix resolution ---------------------------------------


class TestLabwareJourneyResolution:
    def test_prefix_resolves_before_journey_fetch(self) -> None:
        plate = _labware()
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [plate]
            mock_local.return_value.labware_journey.return_value = GetLabwareJourneyResponse(labware_id="x", entries=[])
            result = runner.invoke(app, ["labware", "journey", "7faedeec"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.labware_journey.assert_called_once_with(plate.id, kinds=None)

    def test_unrecognised_full_id_passes_through_to_daemon(self) -> None:
        """Regression found in review: the daemon's ``_find_by_id`` is
        store-first, while ``list_all`` only iterates ``system.labwares``.
        After a runtime rebuild, a labware that lives in the durable store
        but not in ``system.labwares`` should still resolve via the daemon's
        journey endpoint. The pre-resolve step must not reject full UUIDs
        the local view can't see.
        """
        plate = _labware()  # local view has only this one
        store_only_id = "deadbeef-9999-4abc-9def-000000000999"
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [plate]
            mock_local.return_value.labware_journey.return_value = GetLabwareJourneyResponse(labware_id="x", entries=[])
            result = runner.invoke(app, ["labware", "journey", store_only_id])
        assert result.exit_code == 0, result.output
        # Original input passes through unchanged; daemon does the
        # authoritative store-first check.
        mock_local.return_value.labware_journey.assert_called_once_with(
            store_only_id, kinds=None,
        )

    def test_ambiguous_prefix_fails_locally_no_passthrough(self) -> None:
        """Ambiguous prefix should fail with the candidate list instead
        of passing through. The daemon does exact match only, so a
        pass-through would always 404 -- losing the candidate context
        the operator needs to disambiguate.
        """
        a = _labware(id="abcdeeee-1111-4abc-9def-000000000001")
        b = _labware(id="abcdffff-2222-4abc-9def-000000000002")
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [a, b]
            result = runner.invoke(app, ["labware", "journey", "abcd"])
        assert result.exit_code != 0, result.output
        assert "abcdeeee" in result.output
        assert "abcdffff" in result.output
        mock_local.return_value.labware_journey.assert_not_called()


class TestLabwareJourneyLimit:
    def _moves(self, count: int) -> GetLabwareJourneyResponse:
        return GetLabwareJourneyResponse(
            labware_id="x",
            entries=[JourneyMove(sequence=n, position_id=f"pad_{n}", timestamp=float(n)) for n in range(count)],
        )

    def test_limit_keeps_the_newest_entries(self) -> None:
        plate = _labware()
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [plate]
            mock_local.return_value.labware_journey.return_value = self._moves(25)
            result = runner.invoke(app, ["--json", "labware", "journey", plate.id, "--kind", "move", "--limit", "20"])
        assert result.exit_code == 0, result.output
        mock_local.return_value.labware_journey.assert_called_once_with(plate.id, kinds=["move"])
        positions = [entry["position_id"] for entry in json.loads(result.output)["entries"]]
        assert positions == [f"pad_{n}" for n in range(5, 25)]

    def test_no_limit_prints_every_entry(self) -> None:
        plate = _labware()
        with patch("orca.cli.labware.get_client") as mock_local:
            mock_local.return_value.labware_list.return_value = [plate]
            mock_local.return_value.labware_journey.return_value = self._moves(25)
            result = runner.invoke(app, ["--json", "labware", "journey", plate.id])
        assert result.exit_code == 0, result.output
        assert len(json.loads(result.output)["entries"]) == 25

    def test_a_limit_below_one_is_refused(self) -> None:
        with patch("orca.cli.labware.get_client") as mock_local:
            result = runner.invoke(app, ["labware", "journey", "7faedeec", "--limit", "0"])
        assert result.exit_code != 0
        mock_local.return_value.labware_journey.assert_not_called()


# -- daemon error envelope formatter -----------------------------------------


def _envelope_handler(
    status_code: int,
    detail: dict[str, Any] | str | None,
) -> httpx.MockTransport:
    """Build a transport that returns the given envelope payload."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        body: dict[str, Any] = {}
        if detail is not None:
            body["detail"] = detail
        return httpx.Response(status_code, json=body)

    return httpx.MockTransport(handler)


class _ProbeClient(OrcaClient):
    """OrcaClient stand-in that overrides the http-client builder
    to inject a MockTransport. Constructs without a live daemon pid.
    """

    def __init__(self, transport: httpx.MockTransport) -> None:
        super().__init__("http://daemon.test", timeout=1.0)
        self._transport = transport

    def _make_http_client(self) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            base_url=self._base_url,
            timeout=self._timeout,
        )


class TestErrorEnvelopeFormatter:
    def test_envelope_detail_renders_code_and_message_no_prefix(
        self,
    ) -> None:
        """Bug #6: pre-fix this rendered as ``daemon call failed (404):
        {"detail": {...}}`` (raw JSON because the bare ErrorResponse
        validator rejected the dict-shaped detail). Now the typed code +
        message is rendered without the HTTP-layer prefix.
        """
        client = _ProbeClient(_envelope_handler(404, {
            "code": "WORKFLOW_NOT_FOUND",
            "message": "workflow 'nope' not found",
            "extras": {},
        }))
        resp = client._make_http_client().get("/anything")
        with pytest.raises(ControlPlaneError) as failure:
            client._check(resp)
        err = str(failure.value)
        assert "WORKFLOW_NOT_FOUND" in err
        assert "workflow 'nope' not found" in err
        assert "daemon call failed" not in err
        assert "HTTP" not in err  # no HTTP-status prefix

    def test_bare_string_detail_still_renders_clean(
        self,
    ) -> None:
        """Backward-compatible: bare ``{detail: str}`` daemon responses
        also drop the ``daemon call failed`` prefix; the operator sees
        the message directly.
        """
        client = _ProbeClient(_envelope_handler(409, "system already loaded"))
        resp = client._make_http_client().get("/anything")
        with pytest.raises(ControlPlaneError) as failure:
            client._check(resp)
        err = str(failure.value)
        assert "system already loaded" in err
        assert "daemon call failed" not in err

    def test_unparseable_body_falls_back_cleanly(
        self,
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(500, content=b"not json")

        client = _ProbeClient(httpx.MockTransport(handler))
        resp = client._make_http_client().get("/anything")
        with pytest.raises(ControlPlaneError) as failure:
            client._check(resp)
        err = str(failure.value)
        assert "not json" in err
        assert "daemon call failed" not in err

    def test_envelope_with_code_only_surfaces_code(
        self,
    ) -> None:
        """Partial envelope (code without message) should still surface
        the typed code rather than fall back to raw JSON.
        """
        client = _ProbeClient(_envelope_handler(409, {
            "code": "CONFLICT",
        }))
        resp = client._make_http_client().get("/anything")
        with pytest.raises(ControlPlaneError) as failure:
            client._check(resp)
        err = str(failure.value)
        assert "CONFLICT" in err
        assert "{" not in err  # no raw JSON spill
