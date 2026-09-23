"""Typed exceptions for runtime mutation precondition failures.

Each error subclasses both `MutationError` (so wire layers can catch the
family with one `except` clause) and `ValueError` (so existing
`except ValueError` callers continue to work). Carrying typed attributes
avoids brittle string-match-on-message in REST/MCP error envelopes.
"""


class MutationError(ValueError):
    """Base class for runtime mutation precondition failures."""


class ThreadNotPausedError(MutationError):
    """Mutation refused because the target thread is not PAUSED.

    Two distinguishable flavors via `terminal`:
    - terminal=True: thread is in a terminal state (COMPLETED, STOPPED,
      ABORTED, FAILED). The message says the thread "cannot accept
      mutations" because it has terminated.
    - terminal=False: thread is in some other non-PAUSED state (RUNNING,
      EXECUTING_ACTION, ...). The message instructs the caller to pause
      first.

    Both cases share `current_status` so wire layers can map to the same
    error code (`thread_not_paused`) while still surfacing the specific
    state to the operator.
    """

    _TERMINAL_STATUSES: frozenset[str] = frozenset(
        {"COMPLETED", "STOPPED", "ABORTED", "FAILED"}
    )

    def __init__(self, thread_name: str, current_status: str) -> None:
        terminal = current_status in self._TERMINAL_STATUSES
        if terminal:
            message = (
                f"Thread {thread_name} has status {current_status} and "
                f"cannot accept mutations"
            )
        else:
            message = (
                f"Thread {thread_name} must be PAUSED to accept mutations "
                f"(current status: {current_status}). Call pause_thread first."
            )
        super().__init__(message)
        self.thread_name = thread_name
        self.current_status = current_status
        self.terminal = terminal


class MethodNotAssignedError(MutationError):
    """Mutation refused because the thread has no assigned method.

    Action mutations (skip_action, insert_action) require an assigned
    method whose lane the action targets. A thread between methods has
    no such lane.
    """

    def __init__(self, thread_name: str) -> None:
        super().__init__(
            f"Thread {thread_name} has no assigned method. "
            f"Action mutation requires an in-progress method."
        )
        self.thread_name = thread_name


class MethodNotInProgressError(MutationError):
    """Action insertion refused because the assigned method is not IN_PROGRESS.

    Insertion targets the assigned method's action_lane. That lane only
    exists once the method is actively running; a queued or completed
    method has no usable lane.
    """

    def __init__(
        self, thread_name: str, method_name: str, current_status: str,
    ) -> None:
        super().__init__(
            f"Thread {thread_name}'s assigned method '{method_name}' has status "
            f"{current_status}, not IN_PROGRESS. Action insertion is only valid "
            f"while a method is actively running."
        )
        self.thread_name = thread_name
        self.method_name = method_name
        self.current_status = current_status


class ActionAlreadyExecutingError(MutationError):
    """skip_action refused because the target action is already executing.

    Use error recovery to abort the running action; skip only applies to
    actions that have not started.
    """

    def __init__(self, action_name: str) -> None:
        super().__init__(
            f"Cannot skip action '{action_name}' - already executing. "
            f"Use error recovery to abort it."
        )
        self.action_name = action_name


class MethodAlreadyInProgressError(MutationError):
    """skip_method refused because the target method is already IN_PROGRESS.

    Methods that have started cannot be skipped; use abort_method instead.
    """

    def __init__(self, method_name: str) -> None:
        super().__init__(
            f"Cannot skip method '{method_name}' - already IN_PROGRESS. "
            f"Use abort_method() instead."
        )
        self.method_name = method_name


class ReplacementSharesTargetNameError(MutationError):
    """replace refused on the pending path because the replacement shares the
    target's name/command.

    The drop is a one-shot, name-keyed skip; if the substitute has the same
    name it is the first match and gets skipped instead of the original, so the
    replace would silently no-op. Give the replacement a different name.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"Replacement shares the target name '{name}'; the one-shot skip "
            f"would drop the replacement, not the original. Give the "
            f"replacement a different name."
        )
        self.name = name


class MutationLeavesInputUnassignedError(MutationError):
    """insert_action/replace_action refused because the built action still has
    a declared input with no labware assigned.

    Wiring assigns from every thread currently participating in the target
    method (`ExecutingMethod.participating_thread_ids`). A slot stays
    unassigned when no current participant's labware template matches it --
    typically a co-thread that has not joined this method instance yet.
    Refusing here turns that into an immediate, named error instead of a
    silent AWAITING_CO_THREADS deadlock with no owning thread to report.
    """

    def __init__(self, action_command: str, unassigned_names: list[str]) -> None:
        super().__init__(
            f"Cannot wire action '{action_command}': no participating thread "
            f"supplies labware for {', '.join(unassigned_names)}. Every "
            f"declared input must be covered by a thread currently in this "
            f"method before the mutation can be applied."
        )
        self.action_command = action_command
        self.unassigned_names = unassigned_names


class CannotReplaceCurrentMethodError(MutationError):
    """replace_method refused because the target is the thread's current method
    and the thread is NOT error-paused.

    The current method has already left the method lane, so it cannot be
    skip-replaced, and only an error pause has the recovery decision that the
    staged path hands back. On a manual pause, abort the method and insert a
    replacement instead.
    """

    def __init__(self, method_name: str) -> None:
        super().__init__(
            f"Cannot replace the current method '{method_name}' on a thread that "
            f"is not error-paused; it has already started. Abort it with "
            f"abort_method and insert a replacement, or replace it while the "
            f"thread is error-paused (which stages the substitute for "
            f"recover_thread)."
        )
        self.method_name = method_name
