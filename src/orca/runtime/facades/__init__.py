"""Concrete implementations of the ISystemRuntime sub-facades.

Each facade wraps one engine concern (variable store, labware location,
device registry, thread mutation, etc.) and encodes the UI-boundary rules
(guards, confirmation, snapshot building) so UIs never touch internal types.

See `orca.runtime.runtime_interface` for the abstract contracts.
"""
