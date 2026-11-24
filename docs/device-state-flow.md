# How a liquid handler's state flows between Orca and its driver

Written from the code at orca-core `44ad0554` with the two-head and 96-head return change on top, and cheshire-drivers `ceb4a6cf`. Every section ends with the files and lines it was checked against. Paths starting `cheshire_drivers/` are in the cheshire-drivers repo under `src/`. All others are in this repo under `src/orca/`.

## The rule

Orca's record is the one state of a device. The driver holds a copy. Orca writes the record down to the driver, the device does the work, and the result comes back to Orca.

A driver is treated as stateless. No deck, tip or volume report from a driver is written into the record. One thing a driver says does decide what is written: which channels of a command failed, which decides which rows move liquid or a tip. When the record and the driver differ, that is an error. A person is asked to look and to state what is true. Nothing repairs itself silently.

Two rules are behind this: each physical fact has one owner, and every device command goes through one pipeline.

## Words used here

- **The record**: what Orca has stored. It has four parts, listed in the next section.
- **The driver**: the code in cheshire-drivers that talks to the instrument. It runs inside orca-client, a separate process that can be on another computer.
- **The driver's deck**: the driver's in-memory copy of the deck. For a PyLabRobot-wrapped handler (a STAR, an OT-2, every simulated handler) it is a PyLabRobot deck with tip and volume counters. It exists only inside the orca-client process.
- **Known, stale, unknown**: how well the record knows a fact. Known: there is a value and nothing has gone unseen since. Stale: there is a value, but something happened that nobody saw. Unknown: nobody has ever said.
- **Observation gap**: a row in the record that says "nobody saw what happened here". It moves nothing. It turns known into stale.

## 1. What the record holds

1. **Where each labware is.** One ledger, written by moves, register and discharge.
2. **What each labware holds.** Tips left in a rack and volume in each well. These are not stored as numbers. Each completed command is saved as an operation row that says what the command asked the device to do. The contents are worked out from those rows each time they are read.
3. **What each head carries.** Worked out the same way, from the tip rows of that device. A handler can have two heads: the pipetting channels and a 96 head. Each is kept apart and numbers its own channels from 0.
4. **How well each fact is known.** Worked out from the same rows: an observation gap after the last statement makes the fact stale.

An operation row says what was asked, not what was seen. The driver's own report is saved beside it as a "witness" row and is never added in.

Sources:
- Operation rows say what was asked; witness rows are kept apart: `state/command_operations.py:11-15`, `:714-750`.
- A witness row is never folded: `state/projections.py:58-68`, `:424-443`.
- Rack tips worked out from rows: `state/projections.py:176-215`.
- Head tips worked out from rows, the two heads, and known/stale/unknown: `state/mounted.py:43-48`, `:140-241`.
- The gap causes: `state/records.py:299-316`.

## 2. What Orca writes down to the driver, and when

Five driver commands carry the record down. None of them moves the instrument.

- `configure_deck`: the carriers and fixed parts of the deck, from the stored deck layout.
- `reconcile_deck_occupancy`: every labware the record places on this deck, with its volumes and tips. The driver clears every site and rebuilds them from this list.
- `add_deck_labware` and `remove_deck_labware`: one labware, with its volumes and tips.
- `reset_deck_labware`: takes every labware off the driver's deck and keeps the carriers.

| When | What is sent | What it does |
|---|---|---|
| Bring-up before a run | `configure_deck` once per deck, then `reconcile_deck_occupancy` | The driver's deck is built from the record. |
| orca-client connects or reconnects | The same two, again | Orca forgets that the deck was laid out, marks the head and the labware on the device stale (cause: device reconnect), then rebuilds the driver's deck from the record. No motion. Bring-up and homing still need their own command. |
| An operator's `initialize` or `connect` | The same two, again | The driver may have a new session with an empty deck, so it is rebuilt. After a `disconnect` Orca only forgets the layout. |
| A labware is registered, spawned, or set down on a deck site | `add_deck_labware` for that one labware | Its neighbours are not touched. If the deck still owes its first full push, the full push is sent instead. |
| A labware's thread ends, or an operator discharges it | `remove_deck_labware` for that one labware, on the world in force | It is taken off that driver deck. An operator's discharge then marks every deck world: the others still hold it until their next command, or an operator's `get_deck_state`, rebuilds them from the record. |
| An operator states a rack's tips or a plate's volumes | `add_deck_labware` for that one labware, replacing the driver's copy | The driver's copy gets the stated contents. |
| An operator runs reconcile-deck | A comparison first, then the same two | See section 4. |
| An operator clears all labware | `reset_deck_labware`, on every deck world the handler has laid out | Every labware is taken off the record and off each driver deck that answers; one that does not is logged and skipped. Then every deck world is marked. The reset also takes off labware loaded outside Orca, which no record holds. |
| Server restart | Nothing at restart | Labware is put back at its stored positions and every labware and every head is marked stale (cause: runtime restart). Every driver world is marked, so each is rebuilt from the record before its next command. |

