"""The surface suite reads a request's locks as soon as its answer arrives, so the recorder must file them first."""

from starlette.types import Message, Receive, Scope, Send

from orca.resource_models.tracked_lock import TrackedLock
from tests.surface.device_locks import DeviceLockRecorder, locks_taken_by_the_last_request


async def _no_body(scope: Scope, receive: Receive, send: Send) -> None:
    async with TrackedLock("arm transporter lock").held_for("capture"):
        pass
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _nothing_held(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


async def _receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


async def test_a_204_answer_reads_its_own_locks_when_its_start_leaves() -> None:
    # A 204 is complete on the wire at its start message, so the client can read the record then.
    seen_at_start: list[set[str]] = []

    async def server_send(message: Message) -> None:
        if message["type"] == "http.response.start":
            seen_at_start.append(locks_taken_by_the_last_request())

    await DeviceLockRecorder(_no_body)({"type": "http"}, _receive, server_send)
    await DeviceLockRecorder(_nothing_held)({"type": "http"}, _receive, server_send)

    assert seen_at_start == [{"arm transporter lock"}, set()]
