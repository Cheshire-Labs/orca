# Reservation System Hybrid Refactor & Fix Roadmap

**Status:** Planning Phase
**Approach:** Hybrid (Critical Fixes → Asyncio Refactor → Cleanup)
**Timeline:** 4-5 weeks
**Testing Strategy:** Strategic test placement to minimize maintenance burden

---

## Executive Summary

The reservation system has both **critical bugs** and **fundamental architectural issues** (polling-based instead of event-driven asyncio). This roadmap uses a hybrid approach:

1. **Phase 1 (Week 1):** Fix critical bugs that block functionality
2. **Phase 2 (Weeks 2-4):** Refactor to proper event-driven asyncio architecture
3. **Phase 3 (Week 5):** Integration testing, performance validation, cleanup

**Test Strategy:** Write ~35 critical tests strategically timed to provide safety net during refactor without excessive maintenance burden.

---

## Critical Bugs Identified

| # | Severity | Type | File | Lines |
|---|----------|------|------|-------|
| 1 | CRITICAL | TOCTOU Race Condition | reservation_manager.py | 28-35, 43-46 |
| 2 | CRITICAL | Unbounded Recursion | move_handler.py | 185-199 |
| 3 | HIGH | Missing Null Checks | deadlock_manager.py | 150 |
| 4 | MEDIUM | Inconsistent Event State | move_handler.py | 78-87 |
| 5 | MEDIUM | Known TODO | move_handler.py | 211-217 |
| 6 | MINOR | Typo | reservation_manager.py | 44 |
| 7 | CRITICAL | Multiple Tick Loops | executing_workflow.py | 69 |

---

## Architectural Issues

| Issue | Current | Should Be | Impact |
|-------|---------|-----------|--------|
| Tick-based polling | `while True: await sleep(0.3); process()` | `asyncio.Queue` event-driven | 0-300ms latency per reservation |
| Location polling | `while location.labware: await sleep(0.2)` | `asyncio.Condition` with notify | 0-200ms latency per move |
| Manual queue | `List + asyncio.Lock` | `asyncio.PriorityQueue` | Complex synchronization |
| No async primitives | Zero use of Queue/Condition/Semaphore | Use built-in primitives | Reinventing wheels |
| Recursive retry | Unbounded recursion | Iterative with backoff | Stack overflow risk |

---

# PHASE 1: Critical Bug Fixes (Week 1)

**Goal:** Get system minimally functional, fix showstoppers
**Timeline:** 5 days
**Tests Written:** 8 regression tests (for bugs we fix)

## Day 1-2: Critical Bug Fixes

### Step 1.1: Fix Multiple Tick Loops (Bug #7)
**File:** `src/orca/workflow_models/workflows/executing_workflow.py`
**Lines:** ~69

**Current:**
```python
async def start(self) -> None:
    asyncio.create_task(self._thread_reservation_coordinator.start_tick_loop(0.3))
    # ... rest
```

**Fix:**
```python
async def start(self) -> None:
    # Only start tick loop if not already started
    if not self._thread_reservation_coordinator.ticker_started:
        asyncio.create_task(self._thread_reservation_coordinator.start_tick_loop(0.3))
    # ... rest
```

**Why now:** Prevents race conditions from multiple tick loops competing

**Test to write:** `tests/test_reservation_regression.py::test_single_tick_loop_per_system`

---

### Step 1.2: Add Recursion Depth Limit (Bug #2 - Partial Fix)
**File:** `src/orca/system/reservation_manager/move_handler.py`
**Lines:** 185-199

**Current:**
```python
async def _resolve_reservation_from_move_action_collection(self, thread_id: str, potential_moves: List[MoveAction]) -> MoveAction:
    # ... request processing ...
    if reservation_request_collection.rejected.is_set():
        await asyncio.sleep(0.2)
        return await self._resolve_reservation_from_move_action_collection(thread_id, potential_moves)  # RECURSION
```

