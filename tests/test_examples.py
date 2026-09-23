"""Smoke tests verifying all examples import and build without errors.

These tests do NOT run the examples (hardware drivers unavailable in CI).
They verify that the system topology and workflow can be constructed.
"""

import pytest


_has_pylabrobot = pytest.importorskip("pylabrobot", reason="pylabrobot not installed")


class TestExampleBuilds:

    async def test_smc_assay_builds(self) -> None:
        from examples.smc_assay.smc_assay_example import build_smc
        smc = await build_smc()
        assert smc.workflow is not None
        assert smc.workflow.name == "smc_assay"
        assert len(smc.workflow.thread_templates) > 0

    async def test_hamilton_smc_builds(self) -> None:
        from examples.hamilton_smc.hamilton_smc_example import build_hamilton_smc
        smc = await build_hamilton_smc()
        assert smc.workflow is not None
        assert smc.workflow.name == "hamilton_smc_assay"
        assert len(smc.workflow.thread_templates) > 0

    async def test_opentrons_flex_smc_builds(self) -> None:
        from examples.opentrons_flex_smc.opentrons_flex_smc_example import build_opentrons_flex_smc
        smc = await build_opentrons_flex_smc()
        assert smc.workflow is not None
        assert smc.workflow.name == "opentrons_flex_smc_assay"
        assert len(smc.workflow.thread_templates) > 0

    async def test_pylabrobot_builds(self) -> None:
        from examples.pylabrobot_example.pylabrobot_example import build_plr
        plr = await build_plr()
        assert plr.workflow is not None
        assert plr.workflow.name == "learn_orca"
        assert len(plr.workflow.thread_templates) > 0

    async def test_venus_builds(self) -> None:
        from examples.simple_venus_example.simple_venus_example import build_venus
        venus = await build_venus()
        assert venus.workflow is not None
        assert venus.workflow.name == "example_workflow"
        assert len(venus.workflow.thread_templates) > 0
