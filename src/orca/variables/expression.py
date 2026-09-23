"""Arithmetic expression evaluator for computed variables.

A computed variable is an expression string authored in the deployment profile,
alongside the deployment's own Python. It is evaluated against a namespace
holding only that run's variables, with builtins removed, so any name the
expression does not resolve to a variable is an error rather than a callable.
"""

import re

from orca.variables.errors import OptionValue

_NAME = re.compile(r"[A-Za-z_]\w*")


class ExpressionError(Exception):
    pass


def evaluate_expression(
    expression: str,
    variables: dict[str, OptionValue],
) -> OptionValue:
    """Evaluate an arithmetic expression with variable substitution.

    Raises ExpressionError for undefined variables, non-numeric operands,
    invalid syntax, or division by zero.
    """
    _reject_non_numeric_operands(expression, variables)

    try:
        result = eval(expression, {"__builtins__": {}}, dict(variables))
    except SyntaxError as exc:
        raise ExpressionError(f"Invalid expression syntax: {exc}") from exc
    except ZeroDivisionError as exc:
        raise ExpressionError("Expression resulted in division by zero") from exc
    except NameError as exc:
        raise ExpressionError(
            f"Undefined variable '{exc.name}' in expression"
        ) from exc
    except TypeError as exc:
        raise ExpressionError(f"Invalid operand in expression: {exc}") from exc

    if not isinstance(result, (int, float)):
        raise ExpressionError(
            f"Expression produced {type(result).__name__}, expected numeric"
        )
    return result


def _reject_non_numeric_operands(
    expression: str, variables: dict[str, OptionValue]
) -> None:
    """Fail on a referenced variable that cannot take part in arithmetic.

    Reported before evaluating so the operator gets the variable's name and
    type rather than whatever TypeError the operation happened to produce.
    """
    for name in sorted(set(_NAME.findall(expression)) & variables.keys()):
        value = variables[name]
        if not isinstance(value, (int, float)):
            raise ExpressionError(
                f"Variable '{name}' has type {type(value).__name__}, expected numeric"
            )
