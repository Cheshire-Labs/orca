# Orca Reservation System Deadlock Analysis

## Problem Statement
The SMC assay example gets stuck in an infinite loop where plates move back and forth between the same locations without making progress toward their destinations. No deadlock is being detected by the system.

## System Architecture Overview

### Reservation Flow
1. **Thread requests move** from current location to target location
2. **MoveHandler** creates multiple `MoveAction` objects (one per shortest path)
3. **MoveActionCollectionReservationRequest** submitted to queue with all potential paths
4. **ThreadReservationCoordinator tick loop** (every 0.3s):
   - Processes queue
   - Attempts to reserve each path's target location
   - First successful reservation wins, others released
5. **Thread receives granted reservation** and executes move
6. **Reservation released** when labware picked up

### Deadlock Detection
1. After processing reservations, `_detect_dead_lock()` runs on **rejected** requests only
2. Builds "wait-for graph": edges represent "Thread A waiting for location held by Thread B"
3. Uses NetworkX to find cycles
4. Threads in cycles marked as `deadlocked`
5. Deadlocked threads call `MoveHandler.handle_deadlock()` to reroute to parking pads

### Deadlock Resolution
1. `handle_deadlock()` calls `SystemMap.get_shortest_paths_to_deadlock_resolution()`
2. Returns all shortest paths to **any PlatePad** (parking spots)
3. Filters out paths containing original blocked target
4. Creates new move actions, submits for reservation

## Current Implementation Status

### Starvation Prevention (Recently Implemented)
- ✅ `DeadlockStarvationRegistry` tracks how many times threads are deadlocked
- ✅ Queue sorted by starvation score (highest first) before processing
- ✅ Scores incremented when thread marked as deadlocked
- ✅ Scores reset when reservation granted
- ⚠️ **NOT HELPING** because deadlock is never detected in the first place

