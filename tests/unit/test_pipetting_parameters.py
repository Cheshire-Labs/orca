"""Which layer decides each pipetting parameter, and how a reader finds out."""

from dataclasses import fields

from cheshire_drivers.liquid_handler_models import (
    SEED_PIPETTING_PARAMETERS,
    PipettingParameters,
    PipettingPatch,
)
from cheshire_drivers.pipetting import MixParams, PipettingProfile

from orca.runtime.pipetting_parameters import (
    DEFAULTS,
    LIQUID_CLASS,
    STEP,
    profile_patch,
    resolve_authored_pipetting,
    resolve_pipetting,
    resolve_wire_pipetting,
)


def _resolve(**kwargs: PipettingProfile | None):
    return resolve_authored_pipetting(**kwargs)


class TestResolution:
    def test_a_step_that_states_nothing_gets_the_defaults(self) -> None:
        resolved = _resolve()

        assert resolved.parameters == SEED_PIPETTING_PARAMETERS
        assert set(resolved.sources.values()) == {DEFAULTS}

    def test_a_liquid_class_supplies_what_the_step_does_not(self) -> None:
        resolved = _resolve(liquid_class=PipettingProfile(flow_rate=20.0, height=2.0))

        assert resolved.parameters.flow_rate == 20.0
        assert resolved.parameters.height == 2.0

    def test_the_step_wins_over_the_liquid_for_a_field_both_name(self) -> None:
        """The layering the model exists for. Glycerol is drawn slowly wherever
        it goes; this one call is slower still, and only the call knows that."""
        resolved = _resolve(
            liquid_class=PipettingProfile(flow_rate=20.0, height=2.0),
            technique=PipettingProfile(flow_rate=5.0),
        )

        assert resolved.parameters.flow_rate == 5.0
        assert resolved.parameters.height == 2.0, "the step said nothing about height"

    def test_a_step_can_state_a_field_the_liquid_never_mentions(self) -> None:
        """Neither layer is missing a knob the other has: a blow-out is as much a
        step decision as a liquid one."""
        resolved = _resolve(technique=PipettingProfile(blow_out=True))

        assert resolved.parameters.blow_out is True

    def test_a_mix_crosses_from_the_authored_profile_to_the_wire(self) -> None:
        resolved = _resolve(
            liquid_class=PipettingProfile(
                mix=MixParams(volume=50.0, repetitions=3, flow_rate=100.0)
            )
        )

        assert resolved.parameters.mix is not None
        assert resolved.parameters.mix.volume == 50.0
        assert resolved.parameters.mix.repetitions == 3
        assert resolved.parameters.mix.flow_rate == 100.0

    def test_resolving_leaves_the_defaults_it_started_from_alone(self) -> None:
        _resolve(technique=PipettingProfile(flow_rate=99.0))

        assert SEED_PIPETTING_PARAMETERS.flow_rate is None


class TestSources:
    def test_every_field_starts_out_credited_to_the_defaults(self) -> None:
        sources = _resolve().sources

        assert set(sources) == set(PipettingParameters.model_fields)
        assert set(sources.values()) == {DEFAULTS}

    def test_each_layer_is_credited_for_the_fields_it_set(self) -> None:
        resolved = _resolve(
            liquid_class=PipettingProfile(flow_rate=20.0, height=2.0),
            technique=PipettingProfile(flow_rate=5.0),
        )

        assert resolved.sources["flow_rate"] == STEP
        assert resolved.sources["height"] == LIQUID_CLASS
        assert resolved.sources["blow_out"] == DEFAULTS

    def test_a_named_profile_labels_itself(self) -> None:
        """A plate pipetted oddly should name the profile that decided it, not
        just the slot the profile sat in."""
        resolved = _resolve(
            liquid_class=PipettingProfile(name="glycerol", flow_rate=20.0),
            technique=PipettingProfile(name="gentle top-up", flow_rate=5.0),
        )

        assert resolved.sources["flow_rate"] == "gentle top-up"

    def test_a_name_alone_credits_no_field(self) -> None:
        """Naming a profile is not stating a parameter."""
        resolved = _resolve(liquid_class=PipettingProfile(name="water"))

        assert set(resolved.sources.values()) == {DEFAULTS}


