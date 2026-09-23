from orca.resource_models.plate_map import PlateMap, WellSample


class TestPlateMap:
    def test_get_sample_at(self) -> None:
        pm = PlateMap(
            labware_id="plate_1",
            samples=[
                WellSample(sample_id="S-001", well="A1", concentration=10.0),
                WellSample(sample_id="S-002", well="A2"),
            ],
        )
        s = pm.get_sample_at("A1")
        assert s is not None
        assert s.sample_id == "S-001"
        assert s.concentration == 10.0

    def test_get_sample_at_missing(self) -> None:
        pm = PlateMap(labware_id="plate_1", samples=[])
        assert pm.get_sample_at("A1") is None

    def test_get_well_for_sample(self) -> None:
        pm = PlateMap(
            labware_id="plate_1",
            samples=[WellSample(sample_id="S-001", well="B3")],
        )
        assert pm.get_well_for_sample("S-001") == "B3"

    def test_get_well_for_sample_missing(self) -> None:
        pm = PlateMap(labware_id="plate_1", samples=[])
        assert pm.get_well_for_sample("NOPE") is None
