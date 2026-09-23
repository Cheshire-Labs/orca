"""Interface for runtime mutation (skip + insert) and pause control."""

from abc import ABC, abstractmethod

from orca.workflow_models.action_template import ActionTemplate
from orca.workflow_models.method_template import MethodTemplate
from orca.workflow_models.mutation_position import InsertPosition


class IThreadMutator(ABC):

    # --- Pause control ---

    @abstractmethod
    def pause_thread(self, thread_id: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def resume_thread(self, thread_id: str) -> None:
        raise NotImplementedError

    # --- Method mutation: skip + insert + abort ---

    @abstractmethod
    def skip_pending_method(
        self, thread_id: str,
        method_id: str | None = None, method_name: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def abort_method(
        self, thread_id: str,
        method_id: str | None = None, method_name: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def insert_method(
        self, thread_id: str, template: MethodTemplate,
        where: InsertPosition,
    ) -> None:
        """Synchronous insert. Used by `_SystemMutationContext` (the
        `mutate_on_next_pause` callback path) which is sync by the
        `IThreadMutationContext` contract. Iterates the template's
        async generator on a worker thread; do NOT call from an async
        request handler -- use `insert_method_async` instead.
        """
        raise NotImplementedError

    @abstractmethod
    async def insert_method_async(
        self, thread_id: str, template: MethodTemplate,
        where: InsertPosition,
    ) -> None:
        """Async insert. Iterates the template generator with native
        `async for` on the caller's event loop, so the caller does not
        block for up to 10s on the thread-pool fallback. Used by
        `ThreadFacade.insert_method` (REST/CLI/MCP wire path).
        """
        raise NotImplementedError

    @abstractmethod
    async def replace_method(
        self, thread_id: str, target_name: str, template: MethodTemplate,
    ) -> bool:
        """Replace a method, matched by name first-match (one-shot; names are
        not unique on the lane). Pending target: spliced in place (skip +
        insert). Current error-paused method: replacement staged to run next;
        returns True so the caller drops the failed method via
        recover_thread(ABORT_METHOD). Returns False when fully spliced.
        Replacing the current method on a non-error pause is refused."""
        raise NotImplementedError

    # --- Action mutation: skip + insert ---

    @abstractmethod
    def skip_pending_action(
        self, thread_id: str,
        action_id: str | None = None, action_command: str | None = None,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def insert_action(
        self, thread_id: str, action_template: ActionTemplate,
        where: InsertPosition,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def replace_action(
        self, thread_id: str, target_command: str, action_template: ActionTemplate,
    ) -> bool:
        """Replace an action. The replacement always runs next (AtHead), ahead
        of other pending actions; the skip is one-shot on the first pending
        consumption of target_command. Pending target: spliced (AtHead + skip).
        Current error-paused action: replacement staged to run next; returns
        True so the caller drops the failed action via
        recover_thread(ABORT_ACTION). Returns False when fully spliced.
        Replacing the current action on a non-error pause is refused."""
        raise NotImplementedError
