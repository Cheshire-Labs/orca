# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Orca is a laboratory automation scheduler designed for parallel processing of laboratory workflows. It coordinates multiple devices (liquid handlers, centrifuges, sealers, etc.) and manages labware movement across a lab system using transporters (robotic arms). The system is built on asyncio for concurrent execution of multiple labware threads and workflows.

**Key Architecture Concept**: Orca uses a reservation-based system to manage resources and prevent deadlocks. Labware threads reserve locations before moving, and the system dynamically routes labware through devices based on method requirements.

## Development Commands

### Installation
```bash
# Install in development mode
pip install -e .

# Install with dev dependencies
pip install -e ".[dev]"
```

### Testing
```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_graph.py

# Run with verbose output
pytest -v
```

### Type Checking
```bash
mypy src/orca
```

### Code Formatting
```bash
black src/orca
```

### Running Examples
```bash
# Run the SMC assay example (uses simulated drivers)
python examples/smc_assay/smc_assay_example.py

# Run simple Venus example (requires Hamilton Venus installed)
python examples/simple_venus_example/simple_venus_example.py

# Run PyLabRobot example
python examples/pylabrobot_example/pylabrobot_example.py
```

## Core Architecture

### Layer Structure

The codebase is organized into distinct layers:

1. **SDK Layer** (`src/orca/sdk/`): User-facing API for defining workflows
   - Provides templates: `LabwareTemplate`, `MethodTemplate`, `ThreadTemplate`, `WorkflowTemplate`
   - Imports from this layer to build workflows

2. **System Layer** (`src/orca/system/`): Runtime system management
   - `System`: Main orchestrator implementing `ISystem` interface
   - `SystemMap`: Graph-based routing system using NetworkX
   - `ResourceRegistry`: Manages devices and transporters
   - `SdkToSystemBuilder`: Converts SDK templates to runtime instances

3. **Workflow Models** (`src/orca/workflow_models/`): Runtime workflow execution
   - `ExecutingWorkflow`: Running workflow instances
   - `ExecutingLabwareThread`: Manages individual labware lifecycle
   - `ExecutingMethod`: Executes sequences of actions
   - `LocationAction`: Atomic operations at specific locations

4. **Resource Models** (`src/orca/resource_models/`): Device and labware definitions
   - `Device`: Base class for lab equipment
   - `Transporter`: Devices capable of moving labware
   - `LabwareInstance`: Runtime labware with location tracking
   - `ResourcePool`: Collection of interchangeable resources

5. **Reservation Manager** (`src/orca/system/reservation_manager/`): Deadlock prevention
   - `LocationReservationManager`: Handles location reservations
   - `ThreadReservationCoordinator`: Coordinates multi-location reservations
   - `DeadlockDetector`: Detects and resolves circular wait conditions
   - `MoveHandler`: Executes labware movements after reservations granted

6. **Events** (`src/orca/events/`): Event-driven coordination
   - `EventBus`: Pub/sub system for workflow events
   - Event pattern: `{emitter_type}.{emitter_id}.{status}` (e.g., `"METHOD.COMPLETED"`)
   - Used for workflow orchestration and custom scripting

7. **Driver Management** (`src/orca/driver_management/`): Hardware abstraction
   - Driver interfaces for physical equipment
   - Simulation drivers for testing (`sims.py`)
   - Venus driver for Hamilton platforms

### Key Concepts

**Labware Thread Lifecycle** (see [executing_labware_thread.py](src/orca/workflow_models/labware_threads/executing_labware_thread.py)):
1. Thread spawns with labware at start location
2. For each method in thread's method sequence:
   - For each action in method:
     - Resolve next action (may wait for resource pool availability)
     - While not at action location:
       - Request move reservation via `MoveHandler`
       - Wait for `granted`/`rejected`/`deadlocked` flag
       - If deadlocked: find alternate path to deadlock resolution location
       - Execute move action (transporter picks, moves, places)
     - Wait for co-threads to arrive (`all_labware_is_present` event)
     - Execute action at location
     - Release reservation when moving to next action
3. Move to end location
4. Mark thread as COMPLETED

**Status Transitions**: `CREATED` → `AWAITING_MOVE_RESERVATION` → `AWAITING_MOVE_TARGET_AVAILABILITY` → `MOVING` → `AWAITING_CO_THREADS` → `EXECUTING_ACTION` → (loop) → `COMPLETED`

**Reservation System**:
- Prevents deadlocks using wait-for graph analysis with NetworkX cycle detection
- Threads request reservations before movement via `MoveHandler`
- `ThreadReservationCoordinator` runs background tick loop (every 0.3s) to process reservation batches
- Reservations have event flags: `granted`, `rejected`, `deadlocked`, `processed`
- Starvation prevention: threads denied repeatedly get higher priority
- When deadlocked, thread finds alternate path to deadlock resolution location (PlatePad with `supports_deadlock_resolution=True`)
- See [reservation_manager/deadlock_manager.py](src/orca/system/reservation_manager/deadlock_manager.py) for detection logic

