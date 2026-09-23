"""Tests for device capability validation.

There is no static `DEVICE_CAPABILITIES` dict any more; validation
resolves against per-device advertised interfaces + capabilities. Each test
in this file preserves the behavioral intent of its earlier counterpart
(every device-type still verified, every assertion still meaningful), just
routed through `validate_capability_for_device(interfaces, capabilities,
command)` instead of the deleted dict.
"""

import pytest

from orca.gateway.registry.capabilities import (
    NAME_TO_INTERFACE,
    validate_capability_for_device,
)


class TestCapabilities:
    """Tests for capability validation functions."""

    def test_validate_capability_returns_true_for_valid_command(self):
        """validate_capability returns True for commands declared on the
        advertised interface contract."""
        # Shaker advertises IShaker; "shake" is on IShakerDriver.
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"IShaker"}),
            capabilities_advertised=frozenset(),
            command="shake",
        ) is True
        # "stop" is also on IShakerDriver.
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"IShaker"}),
            capabilities_advertised=frozenset(),
            command="stop",
        ) is True
        # Centrifuge advertises ICentrifuge; "centrifuge" is on ICentrifugeDriver.
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"ICentrifuge"}),
            capabilities_advertised=frozenset(),
            command="centrifuge",
        ) is True
        # Thermocycler advertises IThermocycler; "run_protocol" is on IThermocyclerDriver.
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"IThermocycler"}),
            capabilities_advertised=frozenset(),
            command="run_protocol",
        ) is True

    def test_validate_capability_returns_false_for_invalid_command(self):
        """validate_capability returns False for commands not on the
        advertised interface and not in the capabilities set."""
        # Shaker does not support centrifuge.
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"IShaker"}),
            capabilities_advertised=frozenset(),
            command="centrifuge",
        ) is False
        # Centrifuge does not support shake.
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"ICentrifuge"}),
            capabilities_advertised=frozenset(),
            command="shake",
        ) is False
        # Random command.
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"IShaker"}),
            capabilities_advertised=frozenset(),
            command="fly_to_moon",
        ) is False

    def test_thermocycler_supports_full_op_set(self):
        """A device advertising IThermocycler can call every mutation + getter."""
        tc_interfaces = frozenset({"IThermocycler"})
        for command in {
            "open_lid", "close_lid",
            "set_block_temperature", "set_lid_temperature",
            "deactivate_block", "deactivate_lid",
            "run_protocol",
            "get_block_current_temperature", "get_block_target_temperature",
            "get_lid_current_temperature", "get_lid_target_temperature",
            "get_lid_open", "get_lid_status", "get_block_status",
            "get_hold_time",
            "get_current_cycle_index", "get_total_cycle_count",
            "get_current_step_index", "get_total_step_count",
        }:
            assert validate_capability_for_device(
                tc_interfaces, frozenset(), command
            ) is True, f"thermocycler should support {command!r}"
        # Thermocycler does not support a foreign command.
        assert validate_capability_for_device(
            tc_interfaces, frozenset(), "shake"
        ) is False

    def test_validate_capability_returns_false_for_unknown_interface(self):
        """validate_capability returns False when the advertised interface
        name does not appear in NAME_TO_INTERFACE.

        Pre-Stream-A this test was about unknown device_type strings.
        Behavioral intent (unknown contract -> reject every command) is
        preserved here.
        """
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"IUnknownInterface"}),
            capabilities_advertised=frozenset(),
            command="shake",
        ) is False

    def test_validate_capability_accepts_capabilities_advertised_extras(self):
        """Auto-derived vendor extras (e.g. PLR centrifuge's set_acceleration)
        flow through `capabilities_advertised`. validate must accept them
        even when they are not on any declared interface."""
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"ICentrifuge"}),
            capabilities_advertised=frozenset({"set_acceleration", "stop"}),
            command="set_acceleration",
        ) is True

    def test_name_to_interface_registry_structure(self):
        """NAME_TO_INTERFACE contains the expected interface entries.

        Pre-Stream-A this test verified DEVICE_CAPABILITIES had entries for
        each device type; behavioral intent (every supported device family
        is registered) preserved here.
        """
        assert isinstance(NAME_TO_INTERFACE, dict)
        # Each device family has an interface entry
        for required in {
            "IShaker", "ICentrifuge", "IThermocycler", "ISealer", "ITransporter",
            "ILiquidHandler",
        }:
            assert required in NAME_TO_INTERFACE, (
                f"NAME_TO_INTERFACE missing required interface {required!r}"
            )
        # Each entry is a class
        for name, cls in NAME_TO_INTERFACE.items():
            assert isinstance(name, str)
            assert isinstance(cls, type), f"NAME_TO_INTERFACE[{name!r}] is not a class"

    def test_stationary_devices_have_stop(self):
        """Stationary device types (shaker, centrifuge, sealer) support 'stop'.

        Pre-Stream-A this also asserted 'get_status'; that was a phantom in
        the static dict (no driver implementation). Removed from the
        assertion since asserting a phantom would always fail post-Stream-A.
        """
        # Shaker.stop is on IShakerDriver
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"IShaker"}),
            capabilities_advertised=frozenset(),
            command="stop",
        ) is True
        # Sealer's stop comes from concrete-driver extras (PLR sealer wrapper).
        # The sim sealer also has it as an extra. Either way validates via the
        # capabilities path.
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"ISealer"}),
            capabilities_advertised=frozenset({"stop"}),
            command="stop",
        ) is True
        # PLR centrifuge advertises stop as a vendor extra.
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"ICentrifuge"}),
            capabilities_advertised=frozenset({"stop"}),
            command="stop",
        ) is True

    def test_transporter_has_halt_and_position_queries(self):
        """Transporter uses 'halt' for emergency stop and 'get_*_position'
        queries instead of generic get_status."""
        for command in {"halt", "get_joint_position", "get_cartesian_position"}:
            assert validate_capability_for_device(
                interfaces_advertised=frozenset({"ITransporter"}),
                capabilities_advertised=frozenset(),
                command=command,
            ) is True

    def test_property_metadata_not_invokable_as_command(self):
        """@property members on an interface (single_carriage, name) are
        engine-read metadata, not wire commands. The gate reads
        interface_command_names, which excludes @property descriptors, so a
        POST .../command naming one is rejected even though the property is
        part of the interface surface."""
        transporter = frozenset({"ITransporter"})
        for prop in {"single_carriage", "name"}:
            assert validate_capability_for_device(
                transporter, frozenset(), prop
            ) is False, f"{prop!r} is metadata and must not validate as a command"

    def test_liquid_handler_has_expected_commands(self):
        """A device advertising ILiquidHandler can call every LH method."""
        lh_interfaces = frozenset({"ILiquidHandler", "IProtocolRunner"})
        for command in {
            "configure_deck", "get_deck_state",
            "pick_up_tips", "drop_tips",
            "aspirate", "dispense",
            "pick_up_tips96", "drop_tips96",
            "aspirate96", "dispense96",
            "run_protocol",  # inherited from IProtocolRunner
        }:
            assert validate_capability_for_device(
                interfaces_advertised=lh_interfaces,
                capabilities_advertised=frozenset(),
                command=command,
            ) is True, f"LH should support {command!r}"

    def test_shaker_only_supports_shake_among_action_commands(self):
        """Shaker supports shake but not centrifuge / seal / transport."""
        shaker_interfaces = frozenset({"IShaker"})
        assert validate_capability_for_device(
            shaker_interfaces, frozenset(), "shake"
        ) is True
        for foreign in {"centrifuge", "seal", "transport"}:
            assert validate_capability_for_device(
                shaker_interfaces, frozenset(), foreign
            ) is False, f"shaker should NOT support {foreign!r}"


