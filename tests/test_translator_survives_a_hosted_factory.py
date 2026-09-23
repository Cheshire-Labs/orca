"""A translator stays a translator when the deployment binds its own factory.

``single_carriage`` is read off the transporter's live driver, and the driver
is chosen by whichever device factory is bound at topology-build time. A
hosted deployment binds its own factory, which replaces the one the
deployment package declared -- so a topology that got its translator driver
from a bespoke package-local factory silently came up with a generic arm
driver instead, no exclusion group, and the reservation layer free to promise
both endpoints of one physical carriage to two different plates. That is the
head-on bridge collision ``test_single_carriage_transporter`` was written to
prevent, arriving through the one path none of its tests covered.

Declaring the translator as a ``Translator`` resource rather than a
``Transporter`` moves the fact into the topology, where every factory has to
honour it: the device kind picks the driver pair, so no factory can hand a
translator an arm driver by omission.
"""
import importlib

import pytest

from orca.resource_models.plate_pad import PlatePad
from orca.resource_models.translator import Translator
from orca.resource_models.transporter import Transporter
from orca.runtime.device_factory import SimDeviceFactory
from orca.runtime.device_factory_context import use_device_factory
from orca.runtime.store_factory import InMemoryRuntimeStoreFactory
from orca.sdk.build import build_system
from tests.test_helpers import (
    assert_translator_carriage_pairs,
    create_simple_system_map,
    create_test_teachpoints,
    seeded_teachpoint_service,
)


def _bridge_pads() -> dict[str, PlatePad]:
    return {
        "t1_start": PlatePad("t1_start", supports_deadlock_resolution=False),
        "t1_end": PlatePad("t1_end", supports_deadlock_resolution=False),
        "pad_1": PlatePad("pad_1"),
    }


def _build_bridge(bind_hosted_factory: bool) -> tuple[Translator, Transporter]:
    """One translator plus one arm, built with or without an outer factory."""
    factory = use_device_factory(SimDeviceFactory())
    if bind_hosted_factory:
        with factory:
            return _bridge_resources()
    return _bridge_resources()


def _bridge_resources() -> tuple[Translator, Transporter]:
    translator = Translator(
        "translator_1",
        teachpoint_store=seeded_teachpoint_service(create_test_teachpoints(["t1_start", "t1_end"])),
    )
    arm = Transporter(
        "arm",
        teachpoint_store=seeded_teachpoint_service(create_test_teachpoints(["pad_1", "t1_start", "t1_end"])),
    )
    return translator, arm


class TestTranslatorKeepsItsCarriageGroup:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bind_hosted_factory", [False, True])
    async def test_a_translator_registers_its_carriage_group(
        self, bind_hosted_factory: bool
    ) -> None:
        translator, arm = _build_bridge(bind_hosted_factory)
        _registry, system_map = await create_simple_system_map(
            [translator, arm], {}, _bridge_pads()
        )

        siblings = system_map.exclusion_siblings_of("t1_start")
        assert [loc.position_id for loc in siblings] == ["t1_end"]

    @pytest.mark.asyncio
    async def test_a_plain_transporter_is_still_an_arm(self) -> None:
        arm = Transporter(
            "arm",
            teachpoint_store=seeded_teachpoint_service(create_test_teachpoints(["t1_start", "t1_end", "pad_1"])),
        )
        _registry, system_map = await create_simple_system_map([arm], {}, _bridge_pads())
        assert system_map.exclusion_siblings_of("t1_start") == []


class TestSmcExamplesUnderAHostedFactory:
    """The N=6 wedge: a hosted deployment binds its own factory over the example's.

    The existing drift guard runs only on the default build, so it could not
    see a bridge that came up as an arm the moment a host bound a factory.
    """

    @pytest.mark.parametrize(
        "module_path",
        [
            "examples.smc_assay.topology",
            "examples.hamilton_smc.topology",
            "examples.opentrons_flex_smc.topology",
        ],
    )
    def test_the_bridges_declare_one_carriage(self, module_path: str) -> None:
        build_topology = importlib.import_module(module_path).build_topology

        with use_device_factory(SimDeviceFactory()):
            topology = build_topology(InMemoryRuntimeStoreFactory())

        bridges = {
            t.name: t.single_carriage
            for t in topology.transporters
            if t.name.startswith("translator_")
        }
        assert bridges == {"translator_1": True, "translator_2": True}

    @pytest.mark.asyncio
    async def test_the_system_map_registers_the_bridge_groups(self) -> None:
        from examples.smc_assay.topology import build_topology

        stores = InMemoryRuntimeStoreFactory()
        with use_device_factory(SimDeviceFactory()):
            topology = build_topology(stores)
        build = await build_system(
            "hosted_smc", topology, stores, configure_logging=False
        )

        assert_translator_carriage_pairs(build.system.system_map)
