from __future__ import annotations
from typing import Any, List, Dict

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
        z_above: float = 10.0
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

    @staticmethod
    def load_teachpoints_from_file(file_path: str) -> List[Teachpoint]:
        import json
        positions: List[Teachpoint] = []

        with open(file_path, 'r') as f:
            data = json.load(f)

        for teachpoint in data.get('teachpoints', []):
            name = teachpoint.get('name')
            x = float(teachpoint.get('x'))
            y = float(teachpoint.get('y'))
            z = float(teachpoint.get('z'))
            yaw = float(teachpoint.get('yaw'))
            pitch = float(teachpoint.get('pitch'))
            roll = float(teachpoint.get('roll'))
            orientation = teachpoint.get('orientation', None)
            access_type = teachpoint.get('access_type', 'vertical')
            gripper_offset = float(teachpoint.get('gripper_offset', 20.0))
            retract_distance = float(teachpoint.get('retract_distance', 100.0))
            vertical_clearance = float(teachpoint.get('vertical_clearance', 50.0))
            z_above = float(teachpoint.get('z_above', 10.0))
            positions.append(Teachpoint(
                name,
                CartesianCoordinates(x, y, z, yaw, pitch, roll),
                orientation,
                access_type,
                gripper_offset,
                retract_distance,
                vertical_clearance,
                z_above
            ))

        return positions


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
        """Saves the teachpoints to a file"""
        import json

        teachpoints_list: List[Dict[str, Any]] = []
        for teachpoint in self._registry.values():
            tp_dict: Dict[str, Any] = {
                'name': teachpoint.name,
                'x': teachpoint.coordinates.x,
                'y': teachpoint.coordinates.y,
                'z': teachpoint.coordinates.z,
                'yaw': teachpoint.coordinates.yaw,
                'pitch': teachpoint.coordinates.pitch,
                'roll': teachpoint.coordinates.roll,
                'orientation': teachpoint.orientation,
                'access_type': teachpoint.access_type,
                'gripper_offset': teachpoint.gripper_offset,
                'retract_distance': teachpoint.retract_distance,
                'vertical_clearance': teachpoint.vertical_clearance,
                'z_above': teachpoint.z_above
            }
            teachpoints_list.append(tp_dict)

        data = {'teachpoints': teachpoints_list}

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

    def clear(self) -> None:
        self._registry = {}