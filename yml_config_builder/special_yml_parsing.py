import re

def is_dynamic_yaml(value: str) -> bool:
    return re.search(r"\$\{([^}]+)\}", value) is not None

def get_dynamic_yaml_keys(value: str) -> list:
    return re.findall(r"\$\{([^}]+)\}", value)
