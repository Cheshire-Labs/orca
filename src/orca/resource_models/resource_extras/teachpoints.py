from __future__ import annotations
from typing import List

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
        self._name = name
        self._coordinates = coordinates
        self._approach_height = approach_height

    @property
    def name(self) -> str:
        return self._name

    @property
    def x(self) -> float:
        return self._coordinates.x

    @property
    def y(self) -> float:
        return self._coordinates.y

    @property
    def z(self) -> float:
        return self._coordinates.z

    @property
    def yaw(self) -> float:
        return self._coordinates.yaw

    @property
    def pitch(self) -> float:
        return self._coordinates.pitch

    @property
    def roll(self) -> float:
        return self._coordinates.roll

    @property
    def approach_height(self) -> float:
        return self._approach_height

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
