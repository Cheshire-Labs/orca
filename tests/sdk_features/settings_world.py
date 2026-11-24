"""Run the one-plate shake workflow and read what the arm was sent."""

from cheshire_drivers.move_parameters import MoveParameters
from pydantic import JsonValue

from orca.runtime.deployment_registries import IDeploymentRegistries
from orca.runtime.startup import deployment_registries_over
from tests.sdk_features.kit import FeatureWorld, RecordedCommand, commands, run_to_end


def registries(world: FeatureWorld) -> IDeploymentRegistries:
    """The operator facades over the setup files the world was built from."""
    return deployment_registries_over(world.lab.stores)


def at(world: FeatureWorld, method: str, position: str) -> list[RecordedCommand]:
    return [c for c in commands(world, "arm", method) if c.args["position_id"] == position]


def handling(command: RecordedCommand) -> MoveParameters:
    return MoveParameters.model_validate(command.args["handling"])


def coordinates(command: RecordedCommand) -> dict[str, JsonValue]:
    found = command.args["coordinates"]
    assert isinstance(found, dict)
    return found


async def run_once(world: FeatureWorld) -> None:
    _, ended = await run_to_end(world)
    assert set(ended.values()) == {"COMPLETED"}, ended