**Fix (Temporary - full fix in Phase 2):**
```python
async def _resolve_reservation_from_move_action_collection(
    self,
    thread_id: str,
    potential_moves: List[MoveAction],
    max_retries: int = 100,  # Generous limit to prevent stack overflow
    retry_count: int = 0
) -> MoveAction:
    if retry_count >= max_retries:
        raise RuntimeError(f"Thread {thread_id} exceeded maximum reservation retries ({max_retries})")

    # ... existing request processing ...

    if reservation_request_collection.rejected.is_set():
        await asyncio.sleep(0.2)
        orca_logger.debug(f"Thread {thread_id} - Retry {retry_count + 1}/{max_retries}")
        reservation_request_collection.clear()
        return await self._resolve_reservation_from_move_action_collection(
            thread_id, potential_moves, max_retries, retry_count + 1
        )
    # ... rest unchanged
```

**Why now:** Prevents stack overflow crashes
**Why temporary:** Phase 2 will replace recursion with iteration entirely

**Test to write:** `tests/test_reservation_regression.py::test_recursion_depth_limited`

---

### Step 1.3: Add Null Checks in Deadlock Detection (Bug #3)
**File:** `src/orca/system/reservation_manager/deadlock_manager.py`
**Lines:** 150

**Current:**
```python
def _get_labware_to_thread_map(self, queue: List[IReservationCollection]) -> Dict[str, str]:
    return {self._thread_registry.get_thread(c.thread_id).labware.id: c.thread_id for c in queue}
```

**Fix:**
```python
def _get_labware_to_thread_map(self, queue: List[IReservationCollection]) -> Dict[str, str]:
    labware_to_thread = {}
    for collection in queue:
        thread = self._thread_registry.get_thread(collection.thread_id)
        if thread is None:
            orca_logger.warning(f"Thread {collection.thread_id} not found in registry during deadlock detection")
            continue
        if thread.labware is None:
            orca_logger.warning(f"Thread {collection.thread_id} has no labware during deadlock detection")
            continue
        labware_id = thread.labware.id
        if labware_id in labware_to_thread:
            orca_logger.warning(f"Duplicate labware ID {labware_id} for threads {labware_to_thread[labware_id]} and {collection.thread_id}")
        labware_to_thread[labware_id] = collection.thread_id
    return labware_to_thread
```

**Why now:** Prevents AttributeError crashes

**Test to write:** `tests/test_reservation_regression.py::test_deadlock_detection_handles_missing_thread`

---

### Step 1.4: Fix Typo (Bug #6)
**File:** `src/orca/system/reservation_manager/reservation_manager.py`
**Line:** 44

**Current:**
```python
loation_is_unreserved = location_name not in self._reservations.keys()
```

**Fix:**
```python
location_is_unreserved = location_name not in self._reservations.keys()
```

**Why now:** Easy fix, improves code quality

**No test needed:** Typo fix only

---

## Day 3-4: Write Regression Tests for Phase 1 Fixes

### Tests to Write (8 tests in `tests/test_reservation_regression.py`)

1. **test_single_tick_loop_per_system**
   - Start 2 workflows with same coordinator
   - Verify only one tick loop running
   - Check ticker_started flag

2. **test_recursion_depth_limited**
   - Mock always-rejected reservation
   - Trigger retry loop
   - Verify RuntimeError after max_retries (not RecursionError)

3. **test_deadlock_detection_handles_missing_thread**
   - Create queue with collection referencing non-existent thread
   - Call _get_labware_to_thread_map
   - Verify returns empty dict, logs warning, doesn't crash

4. **test_deadlock_detection_handles_none_labware**
   - Thread in registry but labware is None
   - Call _get_labware_to_thread_map
   - Verify skips that thread, doesn't crash

5. **test_location_reservation_manager_can_reserve_checks_both_conditions**
   - Test that can_reserve returns False if location occupied
   - Test that can_reserve returns False if location already reserved
   - Test that can_reserve returns True only if both free

6. **test_starvation_score_increments_correctly**
   - Thread gets rejected
   - Verify starvation score increments
   - Thread gets granted
   - Verify starvation score resets to 0

