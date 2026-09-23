"""SystemRuntime defaults to InMemoryLabwareStore.

An audit replaced the prior NullLabwareStore default, which silently dropped
every write. Misconfigured deployments now persist to RAM (durable for the
runtime's lifetime) and expose state via the same query surfaces a real
DB-backed store would. Pin that contract so a future change
can't quietly regress to a Null shape.

Audit follow-up 2026-05-01 (state-persistence-rollback): IStateStore was
removed entirely. The runtime no longer offers an execution-snapshot
persistence interface; crash recovery on a hosted deployment is handled by the
``executions`` table + boot scan that marks non-terminal runs FAILED_INTERRUPTED.
"""
from unittest.mock import MagicMock

import pytest

from orca.runtime.labware_store import InMemoryLabwareStore
from orca.runtime.system_runtime import SystemRuntime
from orca.system.system_interface import ISystem


def _stub_system() -> ISystem:
    system = MagicMock(spec=ISystem)
    system.event_bus = MagicMock()
    system.system_map = MagicMock()
    system.devices = []
    system.labwares = []
    return system


class TestSystemRuntimeDefaultStores:
    def test_default_labware_store_is_in_memory(self) -> None:
        runtime = SystemRuntime(_stub_system())
        assert isinstance(runtime._labware_store, InMemoryLabwareStore)


class TestNullStoresAreGone:
    """``NullLabwareStore`` and ``NullStateStore`` were deleted. Importing them
    should fail loudly so any place that still references them gets caught at
    import time rather than silently dropping data again.
    """

    def test_null_labware_store_import_fails(self) -> None:
        with pytest.raises(ImportError):
            from orca.runtime.labware_store import NullLabwareStore

            assert NullLabwareStore is not None

    def test_null_state_store_import_fails(self) -> None:
        with pytest.raises(ImportError):
            from orca.runtime.interfaces import NullStateStore

            assert NullStateStore is not None

    def test_in_memory_state_store_import_fails(self) -> None:
        """``IStateStore`` and ``InMemoryStateStore`` were removed in the
        state-persistence-rollback (option 3 from the audit). The interface
        was a dead Protocol -- nothing in the runtime ever called
        ``save_execution_state``. A hosted deployment tracks RUNNING/terminal status via
        the ``executions`` table directly.
        """
        with pytest.raises(ImportError):
            from orca.runtime.interfaces import IStateStore

            assert IStateStore is not None
        with pytest.raises(ImportError):
            from orca.runtime.interfaces import InMemoryStateStore

            assert InMemoryStateStore is not None
