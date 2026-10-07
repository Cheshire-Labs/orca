"""A Venus run end to end, down to a fake HxRun.exe, in both places Venus can run.

- Through the device bridge: a fake controller plays orca-client and hands each
  wire command to a real `VenusProtocolDriver`, the way orca-client does.
- On the Hamilton PC itself: orca runs with no device bridge, and Venus builds
  that driver from its own arguments.

Either way the arm puts the plate on a Venus site, the pick/place hook methods
run with the plate and the site, the action's method runs with its values, a
failed method stops the plate, and the volumes and tips the action declares land
in the labware record only when the method succeeds.
"""
import os
import pathlib
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, Optional, cast

import pytest
from pydantic import BaseModel, JsonValue

import orca.orca as orca
from cheshire_drivers import CartesianCoordinates as C
from cheshire_drivers import Teachpoint
from cheshire_drivers.driver_errors import outcome_of
from cheshire_drivers.protocol_runner_request_validation import wrap_protocol_runner_payload
from cheshire_drivers.sims import SimStorageDriver, SimTransporterDriver
from cheshire_drivers.venus_driver import VenusProtocolDriver
from orca.devices.devices import Storage
from orca.devices.venus import Venus
from orca.gateway.controller.controller import DeviceController
from orca.gateway.controller.exceptions import CommandExecutionError
from orca.gateway.remote_device_factory import RemoteDeviceFactory
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory import SimDeviceFactory
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.device_factory_protocol import DriverPairElement
from orca.runtime.registries import NullGatewayRegistry
from orca.runtime.run_modes import WorkflowRunMode
from orca.runtime.status_models import ConnectionCard
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.runtime.system_runtime import SystemRuntime
from orca.sdk.build import SystemBuild, Topology
from orca.sdk.labware import PlateTemplate, TipRackTemplate
from orca.spawn import DISPENSE, LEAVE_IN_PLACE, REUSE_EXISTING
from orca.state.records import DeclaredTracking, DeclaredVolumeTransfer, LabwareInitialState
from orca.workflow_models.action_context import ActionContext
from orca.workflow_models.method_context import MethodContext
from orca.workflow_models.thread_context import ThreadContext
from tests.runtime.registries.test_device_registry import FakeConnectionSource
from tests.test_helpers import run_to_quiescence, wait_for_paused_thread

_FAKE_HXRUN = """
import json, os, pathlib, sys
params = pathlib.Path(os.environ["FAKE_HXRUN_PARAMS"])
record = {"method": pathlib.Path(sys.argv[2]).name, "params": json.loads(params.read_text())["params"]}
with open(os.environ["FAKE_HXRUN_LOG"], "a") as log:
    log.write(json.dumps(record) + "\\n")
if pathlib.Path(sys.argv[2]).name == os.environ.get("FAKE_HXRUN_FAILS", ""):
    sys.stderr.write("Error 3: liquid level not found")
    sys.exit(3)
"""



class Where(Enum):
    THROUGH_THE_BRIDGE = "through the bridge"
    ON_THE_HAMILTON_PC = "on the Hamilton PC"


class _HxRunCall(BaseModel):
    method: str
    params: dict[str, JsonValue]


class _FakeHamiltonPc:
    """The Hamilton PC: a methods folder, HxRun.exe, and what HxRun was asked to run."""

    def __init__(self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.methods = tmp_path / "Methods"
        self.methods.mkdir()
        for method in ("PrepPlace.hsl", "Placed.hsl", "PrepPick.hsl", "Picked.hsl", "AddBuffer.hsl"):
            (self.methods / method).write_text("", encoding="utf-8")
        (tmp_path / "Temp").mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "Temp"))
        monkeypatch.setenv(
            "FAKE_HXRUN_PARAMS", str(tmp_path / "Temp" / "CheshireLabs" / "Orca" / "actionConfig.json"))
        self.log = tmp_path / "hxrun.log"
        monkeypatch.setenv("FAKE_HXRUN_LOG", str(self.log))
        self.exe = self._write_fake_hxrun(tmp_path)

    def _write_fake_hxrun(self, tmp_path: pathlib.Path) -> pathlib.Path:
        if os.name == "nt":
            script = tmp_path / "fake_hxrun.py"
            script.write_text(_FAKE_HXRUN, encoding="utf-8")
            exe = tmp_path / "HxRun.cmd"
            exe.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        else:
            exe = tmp_path / "HxRun"
            exe.write_text(f"#!{sys.executable}\n{_FAKE_HXRUN}", encoding="utf-8")
            exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        return exe

    def orca_client_driver(self) -> VenusProtocolDriver:
        return VenusProtocolDriver(
            "ml_star", exe_path=str(self.exe), methods_folder=str(self.methods),
            prepare_place_protocol="PrepPlace.hsl", placed_protocol="Placed.hsl",
            prepare_pick_protocol="PrepPick.hsl", picked_protocol="Picked.hsl",
        )

    def venus_on_this_pc(self) -> Venus:
        return Venus(
            "ml_star", site_names=["sample_site", "reservoir_site", "tips_site"],
            exe_path=str(self.exe), methods_folder=str(self.methods),
            prepare_place_protocol="PrepPlace.hsl", placed_protocol="Placed.hsl",
            prepare_pick_protocol="PrepPick.hsl", picked_protocol="Picked.hsl",
        )

    def runs(self) -> list[_HxRunCall]:
        if not self.log.exists():
            return []
        return [_HxRunCall.model_validate_json(line) for line in self.log.read_text().splitlines()]


