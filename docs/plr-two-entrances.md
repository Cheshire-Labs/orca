# PLR's two entrances (orca ↔ pylabrobot)

Read this before reasoning about labware, decks, or liquid-handler state. PyLabRobot (PLR) is used as **two different things at two different points, with the same class names**. That double-entrance is the #1 source of "which object am I looking at" confusion, for humans and AI alike.

## The two entrances

1. **PLR as a definitions library — inside orca, at labware creation.**
   `PlateTemplate.create_instance()` (`orca-core/src/orca/resource_models/labware.py`) → `resolve_plr_factory()` → a `cheshire_drivers.plr.plates` factory (a raw PLR factory wrapped in `PLRPlateAdapter`) → a real pylabrobot `Plate`. This PLR object lives **inside the orca `PlateInstance`**. orca-core itself imports pylabrobot **zero** times; it only ever holds the adapter / `IPlate` interface.

2. **PLR as a driver — inside the liquid handler.**
   `PLRLiquidHandlerWrapper.configure_deck()` (`cheshire-drivers/src/cheshire_drivers/plr_wrappers.py`) builds a **separate** pylabrobot `Deck` + `Plate` objects inside `self._lh.deck`, from `DeckLayoutConfig.resources`.

So for one labware instance named e.g. `"reservoir-a1b2c3d4"` there can be **two distinct pylabrobot `Plate` objects**: one in the orca instance (entrance 1), one in the driver's deck (entrance 2). They are linked only by **name (a string)**, never by object identity. The linking name is the **instance name** (`<template>-<id prefix>`, minted at `create_instance`), not the template name -- two racks of one template are two driver resources with two names. The driver resolves `aspirate(labware="reservoir-a1b2c3d4")` by name against its **own** deck (`self._lh.deck.get_resource(...)`) and never sees the orca instance's plate.

## Three PLR resource trees, identical types

There are three pylabrobot resource trees in play, all using the same `Plate` / `Deck` / `Resource` types:
- **(a)** the orca labware instance's adapter (entrance 1),
- **(b)** the transporter sim world — `cheshire_drivers/sims.py` `self._world`, which reparents labware via `assign_child_resource`,
- **(c)** the liquid-handler driver's deck — `self._lh.deck` (entrance 2).

Inside cheshire-drivers, raw PLR is aliased `PLRPlate` / `PLRDeck` / `PLRResource`. **That alias disambiguates the type, not which tree an instance lives in.** Two `PLRPlate` instances both named `"reservoir"` in trees (a) and (c) look identical. When you read code touching a `PLRPlate`, identify which tree the instance belongs to before drawing conclusions.

## How state crosses orca ↔ driver (verified by execution)

The driver's deck (tree c) gets its **carrier skeleton** from `configure_deck` once at runtime start; labware enters and leaves it dynamically: `LiquidHandler._do_notify_placed` calls `add_deck_labware` on arrival, `_do_notify_picked` calls `remove_deck_labware` on departure, and `System.project_labware_on_lh_deck` / `retract_labware_from_lh_deck` push a single asserted arrival or departure the same way (ledger → driver). `reconcile_lh_deck_occupancy` pushes the WHOLE ledger-side occupancy, wiping and rebuilding the deck; it is for a driver world whose occupancy is unknown (a fresh deck world, a rebuilt session, a boot), never for a change that concerns one labware. State moves across the name-link by push/pull:
- **PUSH in:** `configure_deck(labware_state=...)` seeds initial well volumes onto the driver's plate.
- **Driver does the per-op math on its own plate:** a well seeded at 100 µL, after `aspirate` 10 µL, reads 90 µL on the driver's plate (PLR volume tracker). *(verified empirically)*
- **PULL out:** every op returns `LabwareStateResponse.labware_state`; orca reads it under `trust_driver_state` and projects onto its own bookkeeping.
- **`move_plate` does not create a resource.** It requires the plate to already exist in `self._lh.deck` (`_get_plate` → `get_resource`) and only reparents it within tree (c). *(verified)*

## Deck-resident labware (gap CLOSED)

A labware declared only in the workflow, e.g. a reservoir via `@orca.thread(start=("lh/carrier-25-0", REUSE_EXISTING), end=(..., LEAVE_IN_PLACE))`, now reaches entrance 2 through the runtime bridge: placement fires `add_deck_labware`, reuse-bind projects the resident on its own through `project_labware_on_lh_deck`, and lazy init reconciles the whole deck once per fresh driver world. `DeckLayoutConfig` **rejects** labware entries outright (carriers only); routing labware as a thread or `REUSE_EXISTING` resident is the one way onto the deck.

## One-line model

Two PLR plates per labware, one in orca's instance and one in the driver's deck, linked by the unique **instance name**; the driver only ever uses its own; placement/pick/reconcile keep the driver's deck in step with the ledger.
