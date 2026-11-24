"""A refusal or a worklist line names the thing that is wrong and a verb that exists."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from typer.testing import CliRunner

from orca.runtime.errors import ThreadTemplateNotFoundError
from orca.cli import labware, state
from orca.cli.app import app
from orca.operations.protocol import OperationError
from orca.operations.state_models import UnsettledStateResponse, UnsettledSubjectDTO
from orca.operations.thread import SpawnThreadOperation
from orca.operations.thread_models import SpawnThreadRequest
from orca.state.provenance import Provenance


def test_mark_tips_used_with_no_position_asks_for_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labware, "get_client", MagicMock)

    result = CliRunner().invoke(app, ["labware", "mark-tips-used", "rack1", "--reason", "a hand took a column"])

    assert result.exit_code == 2, result.output
    assert "--position" in result.output


@pytest.mark.parametrize(("operation_verb", "cli_verb"), [
    ("set-well-volumes", "orca labware set-volume"),
    ("set-tip-state", "orca labware set-tips"),
    ("set-mounted-tips", "orca device set-mounted-tips"),
])
def test_the_unsettled_worklist_names_the_cli_verb(
    monkeypatch: pytest.MonkeyPatch, operation_verb: str, cli_verb: str,
) -> None:
    listed = UnsettledStateResponse(unsettled=[UnsettledSubjectDTO(
        subject="plate_1", subject_id="lw1", subject_kind="labware", provenance=Provenance.UNKNOWN,
        detail="nobody has said what it holds", settle_with=operation_verb,
    )])
    client = MagicMock()
    client.state_unsettled.return_value = listed
    monkeypatch.setattr(state, "get_client", lambda: client)

    result = CliRunner().invoke(app, ["state", "unsettled"])

    assert f"settle with: {cli_verb}\n" in result.output


@pytest.mark.asyncio
async def test_a_spawn_of_an_unknown_template_says_only_what_is_missing() -> None:
    said = "No thread template named 'nope' in workflow 'learn_orca'. Known: ['tips']"
    runtime = MagicMock()
    runtime.threads.spawn_thread = AsyncMock(side_effect=ThreadTemplateNotFoundError(said))

    with pytest.raises(OperationError) as refused:
        await SpawnThreadOperation(runtime).run(SpawnThreadRequest(execution_id="e", template_name="nope", reason="r"))

    assert refused.value.message == said