Volumes and tips in a push always come from the record. A labware the record knows nothing about is sent with no contents, and the driver keeps its own default.

What is not written down:

- **The tips on the head.** The push has no field for them. On a PyLabRobot-wrapped handler `configure_deck` builds the driver a new, empty head. So after a connect, a reconnect or an operator's `initialize` the driver's head is empty while the record still holds the tips. A connect or reconnect marks the record's head stale; an operator's `initialize` does not. A `reconcile_deck_occupancy` on its own keeps the driver's head and empties the rack spots that head holds.
- **An operator's statement of what the head carries.** It changes the record only.

Sources:
- `configure_deck` once per deck world: `devices/devices.py:396-426`. Forgetting it: `devices/devices.py:385-394`.
- The full push, built from the record: `system/system.py:1062-1099`, `:1006-1060`. Contents with no fallback to the template: `system/system.py:1076-1080`, `resource_models/labware.py:487-500`.
- Bring-up lays out each deck: `system/system.py:605-629`, `:635-643`.
- Connect and reconnect: `runtime/system_runtime.py:1146-1166`, `:1198-1207`, `:1239-1256`; `runtime/facades/devices.py:369-383`, `:398-404`.
- Operator `initialize`, `connect`, `disconnect`: `operations/device.py:62`, `runtime/facades/devices.py:244-257`.
- One labware on arrival: `resource_models/labware_placement.py:74-110`, `system/system.py:1198-1222`, `:1283-1316`, `devices/devices.py:492-509`.
- One labware on leaving: `resource_models/labware_placement.py:148-162`, `runtime/facades/labware.py:1226`, `system/system.py:1224-1249`, `devices/devices.py:554-563`.
- Operator states contents: `runtime/facades/labware.py:918-930`.
- Server restart: `runtime/system_runtime.py:1041-1087`, `resource_models/labware_placement.py:84-85`.
- The push carries no head field: `cheshire_drivers/liquid_handler_models.py:710`, `:806`.
- The driver's side of the push: `cheshire_drivers/plr_wrappers.py:889`, `:1187-1206`, `:1536`, `:1569`. Its head: `cheshire_drivers/plr_wrappers.py:898-911`, `:1224-1237`.
- An operator's head statement is record only: `runtime/facades/devices.py:210-216`, `state/mounted.py:285-291`.
- Clear all: `devices/devices.py:435-484`.

## 3. What a command sends and what comes back

Every device command goes through one pipeline, whoever sent it: a run, an operator, or Orca itself. The pipeline has three stages, always in this order.

1. **Check.** Is the device known, is the command allowed, is the device free. A tip pick-up is also checked against the record: a pick from spots the record reads empty is refused. A rack that is stale from a restart, a reconnect, an error pause, a switch between sim and live, or an older "operator contradicted" mark is let through with a warning, because a person could have refilled it.
2. **Send.** The command goes to orca-client, which runs it on the driver.
3. **Record.** After the device answers, the operation row is saved. Then where the labware now is. Then the device's deck record. Only then does the command count as done.

For a command that moves a tip, liquid or a plate, the record is written after the device answers. Three placement changes go the other way: a spawn or dispense, a thread's end and a discharge write the record first and then tell the driver. A driver that cannot be reached then leaves the record right.

What comes back from a liquid handler command:

- `success`.
- `labware_state`: the driver's whole deck, every rack's tips and every well's volume. Orca saves this as witness rows. It does not change the record. A deployment can switch the witness rows off per device.
- `per_channel_errors`: the channels that failed, when the driver can say.

A `return_tips96` names no rack. Orca reads its record of the 96 head and saves the return as one row per rack, with exactly the spots the head holds from that rack. Tips from two racks go back to their own racks.

Nothing is pushed to the driver after a tip or liquid command. The driver changed its own copy when it ran the command.

