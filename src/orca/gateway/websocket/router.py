"""WebSocket router for device-bridge connections.

Provides the /ws/devices endpoint for on-prem device bridges to connect, handles
protocol message exchange, and integrates with the connection tracker.

Authentication is delegated to an injected ``IConnectionAuthenticator`` read off
``app.state.connection_authenticator`` (defaults to allow-all for single-node
operation). Connection state is purely in-memory; orca-core has no database.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Header, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from cheshire_drivers.gateway_protocol import (
    PROTOCOL_VERSION,
    ConnectMessage,
    HeartbeatMessage,
    MessageEnvelope,
    ResponseMessage,
    StatusMessage,
)
from orca.gateway.auth import IConnectionAuthenticator, NullConnectionAuthenticator
from orca.gateway.controller import device_controller
from orca.gateway.registry import device_connection_tracker
from orca.gateway.registry.connection_tracker import DeviceNameConflictError
from orca.gateway.websocket.collision_check import (
    check_collision,
    format_violations_for_close_reason,
)
from orca.gateway.websocket.connection_events import connection_events
from orca.gateway.websocket.manager import connection_manager

logger = logging.getLogger(__name__)

router = APIRouter()


def _authenticator_from_websocket(websocket: WebSocket) -> IConnectionAuthenticator:
    """Resolve the injected authenticator, or allow-all when none is set.

    A standalone daemon leaves this unset and trusts its LAN; a hosting layer
    stores its own check on ``app.state.connection_authenticator``.
    """
    authenticator = getattr(websocket.app.state, "connection_authenticator", None)
    if authenticator is None:
        return NullConnectionAuthenticator()
    return authenticator


def _runtime_from_websocket(websocket: WebSocket):
    """Extract the live runtime from app state, or ``None`` when not built yet.

    The collision check tolerates ``None``: when the runtime isn't ready we
    accept the connection and rely on the runtime's full collision pass at
    deployment-load time to catch any mismatch.
    """
    return getattr(websocket.app.state, "system_runtime", None)


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
        Message types: connect, heartbeat, status, response.

    Flow:
        1. Device bridge connects with optional API key.
        2. Server validates via the injected authenticator.
        3. Device bridge sends ConnectMessage.
        4. Server registers the device bridge and its devices (in-memory).
        5. Message loop: receive -> handle -> repeat (in-memory only).
        6. On disconnect: cleanup.
    """
    authenticator = _authenticator_from_websocket(websocket)
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

        if envelope.type != "connect":
            logger.warning(f"Expected connect message, got {envelope.type}")
            await websocket.close(code=1002, reason="Expected connect message")
            return

        # Negotiate the protocol version from the RAW payload before strict
        # parsing. ConnectMessage types protocol_version as a Literal, so a
        # mismatched version fails model_validate with a generic ValidationError
        # (caught below, socket left open, client hangs) instead of this clean
        # 1002 close. Version negotiation must precede full message validation.
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

        connect_msg = ConnectMessage.model_validate(envelope.payload)
        client_id = f"{connect_msg.site}-{connect_msg.lab}-client"

        # Re-run the contract check on every connect: a late-connecting device
        # bridge whose advertised contract contradicts topology must be refused
        # (e.g. the operator swapped a driver class on-prem). Mismatch closes
        # 1002 with the offending device named in the close reason.
        runtime = _runtime_from_websocket(websocket)
        violations = await check_collision(runtime, connect_msg.devices)
        if violations:
            reason = format_violations_for_close_reason(violations)
            logger.warning(
                "Late-connect contract collision; refusing client %s: %s",
                client_id, reason,
            )
            await websocket.close(code=1002, reason=reason)
            return

        # Claim the device names first. A name another live client already holds
        # means two device bridges wired to one instrument, so the newcomer is
        # refused before it takes the socket and before the incumbent is
        # displaced.
        try:
            await device_connection_tracker.register_client(
                client_id=client_id,
                site=connect_msg.site,
                lab=connect_msg.lab,
                workcell=connect_msg.workcell,
                devices=connect_msg.devices,
            )
        except DeviceNameConflictError as conflict:
            logger.warning(
                "Refusing client %s, device already connected elsewhere: %s",
                client_id, conflict,
            )
            await websocket.close(code=1002, reason=str(conflict)[:120])
            return

        displaced = await connection_manager.connect(
            websocket=websocket,
            client_id=client_id,
            site=connect_msg.site,
            lab=connect_msg.lab,
            workcell=connect_msg.workcell,
        )
        # Fire device.disconnected for the previous connection's devices
        # so subscribers see the displacement before the new attach lands.
        for displaced_device_id in displaced:
            await connection_events.emit_disconnected(displaced_device_id)

        device_names = [d.name for d in connect_msg.devices]
        await connection_manager.attach_devices(client_id, device_names)
        for device in connect_msg.devices:
            await connection_events.emit_connected(device, client_id)

        logger.info(
            f"Client {client_id} registered with {len(connect_msg.devices)} devices"
        )

        while True:
            data = await websocket.receive_text()
            envelope = MessageEnvelope.model_validate_json(data)

            if envelope.type == "heartbeat":
                HeartbeatMessage.model_validate(envelope.payload)
                await connection_manager.update_heartbeat(client_id)
                await device_connection_tracker.update_heartbeat(client_id)
                logger.debug(f"Heartbeat from {client_id}")

            elif envelope.type == "status":
                status_msg = StatusMessage.model_validate(envelope.payload)
                for device_id, reported in status_msg.devices.items():
                    await device_connection_tracker.apply_agent_report(device_id, reported)
                    await connection_events.emit_reported(device_id, reported)
                logger.debug(
                    f"Status update from {client_id}: {len(status_msg.devices)} devices"
                )

            elif envelope.type == "response":
                response_msg = ResponseMessage.model_validate(envelope.payload)
                await device_controller.handle_response(response_msg)
                logger.debug(
                    f"Routed response for command {response_msg.command_id} "
                    f"from {client_id}"
                )

            else:
                logger.warning(f"Unknown message type from {client_id}: {envelope.type}")

    except ValidationError as e:
        # A malformed envelope/connect (or any later message) must close the
        # socket EXPLICITLY. The implicit close-on-return does not reliably
        # propagate the close frame, so the client would hang on its receive
        # (the protocol-version mismatch above is the same hazard, handled
        # inline). Without client_id yet, this is a handshake-phase reject.
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
            disconnected_devices = await connection_manager.disconnect(client_id)
            try:
                await device_connection_tracker.unregister_client(client_id)
            except Exception as e:
                logger.warning(f"Failed to unregister client {client_id}: {e}")
            for device_id in disconnected_devices:
                await connection_events.emit_disconnected(device_id)
