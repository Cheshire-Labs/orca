"""Unit tests for SharedMethodCoordination contributor protocol."""
import asyncio

import pytest

from orca.workflow_models.shared_method_coordination import SharedMethodCoordination


def test_new_coord_has_no_contributors() -> None:
    coord = SharedMethodCoordination()
    assert coord.is_contributor("any-id") is False


def test_add_contributor() -> None:
    coord = SharedMethodCoordination()
    coord.add_contributor("thread-a")
    assert coord.is_contributor("thread-a") is True
    assert coord.is_contributor("thread-b") is False


def test_remove_contributor() -> None:
    coord = SharedMethodCoordination()
    coord.add_contributor("thread-a")
    coord.remove_contributor("thread-a")
    assert coord.is_contributor("thread-a") is False


def test_remove_nonexistent_contributor_is_noop() -> None:
    coord = SharedMethodCoordination()
    coord.remove_contributor("thread-a")
    assert coord.is_contributor("thread-a") is False


def test_add_contributor_is_idempotent() -> None:
    coord = SharedMethodCoordination()
    coord.add_contributor("thread-a")
    coord.add_contributor("thread-a")
    coord.remove_contributor("thread-a")
    assert coord.is_contributor("thread-a") is False


def test_multiple_contributors() -> None:
    coord = SharedMethodCoordination()
    coord.add_contributor("thread-a")
    coord.add_contributor("thread-b")
    assert coord.is_contributor("thread-a") is True
    assert coord.is_contributor("thread-b") is True
    coord.remove_contributor("thread-a")
    assert coord.is_contributor("thread-a") is False
    assert coord.is_contributor("thread-b") is True


@pytest.mark.asyncio
async def test_contributor_context_manager_registers_and_cleans_up() -> None:
    coord = SharedMethodCoordination()
    assert coord.is_contributor("thread-a") is False
    async with coord.contributor("thread-a"):
        assert coord.is_contributor("thread-a") is True
    assert coord.is_contributor("thread-a") is False


@pytest.mark.asyncio
async def test_contributor_context_manager_cleans_up_on_exception() -> None:
    coord = SharedMethodCoordination()

    class _RaisedInside(Exception):
        pass

    with pytest.raises(_RaisedInside):
        async with coord.contributor("thread-a"):
            assert coord.is_contributor("thread-a") is True
            raise _RaisedInside()
    assert coord.is_contributor("thread-a") is False


@pytest.mark.asyncio
async def test_resolving_action_lock_is_an_asyncio_lock() -> None:
    coord = SharedMethodCoordination()
    async with coord.resolving_action_lock:
        assert coord.resolving_action_lock.locked()
    assert not coord.resolving_action_lock.locked()
