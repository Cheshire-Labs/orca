from __future__ import annotations
from typing import Any, List, Dict
import json

class AccessConfig:
    """Defines how a robotic arm approaches and retracts from a location.

    Access configs are defined in JSON teachpoint files and can be referenced by multiple
    teachpoints to avoid duplication. Parameters specify vertical or horizontal access strategies.
    """
    def __init__(
        self,
        name: str,
        access_type: str,
        gripper_offset: float = 20.0,
        retract_distance: float = 100.0,
        vertical_clearance: float = 50.0,
        z_above: float = 10.0
    ):
        self.name = name
        self.access_type = access_type  # "vertical" or "horizontal"
        self.gripper_offset = gripper_offset  # Gripper height compensation (always used)
        self.retract_distance = retract_distance  # For vertical access: how far to pull back
        self.vertical_clearance = vertical_clearance  # For horizontal access: clearance distance
        self.z_above = z_above  # For horizontal access: extra height above nest

class CartesianCoordinates:
    def __init__(self, x: float, y: float, z: float, yaw: float, pitch: float, roll: float):
        self.x = x
        self.y = y
        self.z = z
        self.yaw = yaw
        self.pitch = pitch
        self.roll = roll
    
class Teachpoint:
    def __init__(
        self,
        name: str,
        coordinates: CartesianCoordinates,
        orientation: str | None,
        access_type: str,
        gripper_offset: float = 20.0,
        retract_distance: float = 100.0,
        vertical_clearance: float = 50.0,
        z_above: float = 10.0,
        gateway: str | None = None
    ) -> None:
        self.name = name
        self.coordinates = coordinates
        self.orientation = orientation
        self.access_type = access_type  # "vertical" or "horizontal"
        self.gripper_offset = gripper_offset  # Gripper height compensation (always used)
        # For VERTICAL access only:
        self.retract_distance = retract_distance  # How far to pull back horizontally
        # For HORIZONTAL access only:
        self.vertical_clearance = vertical_clearance  # Vertical clearance distance
        self.z_above = z_above  # Extra height above nest slot
        # Gateway waypoint - robot must pass through this teachpoint before reaching destination
        self.gateway = gateway

        # Name of access config this teachpoint uses (for JSON save reconstruction)
        self._access_config_name: str | None = None

    @staticmethod
    def load_teachpoints_from_file(file_path: str) -> List[Teachpoint]:
        with open(file_path, 'r') as f:
            data = json.load(f)

        # Parse access configs from JSON
        access_configs: Dict[str, AccessConfig] = {}
        if 'access_configs' in data:
            for name, cfg_data in data['access_configs'].items():
                access_configs[name] = AccessConfig(
                    name=name,
                    access_type=cfg_data['access_type'],
                    gripper_offset=float(cfg_data.get('gripper_offset', 20.0)),
                    retract_distance=float(cfg_data.get('retract_distance', 100.0)),
                    vertical_clearance=float(cfg_data.get('vertical_clearance', 50.0)),
                    z_above=float(cfg_data.get('z_above', 10.0))
                )

        # Add hardcoded defaults
        access_configs['default_vertical'] = AccessConfig(
            'default_vertical', 'vertical', 20.0, 100.0, 50.0, 10.0
        )
        access_configs['default_horizontal'] = AccessConfig(
            'default_horizontal', 'horizontal', 20.0, 100.0, 50.0, 10.0
        )

        # Parse teachpoints and resolve access config references
        teachpoints: List[Teachpoint] = []
        for tp_data in data.get('teachpoints', []):
            # Resolve access config (use default_vertical if not specified)
            config_name = tp_data.get('access', 'default_vertical')
            if config_name not in access_configs:
                raise ValueError(
                    f"Teachpoint '{tp_data['name']}' references unknown access config '{config_name}'"
                )
            cfg = access_configs[config_name]

            # Create teachpoint with resolved access params
            tp = Teachpoint(
                name=tp_data['name'],
                coordinates=CartesianCoordinates(
                    x=float(tp_data['x']),
                    y=float(tp_data['y']),
                    z=float(tp_data['z']),
                    yaw=float(tp_data['yaw']),
                    pitch=float(tp_data['pitch']),
                    roll=float(tp_data['roll'])
                ),
                orientation=tp_data.get('orientation', None),
                access_type=cfg.access_type,
                gripper_offset=cfg.gripper_offset,
                retract_distance=cfg.retract_distance,
                vertical_clearance=cfg.vertical_clearance,
                z_above=cfg.z_above,
                gateway=tp_data.get('gateway', None)
            )
            tp._access_config_name = config_name
            teachpoints.append(tp)

        return teachpoints


