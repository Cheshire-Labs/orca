"""Structural tests for the SMC assay system built via Python SDK.

Verifies build_smc() produces the expected topology (device counts,
transporters, thread templates, resource pools).

Sim execution coverage of the SMC assay lives in test_sdk_smc_assay.py,
which exercises the same build_smc() output end-to-end with stronger
per-thread method-order and labware-journey assertions.
"""

import pytest

from examples.smc_assay.smc_assay_example import build_smc, SmcAssayBuild


@pytest.fixture
async def smc() -> SmcAssayBuild:
    return await build_smc()


class TestSystemStructure:
    """Verify the built system has the correct topology."""

    def test_builds_without_error(self, smc: SmcAssayBuild) -> None:
        assert smc.system is not None
        assert smc.workflow is not None

    def test_device_count(self, smc: SmcAssayBuild) -> None:
        devices = smc.system.devices
        # 2 plate_washers + 2 liquid_handlers + 1 sealer + 1 centrifuge + 8 storages
        # + 1 delidder + 1 reader + 10 shakers + 2 waste (hotel = pads, not a device)
        assert len(devices) == 28

    def test_transporter_count(self, smc: SmcAssayBuild) -> None:
        transporters = smc.system.transporters
        # 5 transporters: ddr_1, ddr_2, ddr_3, translator_1, translator_2
        assert len(transporters) == 5

    def test_workflow_thread_count(self, smc: SmcAssayBuild) -> None:
        assert smc.workflow is not None
        threads = smc.workflow.thread_templates
        # plate_1, sample_plate, neut_plate, final_plate, tips_96, tips_384
        assert len(threads) == 6

    def test_workflow_entry_threads(self, smc: SmcAssayBuild) -> None:
        assert smc.workflow is not None
        entry = smc.workflow.entry_thread_templates
        assert len(entry) == 1
        assert entry[0].name == "plate_1"

    def test_workflow_auto_spawn_registry(self, smc: SmcAssayBuild) -> None:
        assert smc.workflow is not None
        registry = smc.workflow.auto_spawn_registry
        assert len(registry) == 5
        assert "sample_plate" in registry
        assert "neut_plate" in registry
        assert "tips_96" in registry
        assert "tips_384" in registry
        assert "final_plate" in registry

    def test_resource_pools(self, smc: SmcAssayBuild) -> None:
        pools = smc.system.resource_pools
        pool_names = {p.name for p in pools}
        assert "shaker_collection" in pool_names

    def test_shaker_collection_has_10_devices(self, smc: SmcAssayBuild) -> None:
        pools = smc.system.resource_pools
        shaker_pool = next(p for p in pools if p.name == "shaker_collection")
        assert len(shaker_pool.resources) == 10
