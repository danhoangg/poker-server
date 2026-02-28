"""
AlgoPoker WebSocket server.

Each connected bot gets its own handle_connection coroutine that:
  1. Waits for a 'join' message and registers with the TournamentManager.
  2. Forwards all subsequent 'action' messages to the player's action_queue.
  3. Sends error messages for malformed or unexpected inputs.
  4. Notifies the TournamentManager when the connection closes.
"""

from __future__ import annotations

import asyncio
import json
import logging

import websockets
from websockets.asyncio.server import ServerConnection, serve

import config
from protocol import (
    MSG_ACTION,
    MSG_JOIN,
    MSG_SPECTATE,
    MSG_START,
    ERR_BAD_JOIN,
    ERR_BAD_NAME,
    ERR_BAD_JSON,
    ERR_UNKNOWN_TYPE,
    build_error,
)
from tournament import TournamentManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("algopoker.server")

# Single tournament instance for the lifetime of the server
tournament = TournamentManager()


async def handle_connection(websocket: ServerConnection) -> None:
    player = None
    remote = websocket.remote_address
    log.info("New connection from %s", remote)

    try:
        # ----------------------------------------------------------------
        # Step 1: Expect a 'join' message within 10 seconds
        # ----------------------------------------------------------------
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
        except asyncio.TimeoutError:
            await _send_error(websocket, ERR_BAD_JOIN, "No join message received within 10 seconds.")
            return
        except websockets.exceptions.ConnectionClosed:
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await _send_error(websocket, ERR_BAD_JOIN, "Expected JSON 'join' message.")
            return

        if not isinstance(msg, dict):
            await _send_error(websocket, ERR_BAD_JOIN, "First message must be a JSON object.")
            return

        msg_type = msg.get("type")

        # ----------------------------------------------------------------
        # Spectator path
        # ----------------------------------------------------------------
        if msg_type == MSG_SPECTATE:
            spectator = await tournament.register_spectator(websocket)
            try:
                async for raw_msg in websocket:
                    try:
                        m = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(m, dict) and m.get("type") == MSG_START:
                        await tournament.force_start()
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                tournament.remove_spectator(spectator)
                log.info("Spectator connection closed: %s", remote)
            return

        # ----------------------------------------------------------------
        # Player path — expect a 'join' message
        # ----------------------------------------------------------------
        if msg_type != MSG_JOIN:
            await _send_error(websocket, ERR_BAD_JOIN, "First message must be {\"type\": \"join\", \"name\": \"...\"}.")
            return

        name = str(msg.get("name", "")).strip()
        if not name or len(name) > 32:
            await _send_error(websocket, ERR_BAD_NAME, "Name must be 1–32 non-whitespace characters.")
            return

        # ----------------------------------------------------------------
        # Step 2: Register with the tournament
        # ----------------------------------------------------------------
        player = await tournament.register_player(websocket, name)
        if player is None:
            # register_player sent its own error; just close cleanly
            return

        log.info("'%s' registered as seat %d.", name, player.seat_index)

        # ----------------------------------------------------------------
        # Step 3: Message pump — forward actions to the tournament
        # ----------------------------------------------------------------
        async for raw_msg in websocket:
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                await player.send(build_error(ERR_BAD_JSON, "Message is not valid JSON."))
                continue

            if not isinstance(msg, dict):
                await player.send(build_error(ERR_BAD_JSON, "Message must be a JSON object."))
                continue

            msg_type = msg.get("type")

            if msg_type == MSG_ACTION:
                # Put into the action queue; discard if it arrives out of turn
                try:
                    player.action_queue.put_nowait(msg)
                except asyncio.QueueFull:
                    pass  # Silently drop: either out-of-turn or duplicate

            else:
                await player.send(build_error(
                    ERR_UNKNOWN_TYPE,
                    f"Unknown message type: {msg_type!r}. Expected 'action'.",
                ))

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as exc:
        log.exception("Unhandled error in connection handler for %s: %s", remote, exc)
    finally:
        if player is not None:
            tournament.handle_disconnect(player)
        log.info("Connection closed: %s", remote)


async def _send_error(websocket: ServerConnection, code: str, message: str) -> None:
    try:
        await websocket.send(json.dumps(build_error(code, message)))
    except websockets.exceptions.ConnectionClosed:
        pass


async def main() -> None:
    log.info("AlgoPoker server starting on ws://%s:%d", config.HOST, config.PORT)
    log.info(
        "Waiting for %d–%d players. Starting in %ds after min reached.",
        config.MIN_PLAYERS, config.MAX_PLAYERS, config.LOBBY_WAIT_SECONDS,
    )

    async with serve(handle_connection, config.HOST, config.PORT) as server:
        log.info("Server ready. Waiting for bots to connect...")
        await server.serve_forever()