7. **test_clear_granted_reservation_raises_error**
   - Create and grant a reservation collection
   - Call clear()
   - Verify ValueError raised

8. **test_multiple_paths_first_granted_wins**
   - Create collection with 3 move actions
   - Grant first one
   - Call resolve_final_reservation
   - Verify first action is reserved_move_action

**Test Infrastructure Needed:**
```python
# In tests/conftest.py

@pytest.fixture
def mock_location():
    """Mock location with configurable labware state"""
    location = Mock()
    location.labware = None
    location.name = "test_location"
    location.teachpoint_name = "test_tp"
    return location

@pytest.fixture
def mock_thread_registry():
    """Mock thread registry for testing"""
    registry = Mock()
    return registry

@pytest.fixture
def location_reservation_manager(mock_location_registry):
    """Configured LocationReservationManager"""
    return LocationReservationManager(mock_location_registry)

@pytest.fixture
def thread_coordinator(mock_location_registry, mock_thread_registry):
    """Configured ThreadReservationCoordinator"""
    return ThreadReservationCoordinator(mock_location_registry, mock_thread_registry)
```

---

## Day 5: Test Phase 1 Fixes, Validate System Works

### Validation Steps:
1. Run all 8 regression tests → all pass
2. Run existing tests → all still pass
3. Run examples (smc_assay_example.py) → works without crashes
4. Manual testing: Create workflow with multiple threads competing for locations
5. Document Phase 1 completion

---

# PHASE 2: Asyncio Refactor (Weeks 2-4)

**Goal:** Replace polling architecture with event-driven asyncio
**Timeline:** 15 days
**Tests Written:** 22 unit tests (BEFORE refactor), 5 integration tests (AFTER refactor)

## Week 2: Write Safety Net Tests + Start Refactor

### Day 6-7: Write Unit Tests for Components We'll Refactor

**Strategy:** Write tests for current behavior BEFORE changing code. These tests become our safety net.

### Tests to Write (22 unit tests across 3 files)

#### `tests/test_location_reservation.py` (8 tests)

1. **test_reservation_creation**
   - Create LocationReservation
   - Verify id generated, events not set

2. **test_set_location_and_retrieve**
   - Call set_location()
   - Verify reserved_location returns it

3. **test_reservation_events**
   - Test granted.set() → granted.is_set() == True
   - Test rejected.set() → rejected.is_set() == True
   - Test deadlocked.set() → deadlocked.is_set() == True
   - Test processed.set() → processed.is_set() == True

4. **test_reserved_location_before_set_raises**
   - Access reserved_location before calling set_location
   - Verify ValueError

5. **test_clear_resets_events**
   - Set deadlocked, rejected, processed
   - Call clear()
   - Verify all cleared

6. **test_release_callback_triggers**
   - Mock callback
   - Set callback via set_reservation_release_callback
   - Call release_reservation
   - Verify callback called once

7. **test_release_without_callback_no_error**
   - Don't set callback
   - Call release_reservation
   - Verify no error

8. **test_clear_granted_raises_valueerror**
   - Set granted
   - Call clear()
   - Verify ValueError

#### `tests/test_location_reservation_manager.py` (7 tests)

1. **test_can_reserve_empty_unreserved_location**
   - Mock location that's empty and unreserved
   - Verify can_reserve() returns True

2. **test_cannot_reserve_occupied_location**
   - Mock location with labware
   - Verify can_reserve() returns False

3. **test_cannot_reserve_already_reserved_location**
   - Make first reservation
   - Try second reservation
   - Verify can_reserve() returns False

4. **test_attempt_reservation_success**
   - Location available
   - Call attempt_reservation
   - Verify granted set, processed set

5. **test_attempt_reservation_rejected**
   - Location occupied
   - Call attempt_reservation
   - Verify rejected set, processed set

6. **test_release_reservation_removes_from_dict**
   - Make reservation
   - Call release_reservation
   - Verify removed from _reservations dict

