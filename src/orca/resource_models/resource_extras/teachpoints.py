from __future__ import annotations
from typing import List, Optional


class Teachpoint:
    @staticmethod
    def load_teachpoints_from_file(file_path: str) -> List[Teachpoint]:
            import xml.etree.ElementTree as ET
            positions: List[Teachpoint] = []
            tree = ET.parse(file_path)
            root = tree.getroot()
            for teachpoint in root.findall('teachpoint'):
                name = str(teachpoint.get('name'))
                shoulder = float(teachpoint.get('shoulder', 0))
                elbow = float(teachpoint.get('elbow', 0))
                wrist = float(teachpoint.get('wrist', 0))
                positions.append(Teachpoint(name, wrist, elbow, shoulder))
            return positions
    
    def __init__(self, name: str, wrist: float, elbow: float, shoulder: float):
        self._name = name
        self._wrist = wrist
        self._elbow = elbow
        self._shoulder = shoulder

    @property
    def name(self) -> str:
         return self._name
     
    @property
    def wrist(self) -> Optional[float]:
        return self._wrist
     
    @property
    def elbow(self) -> Optional[float]:
        return self._elbow

    @property
    def shoulder(self) -> Optional[float]:
        return self._shoulder

        
