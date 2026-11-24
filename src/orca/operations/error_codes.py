"""Every error code Orca's REST API sends, each with the one HTTP status it is sent with."""

from enum import unique

from typing_extensions import Self

from orca.str_enum import StrEnum


class WireErrorCode(StrEnum):
    """The base every error-code enum extends. A member is the code string and carries its HTTP status."""

    http_status: int

    def __new__(cls, code: str, http_status: int) -> Self:
        member = str.__new__(cls, code)
        member._value_ = code
        member.http_status = http_status
        return member


@unique
class ErrorCode(WireErrorCode):
    """The codes both servers send."""

    INTERNAL_ERROR = ("internal_error", 500)
    HTTP_ERROR = ("http_error", 400)
    AUTH_ERROR = ("auth_error", 401)
    FORBIDDEN = ("forbidden", 403)
    METHOD_NOT_ALLOWED = ("method_not_allowed", 405)
    SERVICE_NOT_READY = ("service_not_ready", 503)
    RUNTIME_NOT_READY = ("runtime_not_ready", 503)
    TIMEOUT = ("timeout", 504)
    SETUP_FILE_INVALID = ("setup_file_invalid", 500)
    CONFIRMATION_REQUIRED = ("confirmation_required", 400)

    VALIDATION_ERROR = ("validation_error", 422)
    RUN_MODE_REQUIRED = ("run_mode_required", 422)
    DEVICE_HAS_NO_REAL_DRIVER = ("device_has_no_real_driver", 422)
    SIM_SPEEDUP_NOT_APPLICABLE = ("sim_speedup_not_applicable", 422)
    NOTHING_TO_CONFIRM = ("nothing_to_confirm", 422)
    WORK_LOST = ("work_lost", 422)
    DANGEROUS_CHECK_FAILED = ("dangerous_check_failed", 400)
    PACKAGE_LOAD_FAILED = ("package_load_failed", 400)
    REBUILD_FAILED = ("rebuild_failed", 400)

    ROUTE_NOT_FOUND = ("route_not_found", 404)
    TOPOLOGY_NOT_FOUND = ("topology_not_found", 404)
    WORKFLOW_NOT_FOUND = ("workflow_not_found", 404)
    METHOD_NOT_FOUND = ("method_not_found", 404)
    THREAD_TEMPLATE_NOT_FOUND = ("thread_template_not_found", 404)
    EXECUTION_NOT_FOUND = ("execution_not_found", 404)
    CHECK_RUN_NOT_FOUND = ("check_run_not_found", 404)
    THREAD_NOT_FOUND = ("thread_not_found", 404)
    SUBMISSION_NOT_FOUND = ("submission_not_found", 404)
    MANUAL_STEP_NOT_FOUND = ("manual_step_not_found", 404)
    VARIABLE_NOT_FOUND = ("variable_not_found", 404)
    RESERVATION_NOT_FOUND = ("reservation_not_found", 404)
    INCIDENT_NOT_FOUND = ("incident_not_found", 404)
    HELD_COMMAND_NOT_FOUND = ("held_command_not_found", 404)
    PROFILE_NOT_FOUND = ("profile_not_found", 404)
    LABWARE_NOT_FOUND = ("labware_not_found", 404)
    LABWARE_TYPE_NOT_FOUND = ("labware_type_not_found", 404)
    LABWARE_TEMPLATE_NOT_FOUND = ("labware_template_not_found", 404)
    LOCATION_NOT_FOUND = ("location_not_found", 404)
    DEVICE_NOT_FOUND = ("device_not_found", 404)
    TRANSPORTER_NOT_FOUND = ("transporter_not_found", 404)
    LIQUID_HANDLER_NOT_FOUND = ("liquid_handler_not_found", 404)
    TEACHPOINT_NOT_FOUND = ("teachpoint_not_found", 404)
    DECK_LAYOUT_NOT_FOUND = ("deck_layout_not_found", 404)
    ACCESS_CONFIG_NOT_FOUND = ("access_config_not_found", 404)
    GRIP_PROFILE_NOT_FOUND = ("grip_profile_not_found", 404)
    MOVE_DEFAULTS_NOT_FOUND = ("move_defaults_not_found", 404)
    MOVER_NOT_FOUND = ("mover_not_found", 404)

    CONFLICT = ("conflict", 409)
    ALREADY_EXISTS = ("already_exists", 409)
    RESOURCE_IN_USE = ("resource_in_use", 409)
    ACTIVE_EXECUTIONS_EXIST = ("active_executions_exist", 409)
    EXECUTION_TERMINAL = ("execution_terminal", 409)
    LOCATION_RESERVED = ("location_reserved", 409)
    SLOT_OCCUPIED = ("slot_occupied", 409)
    START_LOCATION_OCCUPIED = ("start_location_occupied", 409)
    BARCODE_IN_USE = ("barcode_in_use", 409)
    MOVER_HOLDS_NOTHING = ("mover_holds_nothing", 409)
    SEED_LABWARE_READ_ONLY = ("seed_labware_read_only", 409)
    SPAWN_INCOMPATIBLE = ("spawn_incompatible", 409)
    REUSE_THREAD_CANNOT_BE_ENTRY = ("reuse_thread_cannot_be_entry", 409)
    SUBMISSION_TO_PAUSED_EXECUTION = ("submission_to_paused_execution", 409)
    SUBMISSION_BLOCKED_BY_ORPHANED_BACKLOG = ("submission_blocked_by_orphaned_backlog", 409)
    RUN_MODE_MISMATCH = ("run_mode_mismatch", 409)
    OTHER_RUN_MODE_ACTIVE = ("other_run_mode_active", 409)
    DEVICES_NOT_READY = ("devices_not_ready", 409)
    DEVICE_INTERFACE_MISMATCH = ("device_interface_mismatch", 409)
    MODE_SWITCH_DURING_RUN = ("mode_switch_during_run", 409)
    CAPTURE_IN_SIM_REFUSED = ("capture_in_sim_refused", 409)
    DEVICE_UNDER_EXTERNAL_CONTROL = ("device_under_external_control", 409)
    DEVICE_LOCKED = ("device_locked", 409)
    DEVICE_FAULTED = ("device_faulted", 409)
    DEVICE_NOT_BROUGHT_UP = ("device_not_brought_up", 409)
    TIPS_NOT_PRESENT = ("tips_not_present", 409)

    DEVICE_OFFLINE = ("device_offline", 503)
    COMMAND_ERROR = ("command_error", 502)
    COMMAND_NOT_SAVED = ("command_not_saved", 500)
    POSITION_NOT_SAVED = ("position_not_saved", 500)
    CHANNEL_FAILED = ("channel_failed", 502)
    DRIVER_FAILURE = ("driver_failure", 502)
    POSITION_ERROR = ("position_error", 502)
    BATCH_ERROR = ("batch_error", 502)
