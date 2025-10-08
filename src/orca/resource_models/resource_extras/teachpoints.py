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
    def __init__(self, name: str, coordinates: CartesianCoordinates, approach_height: float, orientation: str | None) -> None:
        self.name = name
        self.coordinates = coordinates
        self.approach_height = approach_height
        self.orientation = orientation

    @staticmethod
    def load_teachpoints_from_file(file_path: str) -> List[Teachpoint]:
        import json
        positions: List[Teachpoint] = []

        with open(file_path, 'r') as f:
            data = json.load(f)

        for teachpoint in data.get('teachpoints', []):
            name = teachpoint.get('name')
            x = float(teachpoint.get('x', 0))
            y = float(teachpoint.get('y', 0))
            z = float(teachpoint.get('z', 0))
            yaw = float(teachpoint.get('yaw', 0))
            pitch = float(teachpoint.get('pitch', 0))
            roll = float(teachpoint.get('roll', 0))
            approach_height = float(teachpoint.get('approach_height', 0))
            orientation = teachpoint.get('orientation', None)
            positions.append(Teachpoint(name, CartesianCoordinates(x, y, z, yaw, pitch, roll), approach_height, orientation))

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
                'approach_height': teachpoint.approach_height,
                'orientation': teachpoint.orientation
            }
            teachpoints_list.append(tp_dict)

        data = {'teachpoints': teachpoints_list}

        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)

    def clear(self) -> None:
        self._registry = {}