Sources:
- The three stages and their order: `gateway/controller/command_pipeline.py:246-291`, `:356-380`.
- Record first, then the driver: `resource_models/labware_placement.py:82-83`, `:148-162`.
- The rack and spots of a 96-head return: `state/mounted.py:244-271`, `state/projections.py:213-214`; called at `gateway/operator_recorder.py:179-183` and `workflow_models/actions/executable_location_action.py:156-162`.
- The tip check and the stale exception: `resource_models/tip_preflight.py:28-56`, `state/projections.py:393-421`. A run: `workflow_models/actions/device_call_dispatcher.py:329-341`. An operator: `gateway/operator_recorder.py:107-115`.
- A run's save: `workflow_models/actions/device_call_dispatcher.py:142-187`. An operator's save: `gateway/operator_recorder.py:159-194`.
- The answer's shape: `cheshire_drivers/liquid_handler_models.py:523-526`. The driver filling it: `cheshire_drivers/plr_wrappers.py:1713`.
- Witness rows and the per-device switch: `state/command_operations.py:714-750`, `devices/devices.py:142-160`.

## 4. What is compared, and when

A comparison never changes the record. It ends in an incident or an answer that a person acts on.

| What | When | Result |
|---|---|---|
| Where each labware is: the driver's deck against the record | An operator runs compare-deck | A list of differences, and `agrees`. Files nothing. A driver world that is only behind the record, such as one in another run mode after a discharge, is rebuilt from the record first, so the change it missed is not listed. A world in doubt is read as it stands, so after a command that got no answer it shows where the driver left a plate. A driver that reports an empty deck lists no differences, because the push will rebuild it, but `agrees` is false while the record has labware on that deck. |
| The same | An operator runs reconcile-deck | Compared first, each difference filed as a deck reconcile conflict incident, then the record is pushed over the driver. |
| Which tip spots hold a tip: the driver's last witness row against the record | Every full push (bring-up, connect, reconnect, reconcile-deck) | A deck reconcile conflict incident with reason `contents_differ`. The record is still pushed. |
| Tip counts per rack: record, driver's last report, template | Whenever a rack's contents are read | Shown side by side as `layers`. Nothing is filed. |
| The Flex tip sensor against the driver's own head | Before an operator's "retry this command" in a run | A tip the sensor reads and the driver does not track refuses the retry with a divergence error. Tips the driver tracks and the sensor does not read are cleared from the driver's head and the retry goes ahead. The record's head is not changed and not marked. |

What is never compared:

- **Volumes.** No driver reports a volume that did not come from the record.
- **The tips on the head** against the driver's head.
- **Placement at connect or reconnect.** The push goes without a comparison first. Only reconcile-deck compares placement before it pushes.

Sources:
- compare-deck: `system/system.py:847-889`, `:891-895`, `:897-964`; `runtime/facades/devices.py:318-332`, `:346-367`; `operations/device.py:296-309`. What agrees means: `system/system_interface.py:127-135`.
- reconcile-deck compares first: `runtime/facades/devices.py:268-288`.
- Tip spots at every full push: `system/system.py:1051-1054`, `:1165-1187`; `state/contents.py:366-388`.
- The conflict incident: `runtime/system_runtime.py:1271-1275`, `:219-262`.
- `layers`: `state/contents.py:300-325`.
- Before a retry: `workflow_models/actions/device_call_dispatcher.py:343-401`. The reconcile is recorded by name and writes nothing to the record's head: `state/command_operations.py:143-147`.
- Connect and reconnect push with no placement comparison: `runtime/facades/devices.py:369-383`, `:398-404`.

## 5. What happens on each kind of failure

