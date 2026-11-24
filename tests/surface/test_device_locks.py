"""The device lock recorder files a request before its client can see the answer."""

from starlette.types import Message, Receive, Scope, Send

from orca.resource_models.tracked_lock import TrackedLock
from tests.surface.device_locks import DeviceLockRecorder, locks_taken_by_the_last_request

_ARM = TrackedLock("arm transporter lock")


async def _app(scope: Scope, receive: Receive, send: Send) -> None:
    if scope["path"] == "/drives-the-arm":
        async with _ARM.held_for("get position"):
            pass
        await send({"type": "http.response.start", "status": 201, "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"{}"})
    elif scope["path"] == "/streams-then-drives-the-arm":
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"[", "more_body": True})
        async with _ARM.held_for("get position"):
            pass
        await send({"type": "http.response.body", "body": b"]"})
    else:
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})


async def _call(app: DeviceLockRecorder, path: str, seen_at_headers: list[set[str]]) -> None:
    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            seen_at_headers.append(locks_taken_by_the_last_request())

    await app({"type": "http", "method": "POST", "path": path, "headers": []}, receive, send)


async def test_a_204_after_a_device_command_is_not_charged_with_the_command_lock() -> None:
    app = DeviceLockRecorder(_app)
    seen: list[set[str]] = []
    await _call(app, "/drives-the-arm", seen)
    await _call(app, "/deletes-a-row", seen)
    assert seen == [{"arm transporter lock"}, set()]
    assert locks_taken_by_the_last_request() == set()


async def test_a_lock_taken_while_the_body_streams_counts_for_that_request() -> None:
    app = DeviceLockRecorder(_app)
    await _call(app, "/streams-then-drives-the-arm", [])
    assert locks_taken_by_the_last_request() == {"arm transporter lock"}