7. **test_get_reservation_at**
   - Make reservation
   - Call get_reservation_at
   - Verify returns the reservation
   - Call for non-existent location
   - Verify returns None

#### `tests/test_deadlock_graph.py` (7 tests)

1. **test_add_edge_creates_nodes**
   - Add edge A → B
   - Verify both nodes in graph

2. **test_is_deadlocked_detects_cycle**
   - Create A → B → C → A
   - Verify is_deadlocked() returns True

3. **test_is_deadlocked_no_cycle**
   - Create A → B → C
   - Verify is_deadlocked() returns False

4. **test_find_cycle_nodes_returns_cycle**
   - Create A → B → C → A
   - Call find_cycle_nodes()
   - Verify returns {A, B, C}

5. **test_find_cycle_nodes_no_cycle_empty**
   - Create A → B → C
   - Call find_cycle_nodes()
   - Verify returns empty set

6. **test_reset_clears_graph**
   - Add edges
   - Call reset()
   - Verify graph empty

7. **test_self_loop_is_deadlock**
   - Add edge A → A
   - Verify is_deadlocked() returns True

---

### Day 8-10: Refactor 1 - Replace List with PriorityQueue

**File:** `src/orca/system/reservation_manager/reservation_manager.py`

**Changes:**

1. Replace `List` with `asyncio.PriorityQueue`
2. Replace `_on_tick()` poll loop with `_process_reservations()` consumer loop
3. Update `submit_reservation_request()` to use `queue.put()`
4. Update `start_tick_loop()` to `start()` that launches consumer

**Before:**
```python
class ThreadReservationCoordinator:
    def __init__(self, location_reg, thread_registry):
        self._queue: List[IReservationCollection] = []
        self._lock = asyncio.Lock()
        self.ticker_started = False

    async def start_tick_loop(self, tick_interval: float = 0.3):
        self.ticker_started = True
        while True:
            await asyncio.sleep(tick_interval)
            await self._on_tick()

    async def _on_tick(self):
        async with self._lock:
            queue_snapshot = list(self._queue)
            self._queue.clear()
        # ... process queue_snapshot

    async def submit_reservation_request(self, thread_id, request):
        async with self._lock:
            self._queue.append(request)
```

**After:**
```python
class ThreadReservationCoordinator:
    def __init__(self, location_reg, thread_registry):
        # Priority queue: lower number = higher priority
        # Priority = -starvation_score (higher starvation = higher priority)
        self._queue: asyncio.PriorityQueue[Tuple[int, IReservationCollection]] = asyncio.PriorityQueue()
        self._processing_task: Optional[asyncio.Task] = None
        self.ticker_started = False  # Keep for backward compatibility during transition

    async def start(self):
        """Start the reservation processing loop (idempotent)"""
        if self._processing_task is not None:
            return  # Already running
        self.ticker_started = True  # Set flag for backward compatibility
        self._processing_task = asyncio.create_task(self._process_reservations())

    async def _process_reservations(self):
        """Consumer loop - processes requests as they arrive"""
        while True:
            # Blocks until item available - no polling!
            priority, collection = await self._queue.get()
            try:
                # Process single collection
                for r in collection.get_reservations():
                    self._reservation_manager.attempt_reservation(r.requested_location.name, r)

                collection.resolve_final_reservation()
                collection.processed.set()

                # Reset starvation if granted
                if collection.granted.is_set():
                    self._starvation_registry.reset_starvation_score(collection.thread_id)

                # Deadlock detection (only if rejected)
                if collection.rejected.is_set():
                    self._detect_dead_lock([collection])
            except Exception as e:
                orca_logger.error(f"Error processing reservation: {e}")
            finally:
                self._queue.task_done()

    async def submit_reservation_request(self, thread_id: str, request: IReservationCollection):
        # Priority = negative starvation score (higher score = lower number = higher priority)
        priority = -self._starvation_registry.get_starvation_score(thread_id)
        await self._queue.put((priority, request))

    async def stop(self):
        """Clean shutdown"""
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
            self._processing_task = None
            self.ticker_started = False
```