### File Changes Made
1. **[deadlock_manager.py:16](src/orca/system/reservation_manager/deadlock_manager.py#L16)** - Fixed typo: `labware_id` → `thread_id`
2. **[deadlock_manager.py:22-25](src/orca/system/reservation_manager/deadlock_manager.py#L22-L25)** - Added `reset_starvation_score()` method
3. **[deadlock_manager.py:57-72](src/orca/system/reservation_manager/deadlock_manager.py#L57-L72)** - ThreadDeadlockDetector uses starvation registry, increments scores
4. **[reservation_manager.py:61-62](src/orca/system/reservation_manager/reservation_manager.py#L61-L62)** - Fixed initialization order
5. **[reservation_manager.py:82-83](src/orca/system/reservation_manager/reservation_manager.py#L82-L83)** - Sort queue by starvation score
6. **[reservation_manager.py:93-95](src/orca/system/reservation_manager/reservation_manager.py#L93-L95)** - Reset starvation score on grant

## SMC Assay Example Layout

### System Topology
- **DDR_1** (robot arm): `bravo_96`, `biotek_1`, `translator_1_start`, `waste_1`, `pad_1`, `pad_2`, `pad_3`
- **DDR_2** (robot arm): `translator_1_end`, `translator_2_start`, shakers, stackers, `pad_4`, `pad_5`, `pad_6`
- **DDR_3** (robot arm): `translator_2_end`, devices, `pad_7`, `pad_8`, `pad_9`
- **Translator_1**: Connects `translator_1_start` ↔ `translator_1_end`
- **Translator_2**: Connects `translator_2_start` ↔ `translator_2_end`

### Observed Behavior (from logs)
```
tips_96: translator_2_start → translator_1_end → translator_2_start → (loop)
sample_plate: pad_7 → translator_2_end → pad_7 → (loop)
```

Both threads trying to reach `bravo_96` for the `sample_to_bead_plate_method`.

### Required Paths
- **tips_96** from `translator_2_start` (DDR_2) to `bravo_96` (DDR_1):
  - `translator_2_start` → `translator_1_end` (direct on DDR_2)
  - `translator_1_end` → `translator_1_start` (via translator_1)
  - `translator_1_start` → `bravo_96` (direct on DDR_1)

- **sample_plate** from `translator_2_end` (DDR_3) to `bravo_96` (DDR_1):
  - `translator_2_end` → `translator_2_start` (via translator_2)
  - `translator_2_start` → `translator_1_end` (direct on DDR_2)
  - `translator_1_end` → `translator_1_start` (via translator_1)
  - `translator_1_start` → `bravo_96` (direct on DDR_1)

Both need to pass through `translator_1_end` → `translator_1_start`.

## Key Findings

### 🔴 Critical: Deadlock NOT Being Detected
- **NO "Deadlock detected" messages in logs**
- Threads successfully getting parking pad reservations granted
- Moving to parking pads, then repeating
- `reservation.deadlocked` event never set

### Why Deadlock Detection Fails
1. **Deadlock detection only runs on REJECTED requests** (line 96-100 in reservation_manager.py)
2. **Both threads finding empty alternative paths** (parking pads are empty)
3. **Reservations get GRANTED** (not rejected)
4. **No rejected queue → no deadlock detection**

### The Parking Pad Problem
From logs, threads get MULTIPLE reservations granted simultaneously:
```
Thread tips_96 - Reservation ... granted for translator_1_end
Thread tips_96 - Reservation ... granted for pad_4
Thread tips_96 - Reservation ... granted for pad_5
Thread tips_96 - Reservation ... granted for pad_6
Releasing reservation ... for pad_4
Releasing reservation ... for pad_5
Releasing reservation ... for pad_6
```

**Issue:** `resolve_final_reservation()` picks `granted_reservations[0]` - the first in list order
- List order depends on NetworkX path ordering
- Can pick parking pad over actual target
- Can pick backwards path over forward path

### Graph Bidirectionality Issue
- `SystemMap.add_transporter()` adds edges in BOTH directions (line 215-216 in system_map.py)
- NetworkX `all_shortest_paths` can return backwards paths
- No logic to prevent backtracking
- Thread can move A→B→A→B→... indefinitely

### Move Handler Retry Logic
Line 137-141 in move_handler.py:
```python
if reservation_request_collection.rejected.is_set():
    await asyncio.sleep(0.2)
    orca_logger.info("Reservation request collection was rejected, retrying")
    reservation_request_collection.clear()
    return await self._resolve_reservation_from_move_action_collection(thread_id, potential_moves)
```
**When ALL paths rejected:** Retries with same paths indefinitely (if no deadlock detected)

## Root Cause Analysis

### The Core Problem Chain
1. **System finds too many alternatives** → parking pads prevent rejection
2. **No rejection** → no deadlock detection trigger
3. **No deadlock detection** → no rerouting logic
4. **Poor path selection** → picks parking pads or backwards paths
5. **Bidirectional graph** → allows backtracking
6. **No backtrack prevention** → infinite loops

### Why This Wasn't Caught Before
- Small test cases with limited parking pads
- Simple linear workflows without competing threads
- Tests may not exercise complex routing scenarios

## Potential Solution Approaches

### Option 1: Improve Path Selection (Tactical)
- Prioritize actual target over parking pads
- Prefer forward progress over lateral moves
- Track previous location, filter out backtracking
- **Risk:** Piecemeal fix, may not address root cause

### Option 2: Fix Deadlock Detection (Strategic)
- Detect deadlock when ALL paths blocked (not just when rejected)
- Consider "effective blocking" (parking pads that don't help)
- Run detection even when some paths granted
- **Risk:** May over-trigger deadlock handling

### Option 3: Smarter Parking Strategy (Architectural)
- Only route to parking pads that make progress toward goal
- Use distance/reachability heuristics
- Parking pad should be "closer" to goal than current location
- **Risk:** Increased complexity, performance impact

### Option 4: Backtracking Prevention (Foundational)
- Track thread movement history
- Filter paths that revisit recent locations
- Implement "tabu" list for recently visited nodes
- **Risk:** May eliminate valid paths in some scenarios

### Option 5: Hybrid Approach
Combine multiple fixes:
1. Prevent immediate backtracking (track last N locations)
2. Prioritize actual target in path selection
3. Improve deadlock detection to catch "livelock" scenarios
4. Add timeout/escape mechanism for infinite loops

## Critical Questions for Design Decision

1. **Should parking pads ever be chosen over a direct path to target?**
   - If yes, under what conditions?
   - How to measure "progress toward goal"?

2. **What constitutes a deadlock vs. a livelock in this system?**
   - Circular wait (deadlock) vs. circular movement (livelock)
   - Should both trigger rerouting?

3. **How to balance waiting vs. rerouting?**
   - When should a thread wait for path to clear?
   - When should it take alternative route?

4. **Is the bidirectional graph necessary?**
   - Do transporters have directional constraints?
   - Should some edges be one-way?

5. **What's the correct priority order for granted paths?**
   - Target location first?
   - Shortest distance first?
   - Forward progress first?

## Recommended Next Steps for Opus

1. **Trace complete execution flow** for the stuck scenario
2. **Identify exact decision points** where wrong path is chosen
3. **Design comprehensive solution** addressing root causes
4. **Consider backward compatibility** with existing workflows
5. **Define test cases** to validate fix
6. **Implement solution** with proper error handling and logging

## Files to Focus On

### Core Reservation System
- `src/orca/system/reservation_manager/reservation_manager.py` (coordinator, tick loop)
- `src/orca/system/reservation_manager/deadlock_manager.py` (detection logic)
- `src/orca/system/reservation_manager/move_handler.py` (path resolution, deadlock handling)

### Execution and Movement
- `src/orca/workflow_models/labware_threads/executing_labware_thread.py` (thread execution, waiting)
- `src/orca/system/system_map.py` (routing, path finding)

### Test Case
- `examples/smc_assay/smc_assay_example.py` (reproduces issue)

## Success Criteria

A successful fix should:
1. ✅ Allow both threads to reach `bravo_96` without infinite loops
2. ✅ Detect true deadlocks when they occur
3. ✅ Prevent livelock (circular movement without progress)
4. ✅ Make forward progress even with limited resources
5. ✅ Work reliably across different workflow configurations
6. ✅ Not break existing functionality

## Notes on Architecture Limitations

The current architecture has fundamental tension:
- **Wants to avoid deadlock** → finds alternatives aggressively
- **Alternatives mask real deadlocks** → detection fails
- **No notion of "progress"** → can't distinguish good vs. bad alternatives
- **Purely reactive** → no planning or global optimization

A more robust solution might require rethinking the reservation model itself, but that's beyond scope for this immediate fix.
