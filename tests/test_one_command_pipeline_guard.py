"""Guard: every device command goes through the one pipeline.

Read as text through `cheshire_source_text`, which blanks strings and comments and joins bracket continuations, so
one logical line holds a whole call. Each line is tagged with the def or class chain it sits in, and every
exemption names that function or a line pattern, never a whole file.

- `execute_command` is called or named only by the pipeline's send stage and by the remote drivers' send helper.
- A driver method is called only inside the parentheses of an engine command (`engine_commands.run(...)` or
  `engine_commands.keep_world(...)`), or by
  a device's command method that forwards to the driver command of its own name. `forget_driver_session` is the one
  driver method that is not a command: it clears a local flag and sends nothing.
- A position or a device deck record is written only by `LabwarePlacer`, and by the holders it writes through: a
  `Location` handing the write to its resource, a mover clearing its own jaws, the liquid handler's own deck record,
  and the system's deck projection the placer is given.
- A saved position (`update_location`, `clear_location`) is written only by `LabwarePlacer`, the labware service,
  the stores below it, and a check run writing its own store.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from cheshire_source_text import CodeLine, code_lines, code_lines_of, python_files

_SRC = Path(__file__).resolve().parent.parent / "src" / "orca"

_HEADER = re.compile(r"^(?:async\s+)?(?:def|class)\s+(\w+)")
_SENDS_ON_THE_WIRE = re.compile(r"\.execute_command\b")
_CALLS_A_DRIVER = re.compile(r"\b(\w*driver)\s*(?:\(\s*\))?\.(?!forget_driver_session\b)([a-z_]\w*)\s*\(")
# A driver method handed on uncalled as an argument (`self._retry(self.driver.home)`).
_HANDS_ON_A_DRIVER_METHOD = re.compile(
    r"[(,]\s*(?:[\w.]*\.)?(\w*driver)\s*(?:\(\s*\))?\.(?!forget_driver_session\b)([a-z_]\w*)\s*(?=[),])"
)
# A driver bound to a name: `d = self.driver`, `d = await self._parking_driver()`, `d = self.driver if ok else None`.
_BINDS_A_DRIVER = re.compile(
    r"^([A-Za-z_]\w*)\s*(?::[^=]*)?=\s*(?:await\s+)?[\w.]*\b\w*driver\s*(?:\(\s*\))?\s*(?:if\b.*)?$"
)
_SENDS_BY_NAME = re.compile(r"^(?!from\s|import\s).*\bsend_to_the_device\b")
_ENGINE_COMMAND = re.compile(r"\bengine_commands\.(?:run|keep_world)\s*\(")
_WRITES_A_POSITION = re.compile(
    r"(?<!\bself)(?<!super\(\))\.(?:place_labware|dispose_labware|initialize_labware|reset_labware_state)\s*\("
    r"|location_service\.(?:update|retire|reset|expect|stop_expecting|clear_all)\s*\("
    r"|\b(?:add|remove|project|retract)_deck_labware\w*"
    r"|\.(?:reset_loaded_labware|reset_labware_state_everywhere)\s*\("
    r"|\b(?:reconcile_lh_deck_occupancy|project_labware_on_lh_decks)\b"
)
_SAVES_A_POSITION = re.compile(r"\.(?:update_location|clear_location)\s*\(")
_TELLS_A_SITE = re.compile(r"\.notify_(?:picked|placed)\b")
# Modules holding device classes, whose command methods forward to the driver command of the same name.
_DEVICE_COMMAND_MODULES = re.compile(
    r"^(?:devices/[\w]+\.py|resource_models/devices\.py|resource_models/transporter\.py|resource_models/plate_pad\.py)$"
)


@dataclass(frozen=True)
class _Line:
    text: str
    function: str


@dataclass(frozen=True)
class _Exempt:
    file: str
    function: str = ""
    line: str = ""

    def __post_init__(self) -> None:
        assert self.function or self.line, f"{self.file}: an exemption names a function or a line, never a file"

    def covers(self, relative: str, line: _Line) -> bool:
        if relative != self.file:
            return False
        if self.function and not (line.function == self.function or line.function.startswith(self.function + ".")):
            return False
        return not self.line or re.search(self.line, line.text) is not None


_WIRE_SENDERS = (
    # The pipeline's send stage, and the one send helper behind each remote driver's command methods.
    _Exempt("gateway/controller/command_pipeline.py", "send_to_the_device"),
    _Exempt("gateway/remote_drivers.py", "_RemoteDriverBase._send"),
    _Exempt("gateway/remote_transporter_driver.py", "RemoteTransporterDriver._send"),
)

# The send stage of a command named by an operator: only the operator runner hands it to the pipeline.
_SENDS_BY_NAME_FROM = (_Exempt("gateway/operator_commands.py", "OperatorControl._dispatch"),)

_DRIVER_CALLERS = (
    # A pad's drop notice that the pipeline's deck-record effect reaches through `notify_picked`.
    _Exempt("resource_models/plate_pad.py", "PlatePad._do_notify_picked"),
    # Bring-up opens the link before it initializes; the run's bring-up wraps the whole method.
    _Exempt("resource_models/transporter.py", "Transporter.initialize", r"^await self\.driver\.connect\s*\(\s*\)$"),
)

_POSITION_WRITERS = (
    # The placer is the one writer; every one of its methods writes.
    _Exempt("resource_models/labware_placement.py", "LabwarePlacer"),
    # A location hands the placer's write to the resource it wraps.
    _Exempt("resource_models/location.py", "Location.initialize_labware"),
    _Exempt("resource_models/location.py", "Location.place_labware"),
    _Exempt("resource_models/location.py", "Location.dispose_labware"),
    # A mover clearing its own jaws, which the placer asks for in `jaws_released`.
    _Exempt("resource_models/transporter_base.py", "TransporterBase.reset_labware_state"),
    # The liquid handler's own deck record, which the placer reaches through the system's deck projection.
    _Exempt("devices/devices.py", "LiquidHandler.project_deck_labware"),
    _Exempt("devices/devices.py", "LiquidHandler._drop_deck_object"),
    _Exempt("devices/devices.py", "LiquidHandler._do_notify_placed"),
    # The system's deck projection, and the constructor that hands it to the placer.
    _Exempt("system/system.py", "System.__init__", r"\bLabwarePlacer\s*\("),
    _Exempt("system/system.py", "System._agree_about_one_labware"),
)

_POSITION_SAVERS = (
    # The placer saves through the labware service.
    _Exempt("resource_models/labware_placement.py", "LabwarePlacer"),
    # The service, its announcing wrapper and the SQL store each pass a save one layer down.
    _Exempt("runtime/labware_service.py", "LabwareService.update_location"),
    _Exempt("runtime/labware_service.py", "LabwareService.clear_location"),
    _Exempt("runtime/labware_service.py", "AnnouncedLabwareService.update_location"),
    _Exempt("runtime/labware_service.py", "AnnouncedLabwareService.clear_location"),
    _Exempt("runtime/sql_labware_store.py", "SqlLabwareStore.update_location"),
    _Exempt("runtime/sql_labware_store.py", "SqlLabwareStore.clear_location"),
    # The shared store contract, which calls the store under test directly.
    _Exempt("store_contract.py", line=r"(?<![\w.])store\.(?:update|clear)_location\s*\("),
    # A check run's seed copies the real record into its own store.
    _Exempt("runtime/check_runs.py", "seed_check_run_store", r"\bcopy\.labware\.update_location\s*\("),
)

# A site's own record of what it holds: told by the pipeline's deck-record effect, or by a site handing it on.
_SITE_TELLERS = (
    _Exempt("resource_models/command_effects.py", "DeckRecordEffect.completed"),
    _Exempt("resource_models/location.py", "Location.notify_picked"),
    _Exempt("resource_models/location.py", "Location.notify_placed"),
    _Exempt("resource_models/device_deck_site.py", "DeviceDeckSite.notify_picked"),
    _Exempt("resource_models/device_deck_site.py", "DeviceDeckSite.notify_placed"),
    # A pad and a Venus deck tell their own driver, from the site methods the deck-record effect calls.
    _Exempt("resource_models/plate_pad.py", "PlatePad.notify_picked"),
    _Exempt("resource_models/plate_pad.py", "PlatePad.notify_placed"),
    _Exempt("resource_models/plate_pad.py", "PlatePad._do_notify_picked"),
    _Exempt("devices/venus.py", "Venus._do_notify_picked"),
    _Exempt("devices/venus.py", "Venus._do_notify_placed"),
)


def _lines(code: Iterable[CodeLine]) -> list[_Line]:
    lines: list[_Line] = []
    scope: list[tuple[int, str]] = []
    for line in code:
        while scope and scope[-1][0] >= line.indent:
            scope.pop()
        text = line.text.strip()
        header = _HEADER.match(text)
        if header:
            scope.append((line.indent, header.group(1)))
            text = text[header.end():]
        lines.append(_Line(text, ".".join(name for _, name in scope)))
    return lines


def _exempt(relative: str, line: _Line, exemptions: tuple[_Exempt, ...]) -> bool:
    return any(exemption.covers(relative, line) for exemption in exemptions)


def _engine_command_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for opened in _ENGINE_COMMAND.finditer(text):
        depth = 0
        end = len(text)
        for at in range(opened.end() - 1, len(text)):
            depth += {"(": 1, ")": -1}.get(text[at], 0)
            if depth == 0:
                end = at
                break
        spans.append((opened.end(), end))
    return spans


def _flag(relative: str, lines: list[_Line], pattern: re.Pattern[str], exemptions: tuple[_Exempt, ...]) -> list[str]:
    return [
        f"{relative}: {line.text}" for line in lines
        if pattern.search(line.text) and not _exempt(relative, line, exemptions)
    ]


def _forwards_its_own_command(relative: str, line: _Line, call: re.Match[str]) -> bool:
    own_driver = call.group(1) in ("driver", "_driver") and line.text[:call.start()].endswith("self.")
    method = line.function.split(".")
    return bool(_DEVICE_COMMAND_MODULES.match(relative)) and own_driver and len(method) > 1 and method[-1] == call.group(2)


def _aliases_seen_from(function: str, aliases: dict[str, set[str]]) -> set[str]:
    return {alias for scope, names in aliases.items() if function == scope or function.startswith(scope + ".")
            for alias in names}


def _driver_calls(relative: str, lines: list[_Line]) -> list[str]:
    found: list[str] = []
    aliases: dict[str, set[str]] = {}
    for line in lines:
        bound = _BINDS_A_DRIVER.match(line.text)
        if bound:
            aliases.setdefault(line.function, set()).add(bound.group(1))
        calls = [*_CALLS_A_DRIVER.finditer(line.text), *_HANDS_ON_A_DRIVER_METHOD.finditer(line.text)]
        alias_calls = [
            call for alias in _aliases_seen_from(line.function, aliases)
            for call in re.finditer(rf"(?<![\w.]){alias}\.(?!forget_driver_session\b)[a-z_]\w*\s*\(", line.text)
        ]
        spans = _engine_command_spans(line.text)
        outside = [call for call in calls + alias_calls if not any(start <= call.start() < end for start, end in spans)]
        if any(call in alias_calls or not _forwards_its_own_command(relative, line, call) for call in outside) \
                and not _exempt(relative, line, _DRIVER_CALLERS):
            found.append(f"{relative}: {line.text}")
    return found


def _scan(relative: str, lines: list[_Line]) -> list[str]:
    return (
        _flag(relative, lines, _SENDS_ON_THE_WIRE, _WIRE_SENDERS)
        + _flag(relative, lines, _SENDS_BY_NAME, _SENDS_BY_NAME_FROM)
        + _driver_calls(relative, lines)
        + _flag(relative, lines, _WRITES_A_POSITION, _POSITION_WRITERS)
        + _flag(relative, lines, _SAVES_A_POSITION, _POSITION_SAVERS)
        + _flag(relative, lines, _TELLS_A_SITE, _SITE_TELLERS)
    )


def _texts(source: str) -> list[_Line]:
    return _lines(code_lines(source))


def test_every_device_command_goes_through_the_pipeline() -> None:
    found: list[str] = []
    for path in python_files(_SRC):
        found += _scan(path.relative_to(_SRC).as_posix(), _lines(code_lines_of(path)))
    assert found == [], (
        "a device command outside the one pipeline, or a position written outside the placer. "
        "Send a command through `DeviceCommandPipeline.run` or the device's `engine_commands.run` or `keep_world`; "
        "write a position through `LabwarePlacer`:\n" + "\n".join(found)
    )


def test_every_exemption_still_covers_a_line_in_src() -> None:
    lines = {path.relative_to(_SRC).as_posix(): _lines(code_lines_of(path)) for path in python_files(_SRC)}
    rules = (
        (_SENDS_ON_THE_WIRE, _WIRE_SENDERS), (_SENDS_BY_NAME, _SENDS_BY_NAME_FROM), (_CALLS_A_DRIVER, _DRIVER_CALLERS),
        (_WRITES_A_POSITION, _POSITION_WRITERS), (_SAVES_A_POSITION, _POSITION_SAVERS), (_TELLS_A_SITE, _SITE_TELLERS),
    )
    stale = [
        exemption for pattern, exemptions in rules for exemption in exemptions
        if not any(exemption.covers(exemption.file, line) and pattern.search(line.text) for line in lines[exemption.file])
    ]
    assert stale == [], f"exemptions that cover nothing the guard flags; delete them: {stale}"


def test_the_guard_fires_on_a_wire_send_outside_the_pipeline() -> None:
    lines = _texts("async def f(c):\n    await c.execute_command(\n        device_id='x', command='home',\n    )\n")
    assert len(_scan("runtime/facades/devices.py", lines)) == 1


def test_the_guard_fires_on_an_aliased_wire_send() -> None:
    lines = _texts("async def f(c):\n    send = c.execute_command\n    await send(device_id='x', command='home')\n")
    assert len(_scan("runtime/facades/devices.py", lines)) == 1


def test_the_guard_fires_on_a_wire_send_in_a_remote_driver_outside_its_send_helper() -> None:
    lines = _texts("class RemoteShakerDriver:\n    async def shake(self):\n        await self._c.execute_command(x)\n")
    assert len(_scan("gateway/remote_drivers.py", lines)) == 1


def test_the_guard_fires_on_a_driver_call_outside_an_engine_command() -> None:
    lines = _texts("async def f(lh):\n    await lh.driver.get_deck_state(\n        GetDeckStateRequest(),\n    )\n")
    assert len(_scan("system/system.py", lines)) == 1
    assert len(_scan("operations/device.py", _texts("x = device.driver().home()\n"))) == 1


def test_the_guard_fires_on_a_driver_held_under_another_name() -> None:
    assert len(_scan("runtime/facades/devices.py", _texts("await self.lh_driver.home()\n"))) == 1


def test_the_guard_fires_on_a_driver_bound_to_a_local_name() -> None:
    lines = _texts("async def f(self):\n    d = self.driver\n    await d.home()\n")
    assert len(_scan("runtime/facades/devices.py", lines)) == 1


def test_the_guard_passes_a_local_driver_called_inside_an_engine_command() -> None:
    lines = _texts(
        "async def f(self):\n    d = self.driver\n    await self.engine_commands.run('home', lambda: d.home())\n"
    )
    assert _scan("system/system.py", lines) == []


def test_the_guard_fires_on_a_one_line_def_that_calls_a_driver() -> None:
    lines = _texts("class Facade:\n    async def home(self): await self._driver.home()\n")
    assert len(_scan("runtime/facades/devices.py", lines)) == 1


def test_the_guard_passes_a_one_line_def_that_forwards_its_own_command() -> None:
    assert _scan("devices/shaker.py", _texts("class Shaker:\n    async def home(self): await self.driver.home()\n")) == []


def test_the_guard_fires_on_a_driver_call_beside_an_engine_command_on_one_line() -> None:
    after = _texts("async def f(self):\n    await self.engine_commands.run('x', lambda: None); await self.driver.home()\n")
    assert len(_scan("system/system.py", after)) == 1
    before = _texts("async def f(self):\n    await self.driver.home(); await self.engine_commands.run('x', lambda: None)\n")
    assert len(_scan("system/system.py", before)) == 1


def test_the_guard_fires_on_a_private_driver_and_a_send_by_name_elsewhere() -> None:
    assert len(_scan("runtime/facades/devices.py", _texts("await self._driver.home()\n"))) == 1
    assert len(_scan("runtime/facades/devices.py", _texts("await send_to_the_device(c, device_id='x')\n"))) == 1
    dispatch = "class OperatorControl:\n    async def _dispatch(self):\n        raw = await send_to_the_device(c)\n"
    assert _scan("gateway/operator_commands.py", _texts(dispatch)) == []
    elsewhere = "class OperatorControl:\n    async def run(self):\n        raw = await send_to_the_device(c)\n"
    assert len(_scan("gateway/operator_commands.py", _texts(elsewhere))) == 1


def test_the_guard_passes_the_one_driver_method_that_sends_nothing() -> None:
    assert _scan("runtime/registries/device_link.py", _texts("driver.forget_driver_session()\n")) == []


def test_the_guard_passes_a_driver_call_inside_an_engine_command() -> None:
    lines = _texts(
        "async def f(self):\n"
        "    await self.engine_commands.run(\n"
        "        'get_deck_state', lambda: self.driver.get_deck_state(GetDeckStateRequest()),\n"
        "    )\n"
    )
    assert _scan("system/system.py", lines) == []


def test_the_guard_passes_a_device_command_method_and_the_wire_leg() -> None:
    method = _texts("class Shaker:\n    async def shake(self, speed):\n        await self.driver.shake(ShakeRequest(speed))\n")
    assert _scan("devices/shaker.py", method) == []
    helper = "class _RemoteDriverBase:\n    async def _send(self):\n        return await self._c.execute_command(x)\n"
    assert _scan("gateway/remote_drivers.py", _texts(helper)) == []
    stage = "async def send_to_the_device(controller):\n    return await controller.execute_command(x)\n"
    assert _scan("gateway/controller/command_pipeline.py", _texts(stage)) == []


def test_the_guard_fires_on_a_device_method_calling_another_driver_command() -> None:
    lines = _texts("class Shaker:\n    async def shake(self, speed):\n        await self.driver.home()\n")
    assert len(_scan("devices/shaker.py", lines)) == 1


def test_the_guard_fires_on_a_same_name_forward_to_a_driver_the_device_does_not_own() -> None:
    other = _texts("class LiquidHandler:\n    async def home(self):\n        await self._lh.driver.home()\n")
    assert len(_scan("devices/devices.py", other)) == 1
    module_level = _texts("async def home(other):\n    await other.driver.home()\n")
    assert len(_scan("devices/devices.py", module_level)) == 1


def test_the_guard_fires_on_a_driver_method_handed_on_uncalled() -> None:
    facade = "runtime/facades/devices.py"
    assert len(_scan(facade, _texts("await self._retry(self.driver.home)\n"))) == 1
    assert len(_scan(facade, _texts("await self._retry(r, self.lh_driver.home)\n"))) == 1


def test_the_guard_passes_a_driver_property_read() -> None:
    lines = _texts("async def f(self):\n    if not self.driver.is_connected:\n        return\n")
    assert _scan("runtime/facades/devices.py", lines) == []


def test_the_guard_fires_on_a_driver_bound_through_await_a_condition_or_used_in_a_nested_def() -> None:
    facade = "runtime/facades/devices.py"
    awaited = "async def f(self):\n    d = await self._parking_driver()\n    await d.park_gantry(r)\n"
    assert len(_scan(facade, _texts(awaited))) == 1
    conditional = "async def f(self, ok):\n    d = self.driver if ok else None\n    await d.home()\n"
    assert len(_scan(facade, _texts(conditional))) == 1
    nested = "async def f(self):\n    d = self.driver\n    async def g():\n        await d.home()\n    await g()\n"
    assert len(_scan(facade, _texts(nested))) == 1


def test_the_guard_passes_a_renamed_driver_inside_an_engine_command() -> None:
    lines = _texts("async def f(self):\n    await self.engine_commands.run('home', lambda: self.lh_driver.home())\n")
    assert _scan("system/system.py", lines) == []


def test_the_guard_fires_on_a_send_by_name_or_a_site_notice_handed_on() -> None:
    facade = "runtime/facades/devices.py"
    assert len(_scan(facade, _texts("await self._retry(send_to_the_device, c)\n"))) == 1
    assert _scan(facade, _texts("from orca.gateway.controller.command_pipeline import send_to_the_device\n")) == []
    assert len(_scan(facade, _texts("await self._retry(site.notify_placed, labware, mover)\n"))) == 1


def test_the_guard_fires_on_a_position_written_outside_the_placer() -> None:
    assert len(_scan("workflow_models/spawn_actions.py", _texts("await location.place_labware(labware)\n"))) == 1
    assert len(_scan("runtime/facades/labware.py", _texts("self._location_service.retire(instance)\n"))) == 1
    assert len(_scan("runtime/facades/labware.py", _texts("await lh.remove_deck_labware(labware)\n"))) == 1


def test_the_guard_fires_on_a_position_written_in_the_placer_module_outside_the_placer() -> None:
    lines = _texts("def helper(at, labware):\n    at.initialize_labware(labware)\n")
    assert len(_scan("resource_models/labware_placement.py", lines)) == 1


def test_the_guard_fires_on_a_clear_an_expectation_or_a_deck_push_outside_the_placer() -> None:
    facade = "runtime/facades/labware.py"
    assert len(_scan(facade, _texts("self._location_service.clear_all()\n"))) == 1
    assert len(_scan(facade, _texts("self._location_service.stop_expecting(instance)\n"))) == 1
    assert len(_scan(facade, _texts("resource.reset_loaded_labware()\n"))) == 1
    assert len(_scan(facade, _texts("await holder.reset_labware_state_everywhere()\n"))) == 1
    assert len(_scan(facade, _texts("await self._system.project_labware_on_lh_decks(instance, at)\n"))) == 1
    assert len(_scan("runtime/facades/devices.py", _texts("await self._system.reconcile_lh_deck_occupancy(at)\n"))) == 1
    assert len(_scan(facade, _texts("await mover.reset_labware_state()\n"))) == 1
    system = "system/system.py"
    assert len(_scan(system, _texts("await self.reconcile_lh_deck_occupancy(location)\n"))) == 1
    assert len(_scan(system, _texts("self._labware_location_service.update(labware, at)\n"))) == 1


def test_the_guard_fires_on_a_deck_retraction_called_or_handed_on() -> None:
    facade = "runtime/facades/labware.py"
    assert len(_scan(facade, _texts("await lh.retract_deck_labware(instance)\n"))) == 1
    assert len(_scan(facade, _texts("await self._best_effort(lh, instance, lh.retract_deck_labware)\n"))) == 1


def test_the_guard_fires_on_a_deck_push_in_the_system_outside_its_deck_projection() -> None:
    lines = _texts("class System:\n    async def run(self, device, labware):\n        await device.retract_deck_labware(labware)\n")
    assert len(_scan("system/system.py", lines)) == 1


def test_the_guard_passes_a_clear_or_a_deck_push_through_the_placer() -> None:
    facade = "runtime/facades/labware.py"
    assert _scan(facade, _texts("await self._system.labware_placer.cleared_all(locations, holders)\n")) == []
    assert _scan(facade, _texts("await self._system.labware_placer.contents_set(instance, at)\n")) == []
    assert _scan("runtime/facades/devices.py", _texts("await self._system.labware_placer.deck_rebuilt(at)\n")) == []
    assert _scan(facade, _texts("await self._system.labware_placer.jaws_released(mover)\n")) == []
    system = "system/system.py"
    assert _scan(system, _texts("await self._labware_placer.deck_rebuilt(location)\n")) == []
    projection = (
        "class System:\n"
        "    async def _agree_about_one_labware(self, device, labware):\n"
        "        await device.retract_deck_labware(labware)\n"
    )
    assert _scan(system, _texts(projection)) == []
    assert _scan("resource_models/transporter.py", _texts("await super().reset_labware_state()\n")) == []


def test_the_guard_passes_the_placer_and_a_method_of_ones_own() -> None:
    placer = "class LabwarePlacer:\n    async def arrived(self, at, labware):\n        await at.place_labware(labware)\n"
    assert _scan("resource_models/labware_placement.py", _texts(placer)) == []
    thread = "workflow_models/labware_threads/executing_labware_thread.py"
    assert _scan(thread, _texts("await self.initialize_labware()\n")) == []


def test_the_guard_fires_on_a_site_told_outside_the_deck_record_effect() -> None:
    move = "workflow_models/actions/move_action.py"
    assert len(_scan(move, _texts("await self._action.target.notify_placed(labware, mover)\n"))) == 1
    effect = "class DeckRecordEffect:\n    async def completed(self, notice):\n        await notice.site.notify_placed(lw, m)\n"
    assert _scan("resource_models/command_effects.py", _texts(effect)) == []
    other = "class DeckRecordEffect:\n    async def failed(self, notice):\n        await notice.site.notify_placed(lw, m)\n"
    assert len(_scan("resource_models/command_effects.py", _texts(other))) == 1


def test_the_guard_fires_on_a_saved_position_written_outside_the_placer() -> None:
    facade = "runtime/facades/labware.py"
    assert len(_scan(facade, _texts("await self._labware_service.update_location(labware_id, at.position_id)\n"))) == 1
    assert len(_scan(facade, _texts("await self._labware_service.clear_location(\n    instance.id,\n)\n"))) == 1
    workflow = "workflow_models/workflows/executing_workflow.py"
    assert len(_scan(workflow, _texts("await self._labware_service.update_location(bound.id, start.position_id)\n"))) == 1


def test_the_guard_fires_on_a_check_run_writing_the_real_store() -> None:
    seed = "async def seed_check_run_store(real, copy):\n    await real.labware.update_location(labware_id, position_id)\n"
    assert len(_scan("runtime/check_runs.py", _texts(seed))) == 1


def test_the_guard_passes_a_saved_position_written_by_the_placer_or_the_service() -> None:
    write = "class LabwarePlacer:\n    async def _save(self):\n        await self._labware_service.update_location(i, p)\n"
    assert _scan("resource_models/labware_placement.py", _texts(write)) == []
    service = "class LabwareService:\n    async def clear_location(self, i):\n        await self._store.clear_location(i)\n"
    assert _scan("runtime/labware_service.py", _texts(service)) == []
    seed = "async def seed_check_run_store(real, copy):\n    await copy.labware.update_location(labware_id, position_id)\n"
    assert _scan("runtime/check_runs.py", _texts(seed)) == []
    contract = "async def a_moved_labware_is_found(store):\n    await store.update_location(plate.id, 'a')\n"
    assert _scan("store_contract.py", _texts(contract)) == []
    assert _scan("runtime/facades/labware.py", _texts("await self._system.labware_placer.confirmed(instance, at)\n")) == []


def test_the_guard_passes_a_driver_call_inside_a_world_upkeep_command() -> None:
    lines = _texts("async def f(self):\n    await self.engine_commands.keep_world('reset_world', lambda: self.driver.reset_world(r))\n")
    assert _scan("resource_models/transporter.py", lines) == []
