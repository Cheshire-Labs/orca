"""WebSocket router for device-bridge connections.

Provides the /ws/devices endpoint for on-prem device bridges to connect, handles
protocol message exchange, and feeds the server's device gateway.

Each server builds the router once at startup with its gateway and its connection
authenticator. Connection state is purely in-memory; orca-core has no database.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Header, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from cheshire_drivers.gateway_protocol import (
    CLIENT_ID_IN_USE_CLOSE_CODE,
    PROTOCOL_VERSION,
    AssignmentResult,
    ClientHello,
    HeartbeatMessage,
    MessageEnvelope,
    ResponseMessage,
    StatusMessage,
)
from orca.gateway.auth import IConnectionAuthenticator
from orca.gateway.device_gateway import ServerDeviceGateway
from orca.gateway.registry.connection_tracker import HEARTBEAT_TOLERANCE_SECONDS
from orca.gateway.websocket.connection_events import take_status_report

logger = logging.getLogger(__name__)


def create_gateway_router(gateway: ServerDeviceGateway, *, authenticator: IConnectionAuthenticator) -> APIRouter:
    """The /ws/devices router over this server's gateway."""
    router = APIRouter()
    tracker = gateway.tracker
    connections = gateway.sockets
    events = gateway.events
    controller = gateway.controller
    assignments = gateway.assignments

    async def apply_assignment_result(hello: ClientHello, result: AssignmentResult) -> None:
        """Record what the client built. A device it no longer runs goes offline; a new or rebuilt one is announced."""
        if not assignments.is_latest(result.assignment_id):
            logger.info("Ignoring client %s's result for an older assignment", hello.client_id)
            return
        applied = result.error is None
        rebuilt = assignments.changed_since_last_build(hello.client_id) if applied else set()
        connection = await connections.get_connection(hello.client_id)
        built = {device.name for device in result.devices}
        attached = [] if connection is None else list(connection.device_ids)
        dropped = [name for name in attached if name not in built]
        for name in await tracker.forget_devices(hello.client_id, dropped):
            await events.emit_disconnected(name)
        for name, error in _build_errors(hello.client_id, result, built).items():
            logger.error("Client %s could not build device %s: %s", hello.client_id, name, error)
            await tracker.record_build_error(hello.client_id, name, error)

        await tracker.register_client(
            client_id=hello.client_id,
            site=hello.site,
            lab=hello.lab,
            workcell=hello.workcell,
            devices=result.devices,
        )
        await connections.attach_devices(hello.client_id, sorted(built))
        for device in result.devices:
            if device.name not in attached or device.name in rebuilt:
                await events.emit_connected(device, hello.client_id)
        assignments.record_build(hello.client_id, result.assignment_id, applied=applied)
        logger.info(f"Client {hello.client_id} runs {len(result.devices)} device(s)")

    def _build_errors(client_id: str, result: AssignmentResult, built: set[str]) -> dict[str, str]:
        """Each device's build error. A failed assignment is the error of every assigned device that did not build."""
        if result.error is None:
            return result.errors
        logger.error("Client %s could not apply its assignment: %s", client_id, result.error)
        assigned = assignments.for_client(client_id)
        failed = [] if assigned is None else [d.name for d in assigned.devices if d.name not in built]
        whole = f"orca-client could not apply its assignment: {result.error}"
        return {**{name: whole for name in failed}, **result.errors}

    @router.websocket("/ws/devices")
    async def websocket_endpoint(
        websocket: WebSocket,
        x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    ):
        """WebSocket endpoint for device-bridge connections.

        Authentication:
            Delegated to the injected IConnectionAuthenticator (X-API-Key header).

        Protocol:
            Expects MessageEnvelope-wrapped messages.
            Message types: hello, assignment_result, heartbeat, status, response.

        Flow:
            1. Device bridge connects with optional API key.
            2. Server validates via the injected authenticator.
            3. Device bridge sends ClientHello; the authenticator decides whether its key may use that client ID.
            4. Server sends the devices it must run (the last successful build's).
            5. Device bridge builds them and answers AssignmentResult.
            6. Message loop: receive -> handle -> repeat (in-memory only).
            7. On disconnect: cleanup.
        """
        # Browsers apply no cross-origin blocking to WebSockets, and no browser client uses this route.
        if "origin" in websocket.headers:
            logger.warning("WebSocket connection rejected: it came from a web page")
            await websocket.close(code=1008, reason="Web pages may not connect to this route")
            return

        try:
            authenticated = await authenticator.authenticate(x_api_key)
        except Exception as e:
            logger.error(f"Error during connection authentication: {e}")
            await websocket.close(code=1008, reason="Authentication error")
            return

        if not authenticated:
            logger.warning("WebSocket connection rejected: authentication failed")
            await websocket.close(code=1008, reason="Authentication failed")
            return

        await websocket.accept()
        logger.info("WebSocket connection accepted")

        client_id: Optional[str] = None

        try:
            data = await websocket.receive_text()
            envelope = MessageEnvelope.model_validate_json(data)

            if envelope.type != "hello":
                logger.warning(f"Expected hello message, got {envelope.type}")
                await websocket.close(code=1002, reason="Expected hello message")
                return

            # Negotiate the protocol version from the RAW payload before strict
            # parsing, so a mismatch closes 1002 instead of a generic ValidationError.
            client_version = envelope.payload.get("protocol_version")
            if client_version != PROTOCOL_VERSION:
                logger.warning(
                    f"Protocol version mismatch: client={client_version}, "
                    f"server={PROTOCOL_VERSION}"
                )
                await websocket.close(
                    code=1002,
                    reason=f"Incompatible protocol version: expected {PROTOCOL_VERSION}",
                )
                return

            hello = ClientHello.model_validate(envelope.payload)

            if not await authenticator.may_claim(x_api_key, hello.client_id):
                logger.warning("Refusing client %s: its key may not use that client ID.", hello.client_id)
                await websocket.close(code=1008, reason="This key may not use that client ID")
                return

            held = await connections.get_connection(hello.client_id)
            if held is not None and not held.closing and not held.is_stale(HEARTBEAT_TOLERANCE_SECONDS):
                logger.warning("Refusing client %s: another connection is still heartbeating.", hello.client_id)
                await websocket.close(
                    code=CLIENT_ID_IN_USE_CLOSE_CODE,
                    reason="Another connection with this client ID is still heartbeating",
                )
                return

            client_id = hello.client_id
            displaced = await connections.connect(
                websocket=websocket,
                client_id=client_id,
                site=hello.site,
                lab=hello.lab,
                workcell=hello.workcell,
            )
            for displaced_device_id in displaced:
                await events.emit_disconnected(displaced_device_id)

            await assignments.send_to(client_id)
            logger.info(f"Client {client_id} connected")

            while True:
                data = await websocket.receive_text()
                envelope = MessageEnvelope.model_validate_json(data)

                if envelope.type == "assignment_result":
                    await apply_assignment_result(hello, AssignmentResult.model_validate(envelope.payload))

                elif envelope.type == "heartbeat":
                    HeartbeatMessage.model_validate(envelope.payload)
                    await connections.update_heartbeat(client_id)
                    await tracker.update_heartbeat(client_id)
                    logger.debug(f"Heartbeat from {client_id}")

                elif envelope.type == "status":
                    status_msg = StatusMessage.model_validate(envelope.payload)
                    await take_status_report(tracker, events, client_id, status_msg.devices)
                    logger.debug(
                        f"Status update from {client_id}: {len(status_msg.devices)} devices"
                    )

                elif envelope.type == "response":
                    response_msg = ResponseMessage.model_validate(envelope.payload)
                    await controller.handle_response(response_msg)
                    logger.debug(
                        f"Routed response for command {response_msg.command_id} "
                        f"from {client_id}"
                    )

                else:
                    logger.warning(f"Unknown message type from {client_id}: {envelope.type}")

        except ValidationError as e:
            # A malformed message must close the socket explicitly: the implicit
            # close-on-return does not reliably send the close frame, so the client would hang.
            logger.warning(f"Malformed message from client {client_id}: {e}")
            await websocket.close(code=1002, reason="Malformed message")
        except WebSocketDisconnect:
            logger.info(f"Client {client_id} disconnected")
        except Exception as e:
            logger.error(
                f"Error in WebSocket handler for {client_id}: {e}", exc_info=True
            )
        finally:
            if client_id:
                disconnected_devices = await connections.disconnect(client_id, websocket)
                # None: a newer connection replaced this socket, and its registration stays.
                if disconnected_devices is not None:
                    try:
                        await tracker.unregister_client(client_id)
                    except Exception as e:
                        logger.warning(f"Failed to unregister client {client_id}: {e}")
                    for device_id in disconnected_devices:
                        await events.emit_disconnected(device_id)

    return router