**Update workflow startup:**

`src/orca/workflow_models/workflows/executing_workflow.py`:
```python
async def start(self) -> None:
    # Start coordinator once (idempotent)
    await self._thread_reservation_coordinator.start()

    if self.status != WorkflowStatus.CREATED:
        raise RuntimeError(f"Workflow {self._workflow.name} is already started or completed.")

    await asyncio.gather(*[thread.start() for thread in self._entry_threads])
```

**Run tests:** All 22 unit tests + 8 regression tests should still pass

---

### Day 11-12: Refactor 2 - Location Availability with Condition Variables

**Files:**
- `src/orca/resource_models/location.py`
- `src/orca/workflow_models/labware_threads/executing_labware_thread.py`

**Changes:**

1. Add `asyncio.Condition` to `Location` class
2. Notify waiters when labware is picked from location
3. Replace polling loop in `ExecutingLabwareThread` with conditional wait

**Location class changes:**

`src/orca/resource_models/location.py`:
```python
class Location:
    def __init__(self, ...):
        # ... existing code ...
        self._availability_condition = asyncio.Condition()

    async def notify_picked(self, labware: LabwareInstance):
        """Called when labware is picked from this location"""
        # ... existing notification logic ...

        # Notify all threads waiting for this location
        async with self._availability_condition:
            self._labware = None  # Location is now empty
            self._availability_condition.notify_all()

    async def wait_until_available(self, timeout: Optional[float] = None):
        """Wait until this location becomes available (empty and unreserved)"""
        async with self._availability_condition:
            while self._labware is not None:
                if timeout:
                    await asyncio.wait_for(self._availability_condition.wait(), timeout)
                else:
                    await self._availability_condition.wait()
```

**Thread execution changes:**

`src/orca/workflow_models/labware_threads/executing_labware_thread.py`:

**Before (lines ~260-263):**
```python
async def _execute_move_action(self) -> None:
    assert self._move_action is not None
    self.status = LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY
    while self._move_action.target.labware is not None:  # POLLING
        if self._move_action.reservation.deadlocked.is_set():
            await self._handle_deadlock()
        await asyncio.sleep(0.2)  # WASTEFUL
    # ... execute move
```

**After:**
```python
async def _execute_move_action(self) -> None:
    assert self._move_action is not None
    self.status = LabwareThreadStatus.AWAITING_MOVE_TARGET_AVAILABILITY

    # Event-driven wait - instant notification when available
    try:
        await self._move_action.target.wait_until_available(timeout=60.0)
    except asyncio.TimeoutError:
        # After 60s timeout, check for deadlock
        if self._move_action.reservation.deadlocked.is_set():
            await self._handle_deadlock()
            return
        raise RuntimeError(f"Timeout waiting for {self._move_action.target.name} to become available")

    # Check deadlock one more time before proceeding
    if self._move_action.reservation.deadlocked.is_set():
        await self._handle_deadlock()
        return

    # ... execute move
```

**Run tests:** All 22 unit tests + 8 regression tests should still pass

---

## Week 3: Complete Refactor + Start Integration Tests

### Day 13-14: Refactor 3 - Iterative Retry with Exponential Backoff

**File:** `src/orca/system/reservation_manager/move_handler.py`

**Replace recursive retry with iterative pattern:**

**Before (lines 185-199):**
```python
async def _resolve_reservation_from_move_action_collection(
    self, thread_id: str, potential_moves: List[MoveAction],
    max_retries: int = 100, retry_count: int = 0
) -> MoveAction:
    # ... recursive implementation ...
    if reservation_request_collection.rejected.is_set():
        await asyncio.sleep(0.2)
        return await self._resolve_reservation_from_move_action_collection(
            thread_id, potential_moves, max_retries, retry_count + 1
        )
```

