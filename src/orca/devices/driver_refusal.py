"""Telling a driver saying "not now" apart from a driver going wrong.

A handler refuses to park while it still holds tips or a plate: it checked its
own state, declined, and moved nothing. The caller waits and asks again. That
is not a failure, and treating it as one stops work that is fine.

A command the device bridge could not act on at all moved nothing either, and
that one is never worth waiting on. The driver says which, so neither caller
has to guess from an exception class name.
"""

from orca.gateway.controller.exceptions import instrument_outcome_of


def is_busy_refusal(exc: BaseException) -> bool:
    """Is this the handler saying "not now" rather than something going wrong?

    Any driver that declares a refusal, so a second driver meaning the same
    thing needs no second case here. Only a refusal: waiting on a command the
    device bridge does not have spends the whole patience budget and then
    reports a handler holding tips that nothing established.
    """
    return instrument_outcome_of(exc).worth_asking_again
