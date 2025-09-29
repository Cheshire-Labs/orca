from __future__ import annotations
from typing import List, Dict

class CartesianCoordinates:
    def __init__(self, x: float, y: float, z: float, yaw: float, pitch: float, roll: float):
        self.x = x
        self.y = y
        self.z = z
        self.yaw = yaw
        self.pitch = pitch
        self.roll = roll
    
class Teachpoint:
    def __init__(self, name: str, coordinates: CartesianCoordinates, approach_height: float) -> None:
        self.name = name
        self.coordinates = coordinates
        self.approach_height = approach_height

    @staticmethod
    def load_teachpoints_from_file(file_path: str) -> List[Teachpoint]:
        import xml.etree.ElementTree as ET
        positions: List[Teachpoint] = []
        tree = ET.parse(file_path)
        root = tree.getroot()
        for teachpoint in root.findall('teachpoint'):
            name = str(teachpoint.get('name'))
            x = float(teachpoint.get('x', 0))
            y = float(teachpoint.get('y', 0))
            z = float(teachpoint.get('z', 0))
            yaw = float(teachpoint.get('yaw', 0))
            pitch = float(teachpoint.get('pitch', 0))
            roll = float(teachpoint.get('roll', 0))
            approach_height = float(teachpoint.get('approach_height', 0))
            positions.append(Teachpoint(name, CartesianCoordinates(x, y, z, yaw, pitch, roll), approach_height))
        return positions


class TeachpointsRegistry:
    def __init__(self) -> None:
        self._registry: Dict[str, Teachpoint] = {}

    def load_teachpoints_from_file(self, file_path: str) -> None:
        import xml.etree.ElementTree as ET
        tree = ET.parse(file_path)
        root = tree.getroot()
        for teachpoint in root.findall('teachpoint'):
            name = str(teachpoint.get('name'))
            x = float(teachpoint.get('x', 0))
            y = float(teachpoint.get('y', 0))
            z = float(teachpoint.get('z', 0))
            yaw = float(teachpoint.get('yaw', 0))
            pitch = float(teachpoint.get('pitch', 0))
            roll = float(teachpoint.get('roll', 0))
            approach_height = float(teachpoint.get('approach_height', 0))
            self.add(Teachpoint(name, CartesianCoordinates(x, y, z, yaw, pitch, roll), approach_height))

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
        import xml.etree.ElementTree as ET
        root = ET.Element('teachpoints')
        for teachpoint in self._registry.values():
            tp_element = ET.SubElement(root, 'teachpoint')
            tp_element.set('name', teachpoint.name)
            tp_element.set('x', str(teachpoint.coordinates.x))
            tp_element.set('y', str(teachpoint.coordinates.y))
            tp_element.set('z', str(teachpoint.coordinates.z))
            tp_element.set('yaw', str(teachpoint.coordinates.yaw))
            tp_element.set('pitch', str(teachpoint.coordinates.pitch))
            tp_element.set('roll', str(teachpoint.coordinates.roll))
            tp_element.set('approach_height', str(teachpoint.approach_height))
        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ", level=0)
        tree.write(filepath, encoding='utf-8', xml_declaration=True)

    def clear(self) -> None:
        self._registry = {}