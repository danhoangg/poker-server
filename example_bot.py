"""
AlgoPoker Example Bot
======================

A heavily annotated bot that walks through every step of interacting with
the AlgoPoker server.  Read this file top-to-bottom to understand the full
lifecycle of a tournament — connecting, receiving hands, acting, and finishing.

Usage:
    python example_bot.py --name ExampleBot
    python example_bot.py --name ExampleBot --host localhost --port 8765
"""

import argparse
import asyncio
import json
import ssl
import logging

import websockets

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")


# =============================================================================
# Bot Strategy
# =============================================================================
# Replace this function with your own logic.  It receives the full game state
# and must return one of the valid_actions supplied by the server.
#
# game_state fields you can use:
#   game_state["street"]             – "preflop" / "flop" / "turn" / "river"
#   game_state["community_cards"]    – e.g. ["Jc", "3d", "5c"]
#   game_state["pot"]["total"]       – total chips in the pot
#   game_state["players"]            – list of all seats (see below)
#   game_state["valid_actions"]      – what you are allowed to do right now
#
# Each entry in game_state["players"] looks like:
#   {
#     "seat": 0,
#     "name": "Alice",
#     "stack": 9500,
#     "current_bet": 200,
#     "is_active": True,        # False if this player has folded
#     "is_all_in": False,
#     "is_dealer": False,
#     "is_small_blind": True,
#     "is_big_blind": False,
#     "hole_cards": ["As", "Kd"],   # only revealed for YOUR seat
#     "hole_cards_known": True,     # False for opponents (their cards are "??")
#   }
#
# valid_actions is a list containing some subset of:
#   {"type": "fold"}
#   {"type": "check"}
#   {"type": "call",  "amount": 200}
#   {"type": "raise", "min_amount": 400, "max_amount": 9500}

def decide_action(game_state: dict, my_seat: int) -> dict:
    """
    This function will run everytime it is your go.
    
    Simple example strategy: always call or check, never fold or raise.
    This keeps us in every hand cheaply, which is fine as a demo — your
    real strategy should evaluate hand strength, pot odds, position, etc.
    """
    valid_actions = game_state["valid_actions"]

    # Build a quick lookup by action type
    actions_by_type = {a["type"]: a for a in valid_actions}

    # if want to raise to amount NOT by amount:
    #    return {"type": "raise", "amount": 200}

    # Prefer check (free) over call (costs chips) over fold
    if "check" in actions_by_type:
        return {"type": "check"}

    if "call" in actions_by_type:
        return {"type": "call"}

    # Fold as a last resort (only if check and call are unavailable)
    return {"type": "fold"}


# =============================================================================
# Bot class — handles the WebSocket lifecycle
# =============================================================================

