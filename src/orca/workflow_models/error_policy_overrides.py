"""Marker base for exceptions that override the method-author's FailurePolicy.

The action-error handler in ``ExecutingLabwareThread`` normally reads
``FailurePolicy`` off the executing method (ABORT vs PAUSE) and branches.
That binary assumes every exception is a *method failure* where the
workflow author's declared intent should govern recovery.

Some exceptions don't fit that frame. They aren't method failures at
all -- some other layer (a gateway holding a device, an operator hitting
halt, a maintenance window) has taken state that prevents the engine
from continuing the in-flight method right now. The method author's
``FailurePolicy.ABORT`` declaration is not a sensible response to "the
operator is troubleshooting on this device"; killing the thread would
discard work the operator intends to resume.

For these *external coordination signals*, recovery must go through the
operator (PAUSE -> RETRY/SKIP/ABORT_THREAD) regardless of what the
method declared. ``OverrideWithPauseError`` is the marker base; any
concrete exception that opts into PAUSE-override-ABORT semantics
multiple-inherits from this class.

The action-error handler checks ``isinstance(error, OverrideWithPauseError)``
at every policy-ABORT branch and routes to PAUSE when True. The four
categories are method failures, external coordination signals, engine
bugs and cancellation; only the first is the workflow author's to declare.
"""


class OverrideWithPauseError(Exception):
    """Marker base for exceptions that force PAUSE over the declared FailurePolicy.

    Subclass via multiple inheritance:

        class DeviceUnderExternalControlError(DeviceError, OverrideWithPauseError):
            ...

    The action-error handler treats any exception in this category as a
    *coordination signal*, not a method failure. PAUSE-for-operator-review
    is the conservative default: stop, surface the state, let a human
    decide. Auto-retry is deliberately out of scope; operator pacing is
    the design.
    """
    pass
