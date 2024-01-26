import pytest

from main import Config


class TestConfig:
    def test_labwares_load(self):
        config = Config("test_config.yml")
        assert config.labwares["test-plate1"].type == "Test 96 Plate"
        assert config.labwares["test-plate2"].type == "Test 384 Plate"

    def test_resources_load(self):
        config = Config("test_config.yml")
        assert config.resources["mock-robot"].name == "mock-robot"
        assert config.resources["mock-robot"].type == "mock-robot"
        assert config.resources["mock-robot"].ip == "127.0.0.1"
        assert config.resources["mock-hamilton"].name == "mock-hamilton"
        assert config.resources["mock-hamilton"].type == "mock-hamilton"
        assert config.resources["mock-hamilton"].ip == "localhost"
