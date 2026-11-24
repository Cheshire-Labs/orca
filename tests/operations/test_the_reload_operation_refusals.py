"""What a caller of POST /api/runtime/reload receives when the reload is refused or fails. Both servers serve it."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from orca.operations.protocol import OperationError
from orca.operations.system_loading import ReloadRuntimeOperation
from orca.operations.wire_models import ReloadRequest
from orca.runtime.package_import import PackageLoadError
from orca.runtime.runtime_holder import RuntimeBusyError
from orca.runtime.runtime_interface import ISystemRuntime


@dataclass
class _Holder:
    active: bool = False
    failure: Exception | None = None
    building: bool = False

    @property
    def package_dir(self) -> Path:
        return Path("package")

    @property
    def is_building(self) -> bool:
        return self.building

    @property
    def runtime(self) -> ISystemRuntime:
        raise AssertionError("a refused reload never reads the runtime")

    def has_active_executions(self) -> bool:
        return self.active

    async def reload(self, *, reason: str | None = None, confirm: bool = False) -> None:
        if self.failure is not None:
            raise self.failure


async def _no_revision() -> str | None:
    return None


async def _reload_refusal(holder: _Holder) -> OperationError:
    with pytest.raises(OperationError) as refused:
        await ReloadRuntimeOperation(holder=holder, revision=_no_revision).run(ReloadRequest())
    return refused.value


async def test_reload_is_refused_during_a_run() -> None:
    err = await _reload_refusal(_Holder(active=True))
    assert err.http_status == 409
    assert err.surface_code() == "active_executions_exist"


async def test_a_run_that_starts_during_the_reload_is_the_same_refusal() -> None:
    err = await _reload_refusal(_Holder(failure=RuntimeBusyError("busy")))
    assert err.http_status == 409
    assert err.surface_code() == "active_executions_exist"


async def test_a_broken_package_answers_rebuild_failed() -> None:
    err = await _reload_refusal(_Holder(failure=PackageLoadError("bad import")))
    assert err.http_status == 400
    assert err.surface_code() == "rebuild_failed"


async def test_reload_is_refused_while_a_mount_is_in_progress() -> None:
    """A reload during a mount would build a second runtime over the one the mount is still starting."""
    err = await _reload_refusal(_Holder(building=True))
    assert err.http_status == 409
    assert err.surface_code() == "conflict"
    assert "mount" in err.message
