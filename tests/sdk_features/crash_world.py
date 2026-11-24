"""Make one labware's thread crash where its own error handling does not reach."""

import pytest

from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)


def crash_when_starting(monkeypatch: pytest.MonkeyPatch, template_name: str) -> None:
    """The thread carrying `template_name` raises while it sets its labware up, before any step runs."""
    original = ExecutingLabwareThread.initialize_labware

    async def crash_for_one_template(self: ExecutingLabwareThread) -> None:
        if self.labware.template_name == template_name:
            raise RuntimeError(f"crash while starting {template_name}")
        await original(self)

    monkeypatch.setattr(ExecutingLabwareThread, "initialize_labware", crash_for_one_template)