| What happened | What the record does | What a person is asked |
|---|---|---|
| **Refused before it was sent.** A check failed: the tip check, bad parameters, the device is busy, faulted, offline or not brought up. | Nothing. No row. | Nothing. The command answers its refusal. No device fault. |
| **Refused by the driver.** The driver checked its own state and did not act, or could not act on the command at all. This includes every check a driver makes before it asks the instrument: a labware its deck does not have, a tip already on the channel (PyLabRobot's own tip check included), a request the head cannot do, a Flex asked to initialize over a tip it cannot account for. On a Flex with a 96 head that last refusal reads tip presence first, which drives the tip motors inside the pipette; nothing on the deck moves. | Nothing. No row. | Nothing. No device fault. The command answers the check's own error. A busy refusal can be asked again later; an arm waiting on a busy handler does that. |
| **A channel failed** on an aspirate or a dispense. | One row per channel. A failed channel's row moves no liquid. | The command fails after the rows are saved. A run's step fails and the run pauses. An operator gets `channel_failed`. It is never sent again by itself. |
| **A channel failed** on a tip pick-up, a tip drop or a discard of named channels. | One row per channel. A failed channel's row names what it tried and moves no tip: the tip stays in the rack for a pick-up, on the head for a drop or a discard. | The same as a failed liquid channel. |
| **A channel failed** on a discard that named no channels. | No row: nothing says which channels let go. The head, and everything standing at the device, get an observation gap. | The command fails as a failed channel. The same incident as a command the device failed (next row). |
| **The device took the command and then raised an error.** | No row. The command's stated failure effect runs (a failed arm pick leaves the plate at its site). The labware the command names gets an observation gap (cause: operations dropped), and so does the device's head when the command is a tip command (pick up, drop, discard, return). A command that names no labware flags everything standing at the device. | A device fault is latched: Orca will not drive the device for a run until a person clears it or a clean initialize or home runs. A run pauses with its own failure incident. A second incident, the same kind a no-answer files, says the command failed after the device took it and asks a person to look and state what the labware holds, and the head for a tip command. |
| **No answer within the time limit.** | No row. The labware the command names, and the device's head, get an observation gap (cause: operations dropped). A command that names no labware (a shake, a seal, a tip discard) flags everything standing at the device. | A recoverable timeout incident. A run is held first: extend, abort or mark complete. An operator's command is not held: it answers with the incident id. Mark complete applies the command's completed effects, but only while the labware is still where the command started from and its position has not been written since the incident was filed. If a retry or a later move has carried it on, or carried it away and back, mark complete changes nothing and says so. Abort leaves the record as it is, still flagged. |
| **Stopped while the device was running it.** | The same as no answer. The gap is written beside the stop so the stop does not wait on the record store. A shutdown waits for these writes, up to a time limit, and saves the incidents it queued before it returns. | The same incident. |
| **The device answered, and saving the record failed.** | Whatever saved. Positions still follow the hardware. | The command fails with `command_not_saved`. It is never sent again. A person states what is there. |
| **orca-client drops while a command is running.** | The command is held for a grace period. No command of any device is ever sent a second time: when the client returns the command fails as "lost during reconnect". If the client stays away it fails as offline. No row either way. Both count as failed after the device took it, so the labware the command names, and the head for a tip command, get an observation gap when the command fails. | A device fault is latched, and the incident of the row above is filed. A run's thread pauses for a person, whatever the method's failure policy says. When the client returns, the reconnect row of section 2 also marks the head and the labware on the device stale. |
| **orca-client restarts between commands.** | On connect: the head and the labware on the device are marked stale, and the driver's deck is rebuilt from the record. The driver's head is empty; the record keeps its tips. | The stale items are on the worklist of things to confirm. An Opentrons Flex refuses to initialize while its sensor reads a tip its driver has no entry for. |
| **The server restarts.** | Every labware and every head is marked stale. | The same worklist. |

Each head of a handler is in doubt on its own. Only a cut-off tip command writes a head gap, and the row says which head it drives: a cut-off 96-head tip command puts the 96 head in doubt, a cut-off channel tip command the channels. A restart or a reconnect puts both in doubt. A channel command settles only the channel head, and a 96-head drop or return settles only the 96 head.

After an observation gap with the cause "operations dropped", agreeing with the record ("confirm") is refused for that labware and that head. The record may be wrong by what the cut-off command did, so a person has to count and state what is there. A gap from a restart, a reconnect or an error pause can be confirmed.

Stale blocks nothing. A run can continue on stale facts. The one exception is the tip check: after "operations dropped" a pick from spots the record reads empty is refused.

Sources:
- Refused before the send: `gateway/controller/command_pipeline.py:255-256`; `gateway/controller/controller.py:467-481`, `:510-516`, `:654-737`.
- Checks before the instrument is asked: `cheshire_drivers/driver_errors.py:109-161`, `cheshire_drivers/plr_wrappers.py:834-844`. Which head a gap is about: `state/mounted.py:122-128`, `:228-234`, `system/system.py:757-771`, `state/records.py:353-360`. The Flex over a stranded tip: `cheshire_drivers/plr/opentrons_flex.py:1128-1146`.
- Driver refusal and what counts as "moved nothing": `cheshire_drivers/driver_errors.py:31-73`, `gateway/controller/exceptions.py:176-194`, `gateway/controller/controller.py:90-97`. The wait on a busy refusal: `devices/driver_refusal.py:15-23`.
- A failed channel, liquid or tip: `state/command_operations.py:439-457`, `:462-463`, `:484-498`, `:525-576`; `workflow_models/actions/device_call_dispatcher.py:160-162`, `:299-301`; `gateway/operator_recorder.py:189-194`; `gateway/controller/command_pipeline.py:362-364`.
- A discard that names no channels: `state/command_operations.py:466-473`, `:579-602`; `gateway/controller/command_pipeline.py:280-284`, `:365-367`. The driver's per-channel report for tips: `cheshire_drivers/plr_wrappers.py:1367-1430`.
- Raised after taking it: `gateway/controller/command_pipeline.py:273-277`, `:331-347`; the mark: `gateway/controller/controller.py:554-567`, `gateway/controller/exceptions.py:13-29`; the fault: `gateway/controller/controller.py:304-335`, `:346-351`, `gateway/device_fault.py:44-78`.
- No answer: `gateway/controller/command_pipeline.py:263-272`, `:323-329`; `runtime/system_runtime.py:2899-2929`, `:425-439`; `system/system.py:757-771`. Mark complete and its guard: `resource_models/command_effects.py:263-286`, `resource_models/labware_placement.py:121-135`, `runtime/facades/incidents.py:129-160`. A run's hold: `runtime/recoverable_timeout.py:308-314`.
- Stopped: `gateway/controller/command_pipeline.py:259-262`, `:285-288`, `:293-304`. Shutdown waits: `gateway/controller/command_pipeline.py:309-321`, `runtime/system_runtime.py:1446-1459`.
- Record not saved: `gateway/controller/command_pipeline.py:356-380`, `gateway/controller/exceptions.py:96-110`.
- A drop mid-command: `gateway/controller/controller.py:847-871`, `:888-920`, `:922-953`. An offline device pauses the thread: `resource_models/error_policy_overrides.py:29-33`, `gateway/controller/exceptions.py:44`.
- Restart and reconnect gaps: `runtime/system_runtime.py:1041-1087`, `:1198-1207`, `:1239-1256`.
- The Flex refusing to initialize: `cheshire_drivers/plr/opentrons_flex.py:1092-1146`.
- Confirm refused after "operations dropped": `state/records.py:319-335`, `state/mounted.py:300-322`, `runtime/facades/labware.py:750-768`.
- The tip check after "operations dropped": `resource_models/tip_preflight.py:44-56`, `state/projections.py:393-421`.

## 6. Where the record is still wrong or too sure

Nothing here is fixed yet.

- **A retry can change the driver's head without the record knowing.** Before a retry in a run, the driver re-reads its tip sensor. If it tracked tips the sensor does not read, it clears them from its own head and the retry goes ahead. The record's head still says those tips are on, and it is not marked stale. `workflow_models/actions/device_call_dispatcher.py:389-401`, `state/command_operations.py:143-147`.
- **An operator's `initialize` empties the driver's head and leaves the record's head marked known.** `configure_deck` gives a PyLabRobot-wrapped driver a new, empty head. A connect or reconnect marks the record's head stale. An operator's `initialize` does not. `runtime/facades/devices.py:244-257`, `cheshire_drivers/plr_wrappers.py:898-911`.
- **The tips on the head are never written down to the driver and never compared with it.** Section 2 and section 4.
- **A restart or a reconnect puts the 96 head in doubt only once the record has heard of that head.** Most handlers have no 96 head, and a head nothing ever mentioned cannot keep a device stale. A cut-off 96-head tip command does count as the record hearing of it. A cut-off non-tip command (an aspirate, an `initialize`, a plate move) writes no head gap.
- **A vendor protocol runs only on a head the record knows is empty.** `run_protocol` on any liquid handler is refused before it is sent when the head reads `unknown`, `stale` or carries a tip (`resource_models/vendor_protocol_head.py`). Orca cannot see the tips a protocol takes on or off, so a head known empty before it is empty after it. A fresh install and every restart need the head stated empty once. `state/mounted.py:140-241`.
- **Mark complete checks only the labware move.** An incident that stored only a site notice, such as a door, is applied as stored. `resource_models/command_effects.py:278-286`.
- **Mark complete cannot see a move made before a server restart.** The times of position writes are kept in memory. A plate that was moved after the incident, came back to where the command started, and then went through a restart looks unmoved, and mark complete replays the stored move. `resource_models/labware_placement.py:121-135`.
- **A 96-head return from a head the record says is empty credits no rack.** The return is saved as a plain row. If the head did hold tips the record did not know of, their rack keeps reading short. `state/mounted.py:244-271`.
- **A 96-head drop to a named rack still credits the rack's last stated layout.** Only the return credits the spots the head holds. `state/projections.py:213-214`.