class TestManualMotionCapabilities:
    """The six granular motion interfaces route manual gripper/channel control.

    A device advertising one of them must accept exactly that interface's commands and reject a
    motion command it does not carry, so an operator or AI cannot dispatch a capability the mounted
    hardware lacks (a force jaw on a width-jaw gripper, a rotation on a robot with none).
    """

    def test_each_motion_interface_accepts_its_own_command(self):
        for interface, command in (
            ("IPipetteMotion", "move_channel_to"),
            ("IPipetteMotion", "get_channel_position"),
            ("IGripperMotion", "move_gripper_to"),
            ("IGripperPosition", "get_gripper_position"),
            ("IForceGripperJaw", "grip_with_force"),
            ("IWidthGripperJaw", "set_jaw_width"),
            ("IGripperRotation", "rotate_gripper"),
        ):
            assert validate_capability_for_device(
                interfaces_advertised=frozenset({interface}),
                capabilities_advertised=frozenset(),
                command=command,
            ) is True, f"{command} should be valid on {interface}"

    def test_a_gripper_command_is_refused_without_its_interface(self):
        """A width-jaw gripper (STAR iSWAP) must not accept the force-jaw command a Flex uses, and
        vice versa: the two jaw models are different hardware."""
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"IWidthGripperJaw"}),
            capabilities_advertised=frozenset(),
            command="grip_with_force",
        ) is False
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"IForceGripperJaw"}),
            capabilities_advertised=frozenset(),
            command="set_jaw_width",
        ) is False

    def test_a_homeable_device_accepts_home_without_being_a_transporter(self):
        """Homing stopped being an arm-only verb, so a liquid handler that
        declares IHomeable has to be able to run the command. Without the
        interface in the map there is nothing to resolve `home` against, and the
        device is refused a capability it advertises."""
        assert validate_capability_for_device(
            interfaces_advertised=frozenset(
                {"ILiquidHandler", "IHomeable", "IGantryParking"}
            ),
            capabilities_advertised=frozenset(),
            command="home",
        ) is True

    def test_an_arm_still_homes_through_its_own_interface(self):
        """ITransporterDriver subclasses IHomeableDriver, so an arm resolved
        `home` before IHomeable had a map entry of its own. Pinned so adding
        that entry cannot quietly move which interface answers for an arm."""
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"ITransporter"}),
            capabilities_advertised=frozenset(),
            command="home",
        ) is True

    def test_home_is_refused_on_a_device_that_never_claimed_it(self):
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"IShaker"}),
            capabilities_advertised=frozenset(),
            command="home",
        ) is False

    def test_a_liquid_handler_without_motion_refuses_motion(self):
        """Declaring ILiquidHandler alone (a pipetting-only driver) must not admit manual motion."""
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"ILiquidHandler"}),
            capabilities_advertised=frozenset(),
            command="move_channel_to",
        ) is False

    def test_only_a_head_with_a_sensor_is_allowed_to_probe(self):
        """Liquid sensing is hardware: a Flex or a STAR reads pressure as a tip
        descends, an OT-2 has no sensor. The one that cannot is refused here, at
        the gateway, rather than at a deck that would fail the command."""
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"ILiquidHandler", "ILiquidProbe"}),
            capabilities_advertised=frozenset(),
            command="liquid_probe",
        ) is True
        assert validate_capability_for_device(
            interfaces_advertised=frozenset({"ILiquidHandler"}),
            capabilities_advertised=frozenset(),
            command="liquid_probe",
        ) is False


class TestReadablePropertiesReachTheDevice:
    """A property read is a read, and the gate has to let it through.

    `interface_command_names` drops `@property` members on purpose, so a device
    that genuinely implements `is_initialized` / `is_connected` had its query
    refused with "does not support command". That killed `is_initialized` for as
    long as it shipped, and it is the surface an operator is told to trust for
    ground truth rather than the registry's cache.
    """

    def test_the_two_readable_properties_are_admitted(self) -> None:
        from cheshire_drivers.response_lookup import WIRE_READABLE_PROPERTIES

        for iface in ("IShaker", "ITransporter", "ILiquidHandler"):
            for name in WIRE_READABLE_PROPERTIES:
                assert validate_capability_for_device(
                    frozenset({iface}), frozenset(), name
                ), f"{iface} refuses to answer {name}"

    def test_engine_metadata_properties_stay_refused(self) -> None:
        """Widening the gate must not turn every property into a wire call.

        `name` and `single_carriage` are scheduling metadata the engine reads in
        process; serving them over the wire would invite callers to treat them
        as commands.
        """
        for name in ("name", "single_carriage"):
            assert not validate_capability_for_device(
                frozenset({"ITransporter"}), frozenset(), name
            ), f"{name} is engine metadata and must not be invokable"
