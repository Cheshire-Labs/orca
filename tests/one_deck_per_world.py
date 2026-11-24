from cheshire_drivers.interfaces import ILiquidHandlerWithProtocolDriver

from orca.modes.run_modes import OUT_OF_RUN_BASE, WorkflowRunMode, current_run_mode

from tests.recording_handler import RecordingHandler


class OneDeckPerWorld(RecordingHandler):
    """A handler set to live, keeping one deck per mode: a write lands only in its own world.

    A check run gets a simulator of its own, as the check-run client inside Orca gives it.
    """

    def __init__(self, live: ILiquidHandlerWithProtocolDriver, sim: ILiquidHandlerWithProtocolDriver) -> None:
        self._worlds: dict[WorkflowRunMode, ILiquidHandlerWithProtocolDriver] = {
            WorkflowRunMode.LIVE: live, WorkflowRunMode.SIM: sim, WorkflowRunMode.CHECK_RUN: RecordingHandler(),
        }
        self.calls = []

    @property
    def _inner(self) -> ILiquidHandlerWithProtocolDriver:
        return self._worlds[current_run_mode.get(OUT_OF_RUN_BASE)]

    @property
    def _handler(self) -> ILiquidHandlerWithProtocolDriver:
        return self._inner
