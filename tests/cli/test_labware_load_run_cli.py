"""`orca labware load-run`: one call loads the threads named, each with an optional uL per well."""

from unittest.mock import patch

from typer.testing import CliRunner

from orca.cli.app import app
from orca.operations.run_loading_models import (
    LoadedRunItem,
    LoadRunLabwareRequest,
    LoadRunLabwareResponse,
    NotLoadedRunItem,
    RunLabwareItem,
)

runner = CliRunner()


def test_each_item_is_sent_with_its_volume_and_the_result_is_reported() -> None:
    with patch("orca.cli.labware.get_client") as client:
        client.return_value.labware_load_run.return_value = LoadRunLabwareResponse(
            cleared=[], loaded=[LoadedRunItem(thread_template_name="sample", labware_id="lw-1")], kept=[],
            not_loaded=[NotLoadedRunItem(thread_template_name="tips", code="not_found", message="no such thread")],
        )
        result = runner.invoke(app, ["labware", "load-run", "smc", "--item", "sample=40", "--item", "tips"])

    client.return_value.labware_load_run.assert_called_once_with(LoadRunLabwareRequest(
        workflow_name="smc",
        items=[RunLabwareItem(thread_template_name="sample", volume_ul=40.0), RunLabwareItem(thread_template_name="tips")],
    ))
    assert result.exit_code == 1
    assert "sample: loaded as lw-1" in result.output and "tips: not loaded: no such thread" in result.output


def test_clearing_first_with_no_item_loads_every_thread() -> None:
    with patch("orca.cli.labware.get_client") as client:
        client.return_value.labware_load_run.return_value = LoadRunLabwareResponse(
            cleared=["lw-0"], loaded=[], kept=[], not_loaded=[],
        )
        result = runner.invoke(app, ["labware", "load-run", "smc", "--clear-all-first", "--yes"])

    client.return_value.labware_load_run.assert_called_once_with(
        LoadRunLabwareRequest(workflow_name="smc", items=[], clear_all_first=True),
    )
    assert result.exit_code == 0, result.output


def test_clearing_first_refuses_on_a_non_tty_without_yes() -> None:
    with patch("orca.cli.labware.get_client") as client:
        result = runner.invoke(app, ["labware", "load-run", "smc", "--item", "sample", "--clear-all-first"])

    assert result.exit_code == 2
    client.assert_not_called()


def test_a_volume_that_is_not_a_number_is_refused_before_any_call() -> None:
    with patch("orca.cli.labware.get_client") as client:
        result = runner.invoke(app, ["labware", "load-run", "smc", "--item", "sample=lots"])

    assert result.exit_code == 2
    client.assert_not_called()