**After:**
```python
async def _resolve_reservation_from_move_action_collection(
    self,
    thread_id: str,
    potential_moves: List[MoveAction]
) -> MoveAction:
    """
    Resolve a reservation from a collection of potential move actions.
    Uses iterative retry with exponential backoff.
    """
    backoff = 0.05  # Start with 50ms
    max_backoff = 1.0  # Cap at 1 second
    retry_count = 0
    max_retries = 100  # Safety limit (should never hit with proper event-driven system)

    while True:
        if retry_count >= max_retries:
            raise RuntimeError(
                f"Thread {thread_id} exceeded maximum reservation retries ({max_retries}). "
                f"This likely indicates a deadlock that wasn't detected or a system issue."
            )

        collection = MoveActionCollectionReservationRequest(thread_id, potential_moves)
        await self._thread_reservation_coordinator.submit_reservation_request(thread_id, collection)
        await collection.processed.wait()

        if collection.granted.is_set():
            return collection.reserved_move_action

        elif collection.deadlocked.is_set():
            collection.clear()
            return await self.handle_deadlock(thread_id, potential_moves[0])

        elif collection.rejected.is_set():
            # Exponential backoff
            retry_count += 1
            orca_logger.debug(
                f"Thread {thread_id} - Reservation rejected, retry {retry_count} "
                f"after {backoff:.2f}s backoff"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, max_backoff)
            collection.clear()
            # Loop continues - no recursion!

        else:
            raise ValueError(
                f"Thread {thread_id} - Reservation collection not granted, rejected, or deadlocked. "
                f"This should never happen."
            )
```

**Benefits:**
- No stack growth
- Adaptive backoff reduces contention
- Clear loop structure
- Better logging

**Run tests:** All tests should still pass

---

### Day 15-17: Write Integration Tests (5 tests)

**File:** `tests/test_reservation_integration.py`

Now that refactor is complete, write tests to validate the new architecture works end-to-end.

1. **test_full_reservation_cycle_event_driven**
   - Create coordinator with real LocationReservationManager
   - Start processing loop
   - Submit reservation request
   - Await processed event
   - Verify granted (location available) or rejected (occupied)
   - Verify instant processing (no 300ms delay)
   - Stop processing loop

2. **test_multiple_threads_same_location_priority_queue**
   - 3 threads request same location
   - Thread 1: starvation score 0
   - Thread 2: starvation score 5
   - Thread 3: starvation score 2
   - Verify Thread 2 gets priority (processed first)
   - Verify only one granted, others rejected

3. **test_location_availability_condition_instant_notification**
   - Thread waits for location to become available
   - Measure time from pick to thread proceeding
   - Verify < 10ms (instant, not 200ms polling delay)

4. **test_deadlock_detection_and_resolution_full_cycle**
   - Set up circular dependency: Thread A → Location B, Thread B → Location A
   - Both threads submit requests
   - Verify deadlock detected
   - Verify one thread yields to parking pad
   - Verify other thread proceeds
   - Verify yielded thread eventually succeeds

5. **test_starvation_prevention_across_ticks**
   - Thread A repeatedly rejected (10 times)
   - Thread B also wants same location
   - Verify Thread A eventually gets priority
   - Verify Thread A starvation score increments
   - Verify Thread A eventually granted

**Test Infrastructure:**
```python
@pytest.fixture
async def running_coordinator(thread_coordinator):
    """Coordinator with running processing loop (auto-cleanup)"""
    await thread_coordinator.start()
    yield thread_coordinator
    await thread_coordinator.stop()

async def measure_latency(operation):
    """Measure time for async operation"""
    start = time.time()
    await operation()
    return time.time() - start
```

---

## Week 4: Final Refactor Touches + Documentation

### Day 18-19: Fix Inconsistent Event State (Bug #4)

**File:** `src/orca/system/reservation_manager/move_handler.py`
**Lines:** 78-87

**Current:**
```python
def clear(self) -> None:
    if self.granted.is_set():
        raise ValueError("Cannot clear a reservation that has been granted")
    for action in self._requested_move_actions:
        action.reservation.clear()
    self._processed.clear()
    self._rejected.clear()
    self._deadlocked.clear()
    # BUG: _granted NOT cleared
```

