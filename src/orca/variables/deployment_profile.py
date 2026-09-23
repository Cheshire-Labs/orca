"""Deployment profile: named bundle of variable values."""

from pydantic import BaseModel, ConfigDict

from orca.variables.errors import OptionValue


class DeploymentProfile(BaseModel):
    """Named bundle of variable values applied at submit time.

    `variables` keys may use the "global." prefix to write into the global
    layer at load_profile time; bare names target the execution partition.
    `computed` is name -> expression.

    Wire shape (REST/MCP/JSON file): identical to this Pydantic schema.
    `extra="forbid"` makes typos fail loud at boundary parse rather than
    silently dropping fields.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    variables: dict[str, OptionValue] = {}
    computed: dict[str, str] = {}
