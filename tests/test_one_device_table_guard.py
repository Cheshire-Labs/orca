"""Guard: only cheshire-drivers answers which driver and which lock a device's command runs on.

orca-client and a check run both hold their devices in ``cheshire_drivers.command_executor.DeviceRegistry``.
A second ``driver_for`` or ``lock_for`` written in this repo is a second copy of that table, and the two
would drift without any test seeing it.
"""

import re
from pathlib import Path

from cheshire_source_text import CodeLine, code_lines, code_lines_of, python_files

_SRC = Path(__file__).resolve().parents[1] / "src"
_DEFINES_THE_TABLE = re.compile(r"^(?:async\s+)?def\s+(driver_for|lock_for)\b")


def _definitions(lines: list[CodeLine]) -> list[CodeLine]:
    return [line for line in lines if _DEFINES_THE_TABLE.match(line.text.strip())]


def test_nothing_in_src_defines_driver_for_or_lock_for() -> None:
    assert python_files(_SRC), f"the scan found no source under {_SRC}"
    found = [
        f"{path.relative_to(_SRC)}:{line.lineno}: {line.text.strip()}"
        for path in python_files(_SRC)
        for line in _definitions(code_lines_of(path))
    ]
    assert found == [], "hold devices in cheshire_drivers.command_executor.DeviceRegistry instead: " + "; ".join(found)


def test_the_scan_fires_on_a_second_table() -> None:
    source = "\n".join([
        "class Mine:",
        "    def driver_for(self, name, mode):",
        "        return None",
        "    async def lock_for(self, name):",
        "        return None",
    ])
    assert len(_definitions(code_lines(source))) == 2


def test_the_scan_ignores_a_call_and_a_mention() -> None:
    source = "\n".join([
        "driver = registry.driver_for(name, mode)",
        "lock = registry.lock_for(name)",
        '"""',
        'def driver_for(self, name, mode):',
        '"""',
    ])
    assert _definitions(code_lines(source)) == []
