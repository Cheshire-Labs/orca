"""A misspelled field on a grip-profile edit is refused, not dropped."""

import pytest
from pydantic import ValidationError

from orca.runtime.move_parameter_models import GripProfilePatchRequest


def test_a_wrong_field_name_is_refused() -> None:
    with pytest.raises(ValidationError):
        GripProfilePatchRequest.model_validate({"clear": ["z_offset"], "reasn": "typo"})