class ExampleBot:
    def __init__(self, name: str, host: str, port: int) -> None:
        self.name = name

        # Build the WebSocket URI.  Port 443 is assumed to be wss://.
        if host.startswith("ws://") or host.startswith("wss://"):
            self.uri = host
        elif port == 443:
            self.uri = f"wss://{host}"
        else:
            self.uri = f"ws://{host}:{port}"

        self.log = logging.getLogger(f"bot.{name}")

        # We learn our permanent seat number from the game_start message.
        # It stays the same for the entire tournament.
        self.my_seat: int | None = None

    async def run(self) -> None:
        """Open the WebSocket connection and play the tournament."""
        self.log.info("Connecting to %s as '%s'", self.uri, self.name)

        # Use SSL for wss:// connections (self-signed certs are accepted here)
        ssl_ctx = None
        if self.uri.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        async with websockets.connect(self.uri, ssl=ssl_ctx) as ws:
            # ----------------------------------------------------------------
            # Step 1 — Join the tournament lobby.
            #
            # The very first thing you must send is a "join" message with your
            # unique player name.  The server will respond with "waiting".
            # ----------------------------------------------------------------
            await ws.send(json.dumps({"type": "join", "name": self.name}))
            self.log.info("Joined.  Waiting for other players...")

            # ----------------------------------------------------------------
            # Step 2 — Listen for messages until the connection closes.
            #
            # The server drives everything from here.  You react to the
            # messages it sends; you only send an "action" message when the
            # server asks you to act.
            # ----------------------------------------------------------------
            async for raw_message in ws:
                message = json.loads(raw_message)
                await self._handle(ws, message)

        self.log.info("Connection closed.")

    # -------------------------------------------------------------------------
    # Message dispatcher
    # -------------------------------------------------------------------------

    async def _handle(self, ws, msg: dict) -> None:
        """Route each incoming message to its handler."""
        msg_type = msg.get("type")

        if msg_type == "waiting":
            self._on_waiting(msg)

        elif msg_type == "game_start":
            self._on_game_start(msg)

        elif msg_type == "hand_start":
            self._on_hand_start(msg)

        elif msg_type == "action_request":
            await self._on_action_request(ws, msg)

        elif msg_type == "action_result":
            self._on_action_result(msg)

        elif msg_type == "hand_end":
            self._on_hand_end(msg)

        elif msg_type == "game_end":
            self._on_game_end(msg)

        elif msg_type == "error":
            self.log.error("Server error [%s]: %s", msg.get("code"), msg.get("message"))

    # -------------------------------------------------------------------------
    # Handlers — one per message type
    # -------------------------------------------------------------------------

    def _on_waiting(self, msg: dict) -> None:
        """
        Sent after every player joins.  Tells you how full the lobby is.
        The tournament starts automatically once enough players have joined.
        """
        self.log.info(
            "Lobby: %d/%d players (need %d to start)",
            msg["current_players"], msg["max_players"], msg["min_players"],
        )

    def _on_game_start(self, msg: dict) -> None:
        """
        Sent once to everyone when the tournament begins.

        This is where you learn your seat number — it never changes.
        msg["player_names"][i] is the player at seat i for the whole tournament.
        """
        for i, name in enumerate(msg["player_names"]):
            if name == self.name:
                self.my_seat = i

        self.log.info(
            "Tournament started!  I am seat %d.  Players: %s  Starting stacks: %s",
            self.my_seat, msg["player_names"], msg["starting_stacks"],
        )

    def _on_hand_start(self, msg: dict) -> None:
        """
        Sent at the beginning of every hand.

        hole_cards contains YOUR two private cards (e.g. ["Ah", "Kd"]).
        Other players receive the same message but with their own cards instead.
        """
        self.log.info(
            "Hand #%d | Dealer seat %d | Blinds %d/%d | My hole cards: %s",
            msg["hand_number"],
            msg["dealer_seat"],
            msg["small_blind_amount"],
            msg["big_blind_amount"],
            msg.get("hole_cards", []),
        )

    async def _on_action_request(self, ws, msg: dict) -> None:
        """
        Sent to ALL active players whenever someone must act.

        Only respond if actor_seat matches your seat.
        Use game_state to decide what to do, then send an "action" message.
        """
        actor_seat = msg["actor_seat"]
        game_state  = msg["game_state"]

        if actor_seat != self.my_seat:
            # Not our turn — we receive this to keep our game model up to date.
            return

        # It's our turn.  Pick an action.
        action = decide_action(game_state, self.my_seat)

        self.log.info(
            "Hand #%d | %s | My turn — acting: %s",
            game_state["hand_number"],
            game_state["street"],
            action,
        )

        # Send the action back to the server.
        # Wrap it in {"type": "action", "action": <your action dict>}.
        await ws.send(json.dumps({"type": "action", "action": action}))

    def _on_action_result(self, msg: dict) -> None:
        """
        Broadcast after every action so everyone can follow the action.

        msg["action"]["amount"] is None for fold/check/call (server fills it in).
        timed_out=True means the player ran out of time and was auto-folded.
        """
        action = msg["action"]
        amount_str = f" {action['amount']}" if action.get("amount") is not None else ""
        timeout_str = " [TIMED OUT]" if msg.get("timed_out") else ""

        self.log.info(
            "  %s (seat %d) → %s%s%s",
            msg["player_name"], msg["actor_seat"],
            action["type"], amount_str, timeout_str,
        )

    def _on_hand_end(self, msg: dict) -> None:
        """
        Sent when a hand finishes (everyone folded to one player, or showdown).

        winners — who won chips and how much (net gain, not total pot).
        hole_cards_revealed — the cards of players who went to showdown.
        eliminated_seats — seats that busted out this hand (stack hit 0).
        """
        for winner in msg["winners"]:
            self.log.info(
                "Hand #%d result: %s (seat %d) won %d chips",
                msg["hand_number"], winner["name"], winner["seat"], winner["amount_won"],
            )

        if msg.get("hole_cards_revealed"):
            revealed = ", ".join(
                f"{p['name']}: {p['hole_cards']}"
                for p in msg["hole_cards_revealed"]
            )
            self.log.info("  Showdown — %s", revealed)

        if msg.get("eliminated_seats"):
            self.log.info("  Eliminated seats: %s", msg["eliminated_seats"])

    def _on_game_end(self, msg: dict) -> None:
        """
        Sent once when only one player remains.  The tournament is over.
        """
        self.log.info(
            "TOURNAMENT OVER after %d hands.  Winner: %s (seat %d)",
            msg["total_hands"], msg["winner"], msg["winner_seat"],
        )


# =============================================================================
# Entry point
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="AlgoPoker example bot")
    parser.add_argument("--name", default="ExampleBot", help="Your unique player name")
    parser.add_argument("--host", default="localhost",   help="Server hostname")
    parser.add_argument("--port", type=int, default=8765, help="Server port")
    args = parser.parse_args()

    bot = ExampleBot(name=args.name, host=args.host, port=args.port)
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