**Dynamic Resource Resolution**:
- Actions can target `ResourcePool` instead of specific devices
- System resolves to available resource at runtime
- Enables load balancing across equivalent devices (e.g., multiple shakers)

**System Map and Routing**:
- Built from transporter teachpoints (locations they can reach)
- Uses NetworkX to find paths between locations
- Automatically determines which transporters to use for movement

## Important Implementation Details

### Async Execution Model
- All workflow execution is asynchronous using `asyncio`
- Entry threads run concurrently via `asyncio.gather()` in `ExecutingWorkflow.start()`
- Each thread is a separate coroutine in the main event loop
- Background tick loop (`ThreadReservationCoordinator.start_tick_loop()`) processes reservation batches every 0.3s
- Device drivers must implement async methods
- Each device/transporter has `asyncio.Lock()` to prevent concurrent hardware access
- Threads wait for reservations using `asyncio.Event`: `await reservation.granted.wait()`
- Use `asyncio.gather()` to run multiple workflows/methods in parallel
- Never block the event loop with synchronous I/O

### Event Bus for Workflow Control
- Workflows use event handlers to coordinate thread spawning
- Subscribe to events like `"METHOD.IN_PROGRESS"` to trigger spawns
- Event handlers can be functions or `SystemBoundEventHandler` subclasses
- System-bound handlers have access to `self.system` API

### Location Reservation Flow
```python
# MoveHandler generates potential paths and creates MoveActions
move_actions = move_handler.resolve_move_action(...)

# Creates collection request with all potential moves
request = MoveActionCollectionReservationRequest(move_actions)

# Submits to coordinator
coordinator.submit_reservation_request(request)

# Wait for any reservation to be granted (first wins, others released)
await request.wait_for_resolution()

# If deadlocked, find alternate path to deadlock resolution location
if request.is_deadlocked():
    alternate_path = move_handler.handle_deadlock(...)
    # Retry with alternate path
```

### SDK to System Translation
- `SdkToSystemBuilder` converts templates to runtime instances
- Templates are immutable definitions (e.g., `MethodTemplate`, `ThreadTemplate`)
- Two-phase conversion: Template → Instance (e.g., `MethodInstance`) → Executing (e.g., `ExecutingMethod`)
- Runtime instances track state (status, current location, etc.)
- Factories create instances from templates (`MethodFactory`, `ThreadFactory`, `WorkflowFactory`)
- Builder wires all dependencies: registries, factories, reservation system, event bus
- Event bus gets bound to system so handlers can access `self.system` API

### Testing Strategy
- Use simulation drivers (`SimTransporterDriver`, `SimDeviceDriver`, etc.)
- Mock teachpoints defined in XML files
- See `tests/conftest.py` for test fixtures
- Examples serve as integration tests

## Common Development Patterns

### Adding a New Device Type
1. Create device class in `src/orca/devices/` extending `Device`
2. Create driver interface in `src/orca/driver_management/drivers/`
3. Implement simulation driver in `sims.py` for testing
4. Add to SDK exports in `src/orca/sdk/devices.py`
5. Document in README device list

### Adding a New Action Type
1. Create action in `src/orca/workflow_models/actions/`
2. Extend `LocationAction` base class
3. Implement `execute()` method with device interaction
4. Add to SDK exports in `src/orca/sdk/actions.py`
5. Document action parameters in README

### Implementing Custom Event Handlers
```python
# Function-based handler
def my_handler(event: str, context: ExecutionContext) -> None:
    if event == "METHOD.COMPLETED":
        print(f"Method {context.method_name} completed")

event_bus.subscribe("METHOD.COMPLETED", my_handler)

# Class-based handler with system access
class MyHandler(SystemBoundEventHandler):
    def handle(self, event: str, context: ExecutionContext) -> None:
        workflow = self.system.get_executing_workflow(context.workflow_id)
        # Custom logic with system access
```

### Working with PyLabRobot Integration
- Labware definitions use PyLabRobot's labware standard
- Generic devices (Sealer, Shaker) accept PyLabRobot backends
- Import labware from `pylabrobot.resources.*`
- Uses forked PyLabRobot: `git+https://github.com/Cheshire-Labs/pylabrobot.git`

## File Organization

Key files to understand:

**System Core**:
- [src/orca/system/system.py](src/orca/system/system.py): Central system orchestrator, main API entry point
- [src/orca/system/SdkToSystemBuilder.py](src/orca/system/SdkToSystemBuilder.py): SDK-to-runtime translation, wires all dependencies
- [src/orca/system/system_map.py](src/orca/system/system_map.py): Graph-based routing with NetworkX, pathfinding algorithms
- [src/orca/system/resource_registry.py](src/orca/system/resource_registry.py): Registry for devices, transporters, resource pools

