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

**Labware Thread Lifecycle**:
1. Thread spawns with labware at start location
2. Reserves locations for next action
3. Moves to reserved location
4. Executes action at location
5. Releases location when moving away
6. Repeats until reaching end location

**Reservation System**:
- Prevents deadlocks using wait-for graph analysis
- Threads request reservations before movement
- Reservations granted when location is free and no circular dependencies exist
- See `reservation_manager/deadlock_manager.py` for detection logic

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
- Device drivers must implement async methods
- Use `asyncio.gather()` to run multiple workflows/methods in parallel
- Never block the event loop with synchronous I/O

### Event Bus for Workflow Control
- Workflows use event handlers to coordinate thread spawning
- Subscribe to events like `"METHOD.IN_PROGRESS"` to trigger spawns
- Event handlers can be functions or `SystemBoundEventHandler` subclasses
- System-bound handlers have access to `self.system` API

### Location Reservation Flow
```python
# Thread requests reservation
reservation = LocationReservation(labware, thread_id)
coordinator.request_reservation(location_name, reservation)

# Wait for reservation granted/rejected
await reservation.granted.wait()

# Use location, then release
reservation.release()
```

### SDK to System Translation
- `SdkToSystemBuilder` converts templates to runtime instances
- Templates are immutable definitions
- Runtime instances track state (status, current location, etc.)
- Factories create instances from templates

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
- `src/orca/system/system.py`: Central system orchestrator
- `src/orca/system/SdkToSystemBuilder.py`: SDK-to-runtime translation
- `src/orca/system/reservation_manager/reservation_manager.py`: Location reservation logic
- `src/orca/system/reservation_manager/deadlock_manager.py`: Deadlock detection
- `src/orca/workflow_models/labware_threads/executing_labware_thread.py`: Thread execution
- `src/orca/events/event_bus.py`: Event system implementation
- Examples in `examples/` directory demonstrate complete workflows

## Notes

- This is AGPL-licensed with contributor license agreement required
- Currently in development - interfaces may change
- Not tested on live systems - use simulation mode for development
- Supports Windows (Venus integration) and cross-platform (core system)
- Python 3.10+ required