**Fix:**
```python
def clear(self) -> None:
    """Clears the reservation collection, resetting all events and states."""
    if self.granted.is_set():
        raise ValueError("Cannot clear a reservation that has been granted")
    for action in self._requested_move_actions:
        action.reservation.clear()
    self._processed.clear()
    self._rejected.clear()
    self._deadlocked.clear()
    self._granted.clear()  # FIX: Clear for consistency
```

**Rationale:** Even though guard clause prevents clearing granted collections, consistency is better. Future changes might relax this.

---

### Day 20: Address Known TODO (Bug #5)

**File:** `src/orca/system/reservation_manager/move_handler.py`
**Lines:** 211-217

**Current:**
```python
def _assign_reservation_to_moves(self, potential_moves: List[MoveAction], assigned_action: ILocationAction) -> None:
    # TODO: Fix this later - this is a temporary fix
    for move in potential_moves:
        if move.target == assigned_action.location:
            move.set_reservation(assigned_action.reservation)
            move.set_release_reservation_on_place(False)
```

**Improved:**
```python
def _assign_reservation_to_moves(self, potential_moves: List[MoveAction], assigned_action: ILocationAction) -> None:
    """
    Assigns an existing reservation from an action to matching move actions.

    This is used when an action has already obtained a reservation for its location,
    and we're creating move actions to reach that location. We reuse the existing
    reservation rather than requesting a new one.

    Args:
        potential_moves: List of move actions we're considering
        assigned_action: Action that already has a reservation for its location
    """
    matched = False
    for move in potential_moves:
        if move.target == assigned_action.location:
            move.set_reservation(assigned_action.reservation)
            move.set_release_reservation_on_place(False)  # Action owns the reservation
            matched = True
            orca_logger.debug(
                f"Reusing reservation {assigned_action.reservation.id} "
                f"for move to {move.target.name}"
            )

    if not matched:
        orca_logger.warning(
            f"Could not match assigned action location {assigned_action.location.name} "
            f"to any potential move targets: {[m.target.name for m in potential_moves]}"
        )
```

**Changes:**
- Better documentation
- Logging for debugging
- Warning if no match found (helps catch logic errors)

---

# PHASE 3: Integration & Cleanup (Week 5)

**Goal:** Validate system works, write remaining tests, performance check
**Timeline:** 5 days
**Tests Written:** 5 concurrency tests

## Day 21-22: Write Concurrency Tests

**File:** `tests/test_reservation_concurrency.py`

These tests validate the refactored system handles concurrent operations correctly.

1. **test_concurrent_reservation_submissions**
   - Spawn 20 threads all submitting reservations simultaneously
   - Verify all processed
   - Verify no queue corruption
   - Verify no lost requests

2. **test_toctou_race_fixed**
   - Two threads check can_reserve() simultaneously
   - Both see location available
   - Both submit reservation requests
   - Verify only one granted (TOCTOU bug fixed)

3. **test_location_condition_multiple_waiters**
   - 5 threads waiting for same location
   - Location becomes available
   - Verify all threads notified instantly
   - Verify all wake up (no lost notifications)

4. **test_starvation_registry_thread_safe**
   - 10 threads incrementing starvation scores concurrently
   - Verify no race conditions
   - Verify counts are accurate

5. **test_deadlock_detection_under_load**
   - Create 10 threads with potential circular dependencies
   - Submit all requests simultaneously
   - Verify deadlock detected and resolved
   - Verify system doesn't hang

---

## Day 23: Performance Validation

### Performance Test Suite

**File:** `tests/test_reservation_performance.py`

1. **test_reservation_latency_improved**
   - Measure time from submission to processing
   - Verify < 50ms (vs 0-300ms with tick loop)

2. **test_location_notification_latency**
   - Measure time from pick to thread wake
   - Verify < 10ms (vs 0-200ms with polling)

3. **test_throughput_under_load**
   - 100 threads, 50 locations, 1000 reservations
   - Measure total time
   - Verify acceptable throughput

