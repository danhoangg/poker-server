"""
Example bot for the AlgoPoker server.

This is a reference implementation showing how to connect to the server
and play a full tournament. It plays randomly — picking a random valid
action each turn.

Usage:
    python example_bot.py --name MyBot
    python example_bot.py --name MyBot --host localhost --port 8765

Run multiple instances in parallel to start a tournament:
    python example_bot.py --name Bot1 &
    python example_bot.py --name Bot2 &
    python example_bot.py --name Bot3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)


class ExampleBot:
    def __init__(self, name: str, host: str = "localhost", port: int = 8765) -> None:
        self.name = name
        self.uri = f"ws://{host}:{port}"
        self.log = logging.getLogger(f"bot.{name}")
        self.my_seat: int | None = None

    async def run(self) -> None:
        self.log.info("Connecting to %s as '%s'", self.uri, self.name)
        async with websockets.connect(self.uri) as ws:
            # Send join message
            await ws.send(json.dumps({"type": "join", "name": self.name}))
            self.log.info("Joined. Waiting for tournament...")

            async for raw in ws:
                msg = json.loads(raw)
                await self._handle_message(ws, msg)

        self.log.info("Connection closed.")

    async def _handle_message(self, ws, msg: dict) -> None:
        t = msg.get("type")

        if t == "waiting":
            self.log.info(
                "Lobby: %d/%d players",
                msg["current_players"], msg["max_players"],
            )

        elif t == "game_start":
            # Learn our permanent seat from the player list
            for i, name in enumerate(msg["player_names"]):
                if name == self.name:
                    self.my_seat = i
            self.log.info(
                "Tournament starting! I am seat %d. Players: %s",
                self.my_seat, msg["player_names"],
            )

        elif t == "hand_start":
            self.log.info(
                "Hand #%d | Dealer seat %d | Blinds %d/%d",
                msg["hand_number"],
                msg["dealer_seat"],
                msg["small_blind_amount"],
                msg["big_blind_amount"],
            )

        elif t == "action_request":
            actor_seat = msg["actor_seat"]
            gs = msg["game_state"]

            if actor_seat != self.my_seat:
                # Not our turn; just observe
                return

            action = self._choose_action(gs)
            self.log.info(
                "Hand #%d | %s | Acting: %s",
                gs["hand_number"],
                gs["street"],
                action,
            )
            await ws.send(json.dumps({"type": "action", "action": action}))

        elif t == "action_result":
            gs = msg["game_state"]
            self.log.info(
                "  %s (seat %d) → %s%s%s",
                msg["player_name"],
                msg["actor_seat"],
                msg["action"]["type"],
                f" {msg['action']['amount']}" if msg["action"]["amount"] is not None else "",
                " [TIMEOUT]" if msg.get("timed_out") else "",
            )

        elif t == "hand_end":
            for w in msg["winners"]:
                self.log.info(
                    "Hand #%d result: %s (seat %d) won %d chips",
                    msg["hand_number"], w["name"], w["seat"], w["amount_won"],
                )
            if msg["eliminated_seats"]:
                self.log.info("Eliminated seats: %s", msg["eliminated_seats"])

        elif t == "game_end":
            self.log.info(
                "TOURNAMENT OVER after %d hands. Winner: %s (seat %d)",
                msg["total_hands"], msg["winner"], msg["winner_seat"],
            )

        elif t == "error":
            self.log.error("Server error [%s]: %s", msg.get("code"), msg.get("message"))

    def _choose_action(self, game_state: dict) -> dict:
        """
        Pick a random valid action. Raises the minimum raise amount.

        Replace this method with your bot's actual strategy.
        """
        valid = game_state.get("valid_actions", [])
        if not valid:
            return {"type": "fold"}

        choice = random.choice(valid)
        if choice["type"] == "raise":
            return {"type": "raise", "amount": choice["min_amount"]}
        return {"type": choice["type"]}


def main() -> None:
    parser = argparse.ArgumentParser(description="AlgoPoker example bot")
    parser.add_argument("--name", default="RandomBot", help="Bot name (must be unique)")
    parser.add_argument("--host", default="localhost", help="Server host")
    parser.add_argument("--port", type=int, default=8765, help="Server port")
    args = parser.parse_args()

    bot = ExampleBot(name=args.name, host=args.host, port=args.port)
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