**Reservation System** (critical for understanding deadlock prevention):
- [src/orca/system/reservation_manager/reservation_manager.py](src/orca/system/reservation_manager/reservation_manager.py): `LocationReservationManager` (single location) and `ThreadReservationCoordinator` (multi-location with tick loop)
- [src/orca/system/reservation_manager/deadlock_manager.py](src/orca/system/reservation_manager/deadlock_manager.py): Wait-for graph construction, cycle detection
- [src/orca/system/reservation_manager/move_handler.py](src/orca/system/reservation_manager/move_handler.py): Movement orchestration, deadlock resolution path finding
- [src/orca/system/reservation_manager/location_reservation.py](src/orca/system/reservation_manager/location_reservation.py): Reservation token with event flags

**Workflow Execution**:
- [src/orca/workflow_models/workflows/executing_workflow.py](src/orca/workflow_models/workflows/executing_workflow.py): Main workflow execution, spawns entry threads
- [src/orca/workflow_models/labware_threads/executing_labware_thread.py](src/orca/workflow_models/labware_threads/executing_labware_thread.py): Thread execution loop (move → execute → repeat)
- [src/orca/workflow_models/method.py](src/orca/workflow_models/method.py): Method execution, action sequences
- [src/orca/workflow_models/actions/location_action.py](src/orca/workflow_models/actions/location_action.py): Base action class
- [src/orca/workflow_models/actions/move_action.py](src/orca/workflow_models/actions/move_action.py): Movement actions
- [src/orca/workflow_models/actions/dynamic_resource_action.py](src/orca/workflow_models/actions/dynamic_resource_action.py): Resource pool resolution

**Events and Status**:
- [src/orca/events/event_bus.py](src/orca/events/event_bus.py): Pub/sub event system
- [src/orca/workflow_models/status_manager.py](src/orca/workflow_models/status_manager.py): Central status tracking, emits events on status changes
- [src/orca/events/event_handlers.py](src/orca/events/event_handlers.py): Event handler implementations (including `Spawn` for thread spawning)

**Resources**:
- [src/orca/resource_models/devices.py](src/orca/resource_models/devices.py): Base `Device` class
- [src/orca/resource_models/labware.py](src/orca/resource_models/labware.py): Labware template/instance pairs
- [src/orca/resource_models/transporter.py](src/orca/resource_models/transporter.py): Robotic arms with teachpoints
- [src/orca/resource_models/location.py](src/orca/resource_models/location.py): Location abstraction wrapper
- [src/orca/resource_models/resource_pool.py](src/orca/resource_models/resource_pool.py): Resource pooling for load balancing

**Examples**: [examples/](examples/) directory contains complete workflow demonstrations

## Critical Interaction Patterns

### Thread Spawning Pattern
1. Workflow defines spawn point: `workflow.set_spawn_point(spawn_thread, parent_thread, at=parent_method, join=True)`
2. `ExecutingWorkflow` subscribes: `event_bus.subscribe("METHOD.IN_PROGRESS", Spawn(...))`
3. When parent thread's method becomes `IN_PROGRESS`, spawn handler fires
4. Handler calls `system.create_and_register_thread_instance(spawn_template)`
5. New thread started as async task, runs concurrently with parent
6. If `join=True`, `SharedMethodTemplate` replaced with shared method instance for coordination

### Co-thread Coordination Pattern
1. Action specifies multiple input labware (e.g., plate transfer needs source + destination)
2. Thread arrives at action location first
3. `LocationAction.all_labware_is_present` event initially unset
4. Thread waits: `await action.all_labware_is_present.wait()`
5. As other co-threads arrive, location updates its labware list
6. When all expected inputs present, event is set
7. All threads proceed to execute action together

### Dynamic Resource Resolution Pattern
1. `ActionTemplate` specifies `ResourcePool` (e.g., pool of 10 shakers)
2. Thread reaches action requiring resource from pool
3. `DynamicResourceActionResolver.resolve()` finds nearest available resource in pool
4. Returns specific location of that resource
5. Thread moves to that location and executes
6. Enables automatic load balancing across equivalent devices

## Debugging Tips

- Check reservation status: Threads stuck in `AWAITING_MOVE_RESERVATION` indicate reservation bottleneck
- Monitor tick loop: Slow tick loops (>0.5s) indicate processing bottleneck
- Event tracing: Subscribe to `"*"` to see all events for debugging workflow coordination
- Deadlock resolution: Check PlatePad `supports_deadlock_resolution` property if threads getting stuck
- Status manager: All status changes emit events - subscribe to debug lifecycle issues

## Notes

- This is AGPL-licensed with contributor license agreement required
- Currently in development - interfaces may change
- Not tested on live systems - use simulation mode for development
- Supports Windows (Venus integration) and cross-platform (core system)
- Python 3.10+ required
- Uses forked PyLabRobot: `git+https://github.com/Cheshire-Labs/pylabrobot.git`
