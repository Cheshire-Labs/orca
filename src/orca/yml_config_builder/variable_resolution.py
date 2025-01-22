from typing import Any, Dict, List
from orca.yml_config_builder.configs import ConfigModel

class IVariableLookupAdapter:
    def get(self, name: str) -> Any:
        raise NotImplementedError()

class ConfigModelLookupAdapter(IVariableLookupAdapter):
    def __init__(self, config: ConfigModel) -> None:
        self._config = config

    def get(self, name: str) -> Any:
        if hasattr(self._config, name):
            value = getattr(self._config, name)
        elif hasattr(self._config, "model_extra") and hasattr(self._config.model_extra, name):
            value = self._config.model_extra[name]
        else:
            raise ValueError(f"Variable {name} not found")
        return value
    
class DictLookupAdapter(IVariableLookupAdapter):
    def __init__(self, config: Dict[str, Any]) -> None:
        self._config = config

    def get(self, name: str) -> Any:
        if name in self._config.keys():
            return self._config[name]
        raise ValueError(f"Variable {name} not found")

class VariablesLookupRouter:
    def __init__(self) -> None:
        self._registry: Dict[str, IVariableLookupAdapter] = {}

    def add_config(self, selector: str, config: ConfigModel | Dict[str, Any]) -> None:
        if isinstance(config, dict):
            self._registry[selector] = DictLookupAdapter(config)
        else:
            self._registry[selector] = ConfigModelLookupAdapter(config)

    def get_config(self, selector: str) -> IVariableLookupAdapter:
        if selector not in self._registry.keys():
            raise ValueError(f"Selector {selector} not found")
        return self._registry[selector]


class VariableGroup:
    opener = "${"
    closer = "}"
    selector_separator = ":"
    property_separator = "."
    def __init__(self, value: str) -> None:
        if value[0:2] != self.opener:
            raise ValueError(f"Invalid variable group: {value}")
        if value[-1] != self.closer:
            raise ValueError(f"Invalid variable group: {value}")
        self._value = value[2:-1]
        selector_split = self._value.split(self.selector_separator)
        self.selector: str = selector_split[0]
        self.property_names: List[str] = selector_split[1].split(self.property_separator)


class VariableStringParser:
    opener = "${"
    closer = "}"

    def __init__(self, router: VariablesLookupRouter) -> None:
        self._lookup_router = router

    def parse(self, value: List[str]) -> str:
        is_opened = False
        constant = ""
        variable_string = ""
        while len(value) > 0:
            c = value.pop(0)
            if not is_opened:
                constant += c
                if constant.endswith(self.opener):
                    constant = constant.rstrip(self.opener)
                    variable_string = self.opener 
                    is_opened = True
                    continue
            else:
                variable_string += c

                # if a another opener is found
                if len(variable_string) > 2 and variable_string.endswith(self.opener):
                    variable_string = variable_string.rstrip(self.opener)
                    prepend = list(self.opener)
                    prepend.extend(value)
                    value = prepend
                    variable_string += self.parse(value)

                if variable_string.endswith(self.closer):
                    return self._resolve_variable_value(variable_string)

        return constant

    def _resolve_variable_value(self, variable_string: str) -> Any:
        var_group = VariableGroup(variable_string)
        config: IVariableLookupAdapter = self._lookup_router.get_config(var_group.selector)
        value = config.get(var_group.property_names[0])
        if isinstance(value, dict):
            for prop_name in var_group.property_names[1:]:
                value = value[prop_name]
        # allows for recursion allowing the look up of any returned variable configurations
        if isinstance(value, str):
            return self.parse(list(value))
        return value


class VariablesRegistry:
    def __init__(self) -> None:
        self._router = VariablesLookupRouter()
        self._parser = VariableStringParser(self._router)

    def set_selector_configuration(self, selector: str, config: ConfigModel | Dict[str, Any]) -> None:
        self._router.add_config(selector, config)

    def get(self, key: Any) -> Any:
        if isinstance(key, str):
            return self._parser.parse(list(key))
        return key