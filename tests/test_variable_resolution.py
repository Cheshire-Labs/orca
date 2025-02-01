import pytest
import yaml

from orca.yml_config_builder.configs import SystemOptionsConfigModel, ConfigModel, VariablesConfigModel
from orca.yml_config_builder.dynamic_config import DynamicBaseConfigModel
from orca.yml_config_builder.variable_resolution import VariablesRegistry

example_variable_config = r"""
config:
    prod:
        settle-time: 120
        volume: 35.5
        plate-name: plate-1
        recursive: ${opt:stage}
        self-recursion: ${config:prod.plate-name}
    test:
        settle-time: 0
        volume: 40.1
        plate-name: test-plate-1
"""

example_options_config = r"""
opt:
    stage: prod
    lookup: volume
"""

@pytest.fixture
def variables_config() -> VariablesConfigModel:
    yml_config = yaml.safe_load(example_variable_config)
    config = yml_config["config"]
    return VariablesConfigModel.model_validate(config)

@pytest.fixture
def options_config() -> SystemOptionsConfigModel:
    yml_config = yaml.safe_load(example_options_config)
    config = yml_config["opt"]
    return SystemOptionsConfigModel.model_validate(config)

@pytest.fixture
def registry(variables_config, options_config) -> VariablesRegistry:
    registry = VariablesRegistry()
    registry.set_selector_configuration("opt", options_config)
    registry.set_selector_configuration("config", variables_config)
    return registry

class TestVariableConfig:
    def test_variable_config(self, variables_config: VariablesConfigModel):
        assert variables_config.model_extra["test"]["volume"] == 40.1

class TestVariableRegistry:
    def test_no_group(self, registry: VariablesRegistry):
        get_string = "test"
        value = registry.get(get_string)
        assert value == get_string

    def test_selector_parsing(self, registry: VariablesRegistry):
        get_string = r"${config:test.volume}"
        value = registry.get(get_string)
        assert value == 40.1

    def test_nested_parse_config(self, registry: VariablesRegistry):
        get_string = r"${config:${opt:stage}.plate-name}"
        value = registry.get(get_string)
        assert value == "plate-1"

    def test_nested_parse_config2(self, registry: VariablesRegistry):
        get_string = r"${config:test.${opt:lookup}}"
        value = registry.get(get_string)
        assert value == 40.1

    def test_consecutive_subgroups(self, registry: VariablesRegistry):
        get_string = r"${config:${opt:stage}.${opt:lookup}}"
        value = registry.get(get_string)
        assert value == 35.5

    def test_recursive_loopup(self, registry: VariablesRegistry):
        get_string = r"${config:prod.recursive}"
        value = registry.get(get_string)
        assert value == "prod"

    def test_self_recursive_loopup(self, registry: VariablesRegistry):
        get_string = r"${config:prod.self-recursion}"
        value = registry.get(get_string)
        assert value == "plate-1"

class MockConfig(ConfigModel):
    test: str

class DynamicMockClass(DynamicBaseConfigModel[MockConfig]):
    @property
    def test(self) -> str:
        return self.get_dynamic(self._config.test)

class TestConfigWrapper:
    def test_lookup_adapter(self, registry: VariablesRegistry):
        config = MockConfig.model_validate({"test": "${config:test.plate-name}"})
        adapter = DynamicMockClass(config, registry)
        assert adapter.test == "test-plate-1"

    