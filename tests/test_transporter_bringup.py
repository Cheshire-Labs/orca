"""Bringing a transporter up must ask for no motion at all.

Bring-up used to initialize, home, and then drive to a safe position. On the
bench that last move faulted the PF400 controller (soft envelope error) and
killed the run before a single thread started. The home was wrong for the same
reason the safe move was: connect, initialize, home and move are separate
commands an operator issues on purpose, and folding two of them into one means
a bring-up sweeps the arm through whatever happens to be in front of it.

The cost is deliberate. An arm that has not homed since power-on refuses the
first move that needs a known position, and that refusal is the prompt to home.
"""

from typing import List

import pytest
from cheshire_drivers.sims import SimTransporterDriver
from cheshire_drivers.homing_models import HomeRequest
from cheshire_drivers.transporter_models import (
    InitializeRequest,
    MoveToSafeRequest,
)

from orca.resource_models.transporter import Transporter

from tests.test_helpers import (
    create_test_teachpoints,
)
from tests.setup_package import teachpoint_file, with_teachpoints
from tests.real_device_path import connected, the_real_device_path

pytestmark = pytest.mark.asyncio


class RecordingArmDriver(SimTransporterDriver):
    """A sim arm that remembers the order of its bring-up calls."""

    def __init__(self, name: str, *, connected: bool) -> None:
        super().__init__(name)
        self.calls: List[str] = []
        self._connected = connected

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self.calls.append("connect")
        self._connected = True

    async def initialize(self, request: InitializeRequest) -> None:
        self.calls.append("initialize")
        await super().initialize(request)

    async def home(self, request: HomeRequest) -> None:
        self.calls.append("home")
        await super().home(request)

    async def move_to_safe(self, request: MoveToSafeRequest) -> None:
        self.calls.append("move_to_safe")
        await super().move_to_safe(request)


async def _arm_with(driver: RecordingArmDriver) -> Transporter:
    store = teachpoint_file(create_test_teachpoints(["pad1"]))
    the_real_device_path().simulate_with("robot1", driver)
    return await connected(with_teachpoints(Transporter("robot1"), store))


async def _arm_linked_through_orca(driver: RecordingArmDriver) -> Transporter:
    """An arm whose link orca opened this session: the forwarding driver knows only what went through it."""
    arm = await _arm_with(driver)
    await arm.connect()
    driver.calls.clear()
    return arm


class TestTransporterBringUp:
    async def test_bring_up_moves_the_arm_nowhere(self) -> None:
        driver = RecordingArmDriver("robot1", connected=True)
        await (await _arm_with(driver)).initialize()
        moves = [c for c in driver.calls if c in ("home", "move_to_safe")]
        assert moves == [], (
            "bring-up moved the arm; on the bench this faulted the controller "
            f"before any thread ran. Calls: {driver.calls}"
        )

    async def test_bring_up_does_not_home_the_arm(self) -> None:
        """`initialize` readies the arm; `home` moves it. Keeping them apart is
        what lets an operator bring an arm up with a hand on the deck."""
        driver = RecordingArmDriver("robot1", connected=True)
        await (await _arm_linked_through_orca(driver)).initialize()
        assert driver.calls == ["initialize"]

    async def test_bring_up_opens_the_link_first_when_it_is_down(self) -> None:
        driver = RecordingArmDriver("robot1", connected=False)
        await (await _arm_with(driver)).initialize()
        assert driver.calls == ["connect", "initialize"], (
            "an arm whose link is down must be connected before it is told to "
            f"initialize. Calls: {driver.calls}"
        )

    async def test_bring_up_leaves_an_open_link_alone(self) -> None:
        driver = RecordingArmDriver("robot1", connected=True)
        await (await _arm_linked_through_orca(driver)).initialize()
        assert "connect" not in driver.calls, (
            "re-connecting a live PreciseFlex orphans the attached session; "
            f"calls: {driver.calls}"
        )

    async def test_homing_is_still_reachable_on_its_own(self) -> None:
        """Granular does not mean unreachable: the verb bring-up stopped calling
        is the same one an operator asks for when the arm needs to move."""
        driver = RecordingArmDriver("robot1", connected=True)
        arm = await _arm_linked_through_orca(driver)
        await arm.initialize()

        await arm.driver.home(HomeRequest())

        assert driver.calls == ["initialize", "home"]

    async def test_a_fresh_server_session_connects_and_initializes_an_arm_whose_link_is_already_open(self) -> None:
        """Orca answers for what it sent this session. A link its orca-client already holds open is connected again."""
        driver = RecordingArmDriver("robot1", connected=True)
        await (await _arm_with(driver)).initialize()
        assert driver.calls == ["connect", "initialize"]