@dataclass
class _OrcaClient:
    """Plays orca-client: each wire command reaches the Venus driver as orca-client delivers it."""

    venus: VenusProtocolDriver

    async def execute_command(
        self,
        device_id: str,
        command: str,
        params: Optional[Dict[str, Any]] = None,
        timeout_seconds: Optional[float] = None,
        effective_mode: WorkflowRunMode = WorkflowRunMode.LIVE,
        resend_on_reconnect: bool = True,
        execution_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        kwargs = wrap_protocol_runner_payload(command, params or {})
        try:
            await getattr(self.venus, command)(**kwargs)
        except Exception as error:
            raise CommandExecutionError(str(error), type(error).__name__, outcome_of(error)) from error
        return {"success": True}


class _VenusOnTheBridge:
    """Venus over the wire; every other device a local sim."""

    def __init__(self, orca_client: _OrcaClient) -> None:
        self._remote = RemoteDeviceFactory(
            controller=cast(DeviceController, orca_client),
            default_timeout=30.0,
            mode_resolver=lambda _name: WorkflowRunMode.LIVE,
            profile_source=lambda name: VenusProtocolDriver.interfaces if name == "ml_star" else None,
        )
        self._sim = SimDeviceFactory()

    def build_drivers(
        self, device_type: str, name: str, *, deck_modeling: bool = False,
    ) -> tuple[DriverPairElement, DriverPairElement]:
        if name == "ml_star":
            return self._remote.build_drivers(device_type, name)
        return self._sim.build_drivers(device_type, name, deck_modeling=deck_modeling)


def _connected() -> FakeConnectionSource:
    now = datetime.now(timezone.utc)
    advertised = {
        "ml_star": VenusProtocolDriver.interfaces,
        "stacker": SimStorageDriver.interfaces,
        "waste": SimStorageDriver.interfaces,
        "arm": SimTransporterDriver.interfaces,
    }
    return FakeConnectionSource([
        ConnectionCard(
            name=name, client_id="hamilton-pc", connection_id=f"conn-{name}",
            last_heartbeat=now, advertised_kind="test", advertised_interfaces=interfaces,
        )
        for name, interfaces in advertised.items()
    ], now=now)


@pytest.fixture
def hamilton_pc(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> _FakeHamiltonPc:
    return _FakeHamiltonPc(tmp_path, monkeypatch)


def _devices(hamilton_pc: _FakeHamiltonPc, where: Where, stores: InMemoryRuntimeStoreFactory) -> tuple[Venus, Storage, Storage, Transporter]:
    def build() -> tuple[Venus, Storage, Storage, Transporter]:
        venus = (
            Venus("ml_star", site_names=["sample_site", "reservoir_site", "tips_site"])
            if where is Where.THROUGH_THE_BRIDGE else hamilton_pc.venus_on_this_pc()
        )
        arm = Transporter(
            "arm",
            teachpoint_store=stores.teachpoints("arm", seed=[
                Teachpoint("stacker", C(0, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("ml_star/sample_site", C(200, 200, 300, 0, 90, 180), orientation="right"),
                Teachpoint("waste", C(400, 200, 300, 0, 90, 180), orientation="right"),
            ]),
        )
        return venus, Storage("stacker"), Storage("waste"), arm

    if where is Where.ON_THE_HAMILTON_PC:
        return build()
    with use_device_factory(_VenusOnTheBridge(_OrcaClient(hamilton_pc.orca_client_driver()))):
        return build()


async def _build(hamilton_pc: _FakeHamiltonPc, where: Where) -> SystemBuild:
    stores = InMemoryRuntimeStoreFactory()
    sample_plate = PlateTemplate(
        "sample_plate", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        initial_state=LabwareInitialState(uniform_volume=0.0),
    )
    reservoir = PlateTemplate(
        "reservoir", labware_type="Cor_Falcon_96_wellplate_340ul_Fb_Black",
        initial_state=LabwareInitialState(uniform_volume=300.0),
    )
    tips = TipRackTemplate("tips", labware_type="hamilton_96_tiprack_10uL_filter", with_tips=True)
    ml_star, stacker, waste, arm = _devices(hamilton_pc, where, stores)

    @orca.action(
        device=ml_star,
        inputs=[sample_plate, reservoir, tips],
        deck_positions={sample_plate: "sample_site", reservoir: "reservoir_site", tips: "tips_site"},
        declares=DeclaredTracking(
            volume_transferred=[DeclaredVolumeTransfer(
                source="reservoir", target="sample_plate", volume_ul=50.0,
                source_wells=["A1", "B1", "C1"], target_wells=["A1", "B1", "C1"],
            )],
            tips_used={"tips": ["A1", "B1", "C1"]},
        ),
    )
    async def add_buffer(ctx: ActionContext) -> None:
        await ctx.device().run_protocol("AddBuffer.hsl", {"vol": 50, "buffer": "PBS"})

    @orca.method
    async def add_buffer_method(ctx: MethodContext):
        yield add_buffer

    @orca.thread(labware=sample_plate, start=("stacker", DISPENSE), end=("waste", LEAVE_IN_PLACE))
    async def plate_journey(ctx: ThreadContext):
        yield add_buffer_method

    @orca.thread(
        labware=reservoir,
        start=("ml_star/reservoir_site", REUSE_EXISTING),
        end=("ml_star/reservoir_site", LEAVE_IN_PLACE),
    )
    async def reservoir_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add_buffer_method])

    @orca.thread(
        labware=tips,
        start=("ml_star/tips_site", REUSE_EXISTING),
        end=("ml_star/tips_site", LEAVE_IN_PLACE),
    )
    async def tips_journey(ctx: ThreadContext):
        while ctx.has_more_work():
            yield orca.join(allows=[add_buffer_method])

    @orca.workflow(name="venus_buffer_wf")
    def workflow(wf):
        wf.start(plate_journey)
        wf.thread(reservoir_journey)
        wf.thread(tips_journey)

    topology = Topology(
        locations={"stacker": stacker, "ml_star": ml_star, "waste": waste},
        transporters=[arm],
    )
    return await orca.build_system(name="Venus Buffer", workflow=workflow, topology=topology, stores=stores)


async def _submit(build: SystemBuild, where: Where, mode: WorkflowRunMode) -> tuple[SystemRuntime, str]:
    """Through the bridge, the way a hosted deployment and the daemon submit; on the PC, the way `SystemBuild.run` does."""
    if where is Where.THROUGH_THE_BRIDGE:
        runtime = SystemRuntime(
            build.system, event_bus=build.event_bus,
            gateway_registry=NullGatewayRegistry(), connection_source=_connected(),
        )
        await runtime.start()
        return runtime, (await runtime.submit_workflow("venus_buffer_wf", mode=mode)).id
    runtime = SystemRuntime(build.system, event_bus=build.event_bus)
    await runtime.start()
    assert build.workflow is not None
    return runtime, (await runtime.submit(build.workflow, mode=mode)).execution_id


def _labware_id(build: SystemBuild, template_name: str) -> str:
    return next(lw.id for lw in build.system.labwares if lw.template_name == template_name)


async def _wells(runtime: SystemRuntime, build: SystemBuild, template_name: str) -> dict[str, float]:
    """A well the record holds nothing for is empty: a plate declared empty records no wells."""
    volumes = (await runtime.labware.get_well_volumes(_labware_id(build, template_name))).volumes
    return {well: volumes.get(well, 0.0) for well in ("A1", "B1", "C1", "D1")}


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.parametrize("where", list(Where))
async def test_the_hooks_and_the_method_run_on_the_hamilton_pc_with_the_plate_and_its_site(
    hamilton_pc: _FakeHamiltonPc, where: Where,
) -> None:
    build = await _build(hamilton_pc, where)
    runtime, execution_id = await _submit(build, where, WorkflowRunMode.LIVE)
    statuses = await run_to_quiescence(runtime, execution_id)

    assert statuses and all(s == "COMPLETED" for s in statuses.values()), statuses
    calls = hamilton_pc.runs()
    assert [c.method for c in calls] == [
        "PrepPlace.hsl", "Placed.hsl", "AddBuffer.hsl", "PrepPick.hsl", "Picked.hsl",
    ]
    placed = calls[1].params
    assert placed["action"] == "notify_placed"
    assert placed["site"] == "sample_site"
    assert placed["labware_type"] == "Cor_Falcon_96_wellplate_340ul_Fb_Black"
    assert str(placed["labware_name"]).startswith("sample_plate")
    assert calls[2].params == {"vol": 50, "buffer": "PBS", "action": "run"}


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.parametrize("where, mode", [
    (Where.THROUGH_THE_BRIDGE, WorkflowRunMode.LIVE),
    (Where.THROUGH_THE_BRIDGE, WorkflowRunMode.PURE_SIM),
    (Where.ON_THE_HAMILTON_PC, WorkflowRunMode.LIVE),
    (Where.ON_THE_HAMILTON_PC, WorkflowRunMode.PURE_SIM),
])
async def test_the_declared_volumes_and_tips_land_in_the_labware_record(
    hamilton_pc: _FakeHamiltonPc, where: Where, mode: WorkflowRunMode,
) -> None:
    build = await _build(hamilton_pc, where)
    runtime, execution_id = await _submit(build, where, mode)
    statuses = await run_to_quiescence(runtime, execution_id)

    assert statuses and all(s == "COMPLETED" for s in statuses.values()), statuses
    assert await _wells(runtime, build, "sample_plate") == {"A1": 50.0, "B1": 50.0, "C1": 50.0, "D1": 0.0}
    assert await _wells(runtime, build, "reservoir") == {"A1": 250.0, "B1": 250.0, "C1": 250.0, "D1": 300.0}
    tips = await runtime.labware.get_tip_state(_labware_id(build, "tips"))
    assert {"A1", "B1", "C1"}.isdisjoint(tips.positions_present)
    assert len(tips.positions_present) == 93


@pytest.mark.slow
@pytest.mark.asyncio
@pytest.mark.parametrize("where", list(Where))
async def test_a_failed_venus_method_pauses_the_plate_and_records_nothing(
    hamilton_pc: _FakeHamiltonPc, monkeypatch: pytest.MonkeyPatch, where: Where,
) -> None:
    monkeypatch.setenv("FAKE_HXRUN_FAILS", "AddBuffer.hsl")
    build = await _build(hamilton_pc, where)
    runtime, execution_id = await _submit(build, where, WorkflowRunMode.LIVE)
    paused = await wait_for_paused_thread(runtime, execution_id, timeout=30.0)

    assert paused.pause_reason == "error"
    assert paused.last_error is not None
    assert "exit code 3" in paused.last_error
    assert "liquid level not found" in paused.last_error
    assert await _wells(runtime, build, "sample_plate") == {"A1": 0.0, "B1": 0.0, "C1": 0.0, "D1": 0.0}
    assert await _wells(runtime, build, "reservoir") == {"A1": 300.0, "B1": 300.0, "C1": 300.0, "D1": 300.0}
    tips = await runtime.labware.get_tip_state(_labware_id(build, "tips"))
    assert len(tips.positions_present) == 96
    await runtime.shutdown()


@pytest.mark.parametrize("make_venus, named", [
    (lambda: Venus("ml_star", methods_folder="D:/Methods", placed_protocol="Placed.hsl"),
     "methods_folder, placed_protocol"),
    (lambda: Venus("ml_star", exe_path=r"C:\Program Files (x86)\HAMILTON\Bin\HxRun.exe"), "exe_path"),
])
def test_a_venus_under_a_device_bridge_refuses_every_hamilton_pc_setting_it_is_given(
    hamilton_pc: _FakeHamiltonPc, make_venus: Callable[[], Venus], named: str,
) -> None:
    """Under a bridge, orca-client's settings are the ones that run; a second copy here would be ignored."""
    with use_device_factory(_VenusOnTheBridge(_OrcaClient(hamilton_pc.orca_client_driver()))):
        with pytest.raises(ValueError, match=f"reads {named} from orca-client"):
            make_venus()


def test_a_venus_given_settings_under_a_local_factory_drives_hxrun_on_this_pc(
    hamilton_pc: _FakeHamiltonPc,
) -> None:
    """A local factory (a sim default or a test's) is not a bridge, so the settings still apply."""
    with use_device_factory(SimDeviceFactory()):
        venus = Venus("ml_star", exe_path=str(hamilton_pc.exe), methods_folder=str(hamilton_pc.methods))
        bare = Venus("ml_star_2")

    assert isinstance(venus.live_driver, VenusProtocolDriver)
    assert not isinstance(bare.live_driver, VenusProtocolDriver)
