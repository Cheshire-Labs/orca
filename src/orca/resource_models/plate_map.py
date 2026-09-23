from dataclasses import dataclass, field


@dataclass(frozen=True)
class WellSample:
    sample_id: str
    well: str
    concentration: float | None = None


@dataclass
class PlateMap:
    labware_id: str
    samples: list[WellSample] = field(default_factory=list)

    def get_sample_at(self, well: str) -> WellSample | None:
        for s in self.samples:
            if s.well == well:
                return s
        return None

    def get_well_for_sample(self, sample_id: str) -> str | None:
        for s in self.samples:
            if s.sample_id == sample_id:
                return s.well
        return None
