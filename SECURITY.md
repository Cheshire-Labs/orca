# Security

**This release is meant for internal use only.** Run it on a lab machine or an
internal network that you control, operated by people you trust. It is not built
to be exposed to the internet, to untrusted users, or to more than one tenant.

Report a vulnerability to support@cheshirelabs.io. Please do not open a public
issue for one.

## The daemon has no authentication

`orca start` binds 127.0.0.1, and every route on it is open to every process
running on that computer as any user. There is no API key and no per-request
check. A caller that reaches the port can move an arm, dispense a reagent, abort
a run, and run arbitrary Python as the user who started the daemon.

Workflows, methods and actions are Python source, and the daemon imports and runs
them. `POST /operations/insert-method`, `/insert-action`, `/replace-method` and
`/replace-action` accept source over the wire and execute it. The import and
builtin restrictions in `orca/runtime/code_injection.py` catch a mistake, not an
attacker, and they are not a sandbox. A deployment profile's `computed`
expression is the same: it runs with the deployment's own reach.

So run the daemon on a machine only your operators use, do not run untrusted
code as the same user, and do not expose the port through a tunnel, a proxy or a
container port map.

## Physical risk

This software drives real laboratory instruments. A command can move a robotic
arm or dispense a reagent, so a mistaken or hostile call destroys samples and can
damage hardware. Operator device writes default to `LIVE`, which is deliberate:
an operator typing `initialize` means the real device.
