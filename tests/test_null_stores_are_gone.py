"""The Null stores, which silently dropped every write, stay deleted.

IStateStore is gone too: crash recovery on a hosted deployment is handled by the
``executions`` table and a boot scan that marks non-terminal runs FAILED_INTERRUPTED.
"""
import pytest


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
            from orca.resource_models.store_interfaces import NullStateStore

            assert NullStateStore is not None

    def test_in_memory_state_store_import_fails(self) -> None:
        """``IStateStore`` and ``InMemoryStateStore`` were removed in the
        state-persistence-rollback (option 3 from the audit). The interface
        was a dead Protocol -- nothing in the runtime ever called
        ``save_execution_state``. A hosted deployment tracks RUNNING/terminal status via
        the ``executions`` table directly.
        """
        with pytest.raises(ImportError):
            from orca.resource_models.store_interfaces import IStateStore

            assert IStateStore is not None
        with pytest.raises(ImportError):
            from orca.resource_models.store_interfaces import InMemoryStateStore

            assert InMemoryStateStore is not None
