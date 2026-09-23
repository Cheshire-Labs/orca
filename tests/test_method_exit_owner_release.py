"""Reservation-owner guard on METHOD_EXIT.

Mirror of the STOP_REQUESTED guard at the same site: a thread reaching
METHOD_EXIT releases the device reservation only if it owns it (by
``reservation.thread_id``), never one held by another thread. Under the
authoritative-resolution model contributors follow the driver's slot
outcome and never reach this co-labware branch, so the thread here is the
owner path; the guard stays as the safe default against dropping a
reservation this thread does not hold.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

from orca.workflow_models.labware_threads.executing_labware_thread import (
    ExecutingLabwareThread,
)


pytestmark = pytest.mark.asyncio


def _build_thread_under_test(reservation_thread_id: str) -> tuple[
    ExecutingLabwareThread, MagicMock, list[bool],
]:
    """Hand-construct an ExecutingLabwareThread with the minimum state
    needed to drive ``_handle_thread_at_assigned_action_location`` to
    the METHOD_EXIT branch.

    Returns (thread, mock_action, release_calls). ``mock_action.action.reservation
    .thread_id`` is ``reservation_thread_id``; the thread's own id is
    fixed to ``contributor-thread`` so equality controls owner vs
    contributor.
    """
    thread = ExecutingLabwareThread.__new__(ExecutingLabwareThread)

    inner_thread = MagicMock()
    inner_thread.id = "contributor-thread"
    inner_thread.name = "contributor-thread"
    thread._thread = inner_thread

    thread._stop_event = asyncio.Event()
    thread._pause_request_event = asyncio.Event()
    thread._site_residency = None

    # Flat model: the thread sits on the device's site node whose
    # owner_mutex_id matches the action's mutex location position_id.
    location = MagicMock()
    location.name = "device_loc/slot"
    location.owner_mutex_id = "device_loc"

    release_calls: list[bool] = []
    mock_action = MagicMock()
    mock_action.action.location.position_id = "device_loc"
    mock_action.action.location.name = "device_loc"
    mock_action.action.reservation.thread_id = reservation_thread_id
    mock_action.action.release_reservation = MagicMock(
        side_effect=lambda: release_calls.append(True)
    )
    mock_action.action.all_labware_is_present = asyncio.Event()
    thread._assigned_action = mock_action

    mock_method = MagicMock()
    mock_method.exit_signal = asyncio.Event()
    mock_method.exit_signal.set()  # fast-path METHOD_EXIT
    mock_method.was_aborted = True
    mock_method.name = "shared_method"
    # Owner path: contributors follow the slot outcome and never reach the
    # co-labware METHOD_EXIT branch this test drives.
    mock_method.shared_coord.is_contributor.return_value = False
    thread._assigned_method = mock_method

    location_service = MagicMock()
    location_service.get.return_value = location
    thread._labware_location_service = location_service

    coordination_config = MagicMock()
    thread._coordination_config = coordination_config

    thread._fire = MagicMock()

    return thread, mock_action, release_calls


async def test_method_exit_on_contributor_does_not_release_reservation() -> None:
    thread, _, release_calls = _build_thread_under_test(
        reservation_thread_id="owner-thread",
    )

    await thread._handle_thread_at_assigned_action_location()

    assert release_calls == [], (
        "Contributor's METHOD_EXIT must not drop the owner's reservation"
    )
    assert thread._assigned_action is None


async def test_method_exit_on_owner_releases_reservation() -> None:
    thread, _, release_calls = _build_thread_under_test(
        reservation_thread_id="contributor-thread",
    )

    await thread._handle_thread_at_assigned_action_location()

    assert release_calls == [True], (
        "Owner's METHOD_EXIT must release the device reservation"
    )
    assert thread._assigned_action is None
