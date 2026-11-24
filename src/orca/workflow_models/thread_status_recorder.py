from typing import Protocol


class IThreadStatusRecorder(Protocol):
    """Saves a thread's status change in its run's record, as part of the change."""

    def record_thread_status(
        self, execution_id: str, *, thread_id: str, name: str,
        template_name: str, status: str,
        last_error: str | None, pause_reason: str | None, group_id: str | None,
    ) -> None: ...