4. **test_cpu_usage_reduced**
   - Monitor CPU usage with idle system
   - Verify low/zero CPU (no polling overhead)

---

## Day 24-25: Documentation & Final Cleanup

### Tasks:

1. **Update CLAUDE.md**
   - Document new event-driven architecture
   - Update reservation system description
   - Add performance characteristics
   - Update troubleshooting section

2. **Add inline documentation**
   - Docstrings for all refactored methods
   - Architecture comments in key files
   - Examples of proper usage

3. **Update README if needed**
   - Note improved performance
   - Update any reservation system documentation

4. **Final test run**
   - All 35 tests pass
   - All existing examples work
   - Manual testing with complex workflows

5. **Create migration notes**
   - Document API changes (if any)
   - Note backward compatibility
   - Performance improvements to expect

---

# Test Summary by Phase

## Total Tests: 35 (down from 95)

| Phase | Test Type | Count | Files |
|-------|-----------|-------|-------|
| Phase 1 | Regression | 8 | test_reservation_regression.py |
| Phase 2 Week 2 | Unit | 22 | test_location_reservation.py (8)<br>test_location_reservation_manager.py (7)<br>test_deadlock_graph.py (7) |
| Phase 2 Week 3 | Integration | 5 | test_reservation_integration.py |
| Phase 3 | Concurrency | 5 | test_reservation_concurrency.py |
| Phase 3 | Performance | (informational, not counted) | test_reservation_performance.py |

**Coverage achieved:** ~85% with strategic test selection

---

# Test Writing Strategy: Why This Timing?

## Phase 1: Write ONLY Regression Tests for Bugs Fixed (8 tests)
**Rationale:**
- Minimal maintenance burden
- Validates fixes work
- Won't need rewriting after refactor
- Quick to write (bugs are well-defined)

## Before Phase 2 Refactor: Write Unit Tests (22 tests)
**Rationale:**
- **Safety net** - ensures refactor doesn't break existing behavior
- Tests written for CURRENT implementation
- Run tests after each refactor step to catch regressions
- Low maintenance - unit tests are stable

## After Phase 2 Refactor: Write Integration Tests (5 tests)
**Rationale:**
- **Validates new architecture** works end-to-end
- Can't write these before refactor (test event-driven behavior)
- Ensures components work together correctly
- High value - catches integration issues

## Phase 3: Write Concurrency Tests (5 tests)
**Rationale:**
- **Validates thread safety** of refactored code
- Hard to write earlier (need event-driven architecture)
- Critical for production readiness
- Catches rare race conditions

---

# Risk Mitigation

## Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Refactor introduces new bugs | Medium | High | Unit tests before refactor, integration tests after |
| Performance regression | Low | High | Performance tests in Phase 3 |
| Breaking existing workflows | Medium | Critical | Run examples after each phase |
| Tests become maintenance burden | Low | Medium | Only 35 strategic tests, not 95 |
| Timeline overruns | Medium | Low | Phases are independent, can pause after Phase 1 or 2 |

---

# Success Criteria

## Phase 1 Success:
- ✅ 8 regression tests pass
- ✅ No stack overflow crashes
- ✅ Single tick loop per system
- ✅ Examples run without errors

## Phase 2 Success:
- ✅ 22 unit tests pass
- ✅ 5 integration tests pass
- ✅ All examples work
- ✅ No polling loops in code
- ✅ Event-driven architecture in place

## Phase 3 Success:
- ✅ All 35 tests pass
- ✅ Performance improved (< 50ms reservation latency)
- ✅ No CPU usage when idle
- ✅ Concurrency tests pass
- ✅ Documentation updated

---

# Next Steps

Once approved, begin with:
1. **Day 1:** Fix Bug #7 (multiple tick loops)
2. **Day 1:** Write first regression test
3. **Day 2:** Fix Bug #2 (recursion depth limit)
4. Continue following roadmap...

**Ready to proceed when you give the signal.**