class TeachpointsRegistry:
    def __init__(self) -> None:
        self._registry: Dict[str, Teachpoint] = {}

    def add(self, teachpoint: Teachpoint, overwrite = True) -> None:
        """Add a teachpoint to the registry."""
        if not overwrite and self.exists(teachpoint.name):
            raise KeyError(f"Teachpoint '{teachpoint.name}' already exists and overwrite is disabled")
        self._registry[teachpoint.name] = teachpoint
        
    def get(self, name: str) -> Teachpoint:
        """Get a teachpoint by name."""
        if name not in self._registry:
            raise KeyError(f"Teachpoint '{name}' not found")
        return self._registry[name]
        
    def update(self, name: str, teachpoint: Teachpoint) -> None:
        """Update an existing teachpoint."""
        if name not in self._registry:
            raise KeyError(f"Teachpoint '{name}' not found")
        self._registry[name] = teachpoint
        
    def delete(self, name: str) -> None:
        """Delete a teachpoint by name."""
        if name not in self._registry:
            raise KeyError(f"Teachpoint '{name}' not found")
        del self._registry[name]
        
    def list(self) -> List[Teachpoint]:
        """Get all teachpoints."""
        return list(self._registry.values())
        
    def exists(self, name: str) -> bool:
        """Check if a teachpoint exists."""
        return name in self._registry
    
    def save(self, filepath: str) -> None:
        """Saves teachpoints to file using access config references."""
        # Reconstruct unique access configs from teachpoints
        # Note: If multiple teachpoints claim same config name but have different params,
        # only the first one's params are used (assumes data is not corrupted)
        access_configs_dict: Dict[str, Dict[str, Any]] = {}

        for tp in self._registry.values():
            config_name = tp._access_config_name or 'default_vertical'

            # Skip hardcoded defaults (always available in load)
            if config_name.startswith('default_'):
                continue

            # Only write each config once (first occurrence)
            if config_name not in access_configs_dict:
                access_configs_dict[config_name] = {
                    'access_type': tp.access_type,
                    'gripper_offset': tp.gripper_offset,
                    'retract_distance': tp.retract_distance,
                    'vertical_clearance': tp.vertical_clearance,
                    'z_above': tp.z_above
                }

        # Build teachpoints list with access references
        teachpoints_list: List[Dict[str, Any]] = []
        for tp in self._registry.values():
            tp_dict: Dict[str, Any] = {
                'name': tp.name,
                'x': tp.coordinates.x,
                'y': tp.coordinates.y,
                'z': tp.coordinates.z,
                'yaw': tp.coordinates.yaw,
                'pitch': tp.coordinates.pitch,
                'roll': tp.coordinates.roll,
                'orientation': tp.orientation,
                'access': tp._access_config_name or 'default_vertical'
            }
            if tp.gateway:
                tp_dict['gateway'] = tp.gateway
            teachpoints_list.append(tp_dict)

        # Build final JSON structure
        data: Dict[str, Any] = {}
        if access_configs_dict:
            data['access_configs'] = access_configs_dict
        data['teachpoints'] = teachpoints_list

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

    def clear(self) -> None:
        self._registry = {}