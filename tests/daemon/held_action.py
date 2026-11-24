"""A release flag a test and the workflow its daemon imports both see.

pytest imports a test file under its bare name and the package imports it as `tests.daemon.<file>`,
so a flag kept in the test file exists twice. This module is imported by one name everywhere.
"""

HOLD = {"released": False}
