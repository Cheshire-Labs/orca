"""Variable system for orca-core: VariableRef, Var, VariableStore, profiles."""

from orca.variables.deployment_profile import DeploymentProfile
from orca.variables.errors import (
    OptionValue,
    SubmissionInputsRefusedError,
    UndefinedVariableError,
    VariableTypeError,
    VariableValidationError,
)
from orca.variables.expression import ExpressionError
from orca.variables.resolution import (
    SubmissionOverride,
    VariableBinding,
    VariableResolution,
    VariableSource,
)
from orca.variables.variable_definition import VariableDefinition
from orca.variables.variable_ref import LiteralRef, NamedRef, Var, VariableParam, VariableRef
from orca.variables.variable_store import (
    IVariableResolver,
    IVariableService,
    IVariableStore,
    NullVariableResolver,
    VariableService,
    VariableStore,
)

__all__ = [
    "DeploymentProfile",
    "ExpressionError",
    "IVariableResolver",
    "IVariableService",
    "IVariableStore",
    "LiteralRef",
    "NamedRef",
    "NullVariableResolver",
    "OptionValue",
    "SubmissionInputsRefusedError",
    "SubmissionOverride",
    "UndefinedVariableError",
    "Var",
    "VariableBinding",
    "VariableDefinition",
    "VariableParam",
    "VariableRef",
    "VariableResolution",
    "VariableService",
    "VariableStore",
    "VariableSource",
    "VariableTypeError",
    "VariableValidationError",
]
