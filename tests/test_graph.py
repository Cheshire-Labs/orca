import pytest
import asyncio

from typing import Dict, List
# import conftest # type: ignore
from resource_models.labware import Labware, LabwareTemplate
from resource_models.resource_pool import EquipmentResourcePool
from system.method_executor import MethodExecutor
from system.move_handler import MoveHandler
from system.registries import LabwareRegistry
from system.reservation_manager import ReservationManager
from system.resource_registry import ResourceRegistry
from system.system_map import SystemMap
from tests.mock import MockEquipmentResource, MockRoboticArm
from workflow_models.action import LocationAction
from workflow_models.dynamic_resource_action import DynamicResourceAction
from workflow_models.labware_thread import LabwareThread, Method

import networkx as nx # type: ignore

    
class TestSystemGraph:
    
    def test_get_shortest_path(self, system_map: SystemMap):
        expected_path = ["stacker1", "loc3", "ham1"]
        path = system_map.get_all_shortest_available_paths("stacker1", "ham1")

        assert path[0] == expected_path

    def test_no_path_through_in_use_plate(self, system_map: SystemMap):
        loc3 = system_map.get_location("loc3")
        blocking_labware = Labware("plate", labware_type="mock_labware")
        asyncio.run(loc3.prepare_for_place(blocking_labware))
        asyncio.run(loc3.notify_placed(blocking_labware))
        has_path = system_map.has_available_route("stacker1", "ham1")
        assert not has_path
    
