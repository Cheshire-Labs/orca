from system.system import System
from yml_config_builder.configs import SystemConfig
from yml_config_builder.template_factories import ConfigToSystemBuilder

import yaml


class ConfigFile:
    def __init__(self, path: str) -> None:
        self._path = path
        with open(self._path, 'r') as f:
            self._yml = yaml.load(f, Loader=yaml.FullLoader)
        self._config = SystemConfig.model_validate(self._yml)

    def get_system(self) -> System:
        return ConfigToSystemBuilder(self._config).get_system()