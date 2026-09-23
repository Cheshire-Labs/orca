"""AccessConfig store errors.

The source-available default persists access configs in SQLite via ``AccessConfigService``
over ``SqliteAccessConfigStore``; a hosted deployment injects a DB-backed store. There
is exactly one registry per process; operator REST/MCP CRUD and runtime
resolution share the same instance.
"""


class ProtectedAccessConfigError(Exception):
    """Raised when a delete targets a name reserved by the deployment.

    The set of protected names is impl-defined: the source-available SQLite store treats
    no names as protected (it carries no defaults concept); a hosted deployment's DB-backed store
    reserves `default_vertical` / `default_horizontal` so operators cannot
    delete the fallback configs the runtime depends on. The facade surface
    lets REST/MCP translate this to a 400.
    """

    def __init__(self, name: str) -> None:
        super().__init__(f"AccessConfig {name!r} is protected and cannot be deleted")
        self.name = name


class AccessConfigInUseError(Exception):
    """Raised when a delete targets a name still referenced by teachpoints.

    Stores that can compute the reference count (a hosted deployment's DB-backed store)
    populate `referencing_count`; stores that cannot (the SQLite default)
    leave it unset and rely on the operator-time guard.
    """

    def __init__(self, name: str, referencing_count: int = 0) -> None:
        super().__init__(
            f"AccessConfig {name!r} is referenced by {referencing_count} teachpoint(s); "
            "delete or reassign the teachpoints first."
        )
        self.name = name
        self.referencing_count = referencing_count
