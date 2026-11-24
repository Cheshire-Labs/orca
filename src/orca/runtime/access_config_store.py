"""AccessConfig store errors. Access configs live in ``access_configs.json``."""


class AccessConfigInUseError(Exception):
    """Raised when a delete targets a name still referenced by teachpoints.

    `where` names the teachpoint files that reference it.
    """

    def __init__(
        self, name: str, referencing_count: int = 0, where: tuple[str, ...] = (),
    ) -> None:
        named_in = f" in {', '.join(where)}" if where else ""
        super().__init__(
            f"AccessConfig {name!r} is referenced by {referencing_count} teachpoint(s)"
            f"{named_in}; delete or reassign the teachpoints first."
        )
        self.name = name
        self.referencing_count = referencing_count
        self.where = where