class TestResolvePipettingShorthand:
    def test_it_returns_the_record_a_request_carries(self) -> None:
        parameters = resolve_pipetting(
            liquid_class=PipettingProfile(flow_rate=20.0),
            technique=PipettingProfile(height=6.0),
        )

        assert parameters == PipettingParameters(flow_rate=20.0, height=6.0)


class TestWireResolution:
    def test_an_ad_hoc_surface_folds_the_same_way_a_workflow_does(self) -> None:
        """REST and MCP hand their layers in as wire patches. They must land on
        the same record, or a plate is pipetted one way under a workflow and
        another way when an operator drives it by hand."""
        from cheshire_drivers.liquid_handler_models import PipettingPatch

        by_wire = resolve_wire_pipetting(
            liquid_class=PipettingPatch(flow_rate=20.0, height=2.0),
            technique=PipettingPatch(flow_rate=5.0),
        )
        by_workflow = resolve_pipetting(
            liquid_class=PipettingProfile(flow_rate=20.0, height=2.0),
            technique=PipettingProfile(flow_rate=5.0),
        )

        assert by_wire == by_workflow

    def test_naming_no_layers_gets_the_defaults(self) -> None:
        assert resolve_wire_pipetting() == SEED_PIPETTING_PARAMETERS


class TestProfileAndPatchStayInStep:
    """`profile_patch` maps the authored profile onto the wire patch by hand.

    A field added to the profile and not to the mapping is settable in a
    workflow and silently never reaches the machine, which is the worst shape a
    parameter bug can take: the caller states a number and nothing refuses it.
    """

    def test_the_two_shapes_carry_the_same_fields(self) -> None:
        authored = {f.name for f in fields(PipettingProfile)} - {"name"}

        assert authored == set(PipettingPatch.model_fields)

    def test_every_field_a_profile_states_survives_the_conversion(self) -> None:
        stated = PipettingProfile(
            name="glycerol",
            height=2.0,
            flow_rate=20.0,
            blow_out=True,
            blow_out_volume=10.0,
            blow_out_flow_rate=5.0,
            mix=MixParams(volume=50.0, repetitions=3, flow_rate=100.0),
        )

        contributed = profile_patch(stated).model_dump(exclude_none=True)

        assert set(contributed) == set(PipettingPatch.model_fields), (
            "a field the profile stated did not reach the patch"
        )

    def test_a_name_is_the_one_thing_that_does_not_cross(self) -> None:
        """It labels the layer in `sources`; no machine acts on it."""
        assert "name" not in profile_patch(PipettingProfile(name="water")).model_dump()


class TestFalsyValuesSurviveTheFold:
    def test_a_step_can_switch_off_what_the_liquid_turned_on(self) -> None:
        """False is a value a layer states; None is a layer saying nothing. Fold
        them the same way and a step can never undo a wider layer."""
        resolved = _resolve(
            liquid_class=PipettingProfile(blow_out=True),
            technique=PipettingProfile(blow_out=False),
        )

        assert resolved.parameters.blow_out is False
        assert resolved.sources["blow_out"] == STEP

    def test_a_zero_is_a_number_and_not_an_absence(self) -> None:
        resolved = _resolve(technique=PipettingProfile(height=0.0))

        assert resolved.parameters.height == 0.0
        assert resolved.sources["height"] == STEP


class TestTheSeedIsNeverHandedOut:
    def test_resolving_nothing_returns_a_record_the_caller_may_keep(self) -> None:
        """A caller holding the shared seed could change what every later
        resolution starts from."""
        resolved = resolve_pipetting()

        assert resolved is not SEED_PIPETTING_PARAMETERS
        assert resolved == SEED_PIPETTING_PARAMETERS
