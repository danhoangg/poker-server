"""
Interactive human player for the AlgoPoker server.

Displays the game state clearly in the terminal and prompts you to
choose an action when it is your turn. All other events are logged
identically to example_bot.py.

Usage:
    python human_bot.py --name Alice
    python human_bot.py --name Alice --host localhost --port 8765
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import ssl
import sys
import threading

import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(message)s",
)

SUITS = {"c": "♣", "d": "♦", "h": "♥", "s": "♠"}
RANKS = {"T": "10", "J": "J", "Q": "Q", "K": "K", "A": "A"}


def fmt_card(c: str) -> str:
    """'Ah' → 'A♥'  |  '??' → '??'"""
    if c == "??":
        return "??"
    rank = RANKS.get(c[0], c[0])
    suit = SUITS.get(c[1], c[1])
    return f"{rank}{suit}"


def fmt_cards(cards: list[str]) -> str:
    return "  ".join(fmt_card(c) for c in cards) if cards else "(none)"


# ---------------------------------------------------------------------------
# Single stdin reader thread
# ---------------------------------------------------------------------------

class _StdinReader:
    """
    One background thread reads stdin line-by-line and puts each line into
    an asyncio queue.  Having exactly one thread avoids the 'stale thread'
    problem: if a previous prompt timed out, the abandoned input() call would
    compete with the next one for stdin and brick all future prompts.

    Usage:
        reader = _StdinReader(loop)
        line = await reader.readline(timeout=29)  # raises TimeoutError on deadline
        reader.drain()                             # discard buffered lines
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        t = threading.Thread(target=self._worker, daemon=True)
        t.start()

    def _worker(self) -> None:
        while True:
            try:
                line = sys.stdin.readline()
            except Exception:
                line = ""
            # None signals EOF; strip only the trailing newline
            result: str | None = None if not line else line.rstrip("\n")
            asyncio.run_coroutine_threadsafe(self._queue.put(result), self._loop)
            if result is None:
                break

    def drain(self) -> None:
        """Discard any lines buffered while nobody was waiting (e.g. after timeout)."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def readline(self, timeout: float) -> str:
        """
        Wait up to `timeout` seconds for the next line.
        Raises asyncio.TimeoutError on deadline, EOFError on EOF.
        """
        result = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        if result is None:
            raise EOFError
        return result


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class HumanBot:
    def __init__(self, name: str, host: str = "localhost", port: int = 8765) -> None:
        self.name = name
        if host.startswith("ws://") or host.startswith("wss://"):
            self.uri = host
        elif port == 443:
            self.uri = f"wss://{host}"
        else:
            self.uri = f"ws://{host}:{port}"
        self.log = logging.getLogger(f"bot.{name}")
        self.my_seat: int | None = None
        self._stdin: _StdinReader | None = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._stdin = _StdinReader(asyncio.get_running_loop())

        self.log.info("Connecting to %s as '%s'", self.uri, self.name)
        ssl_ctx = None
        if self.uri.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        async with websockets.connect(self.uri, ssl=ssl_ctx) as ws:
            await ws.send(json.dumps({"type": "join", "name": self.name}))
            self.log.info("Joined. Waiting for tournament...")

            async for raw in ws:
                msg = json.loads(raw)
                await self._handle_message(ws, msg)

        self.log.info("Connection closed.")

    # ------------------------------------------------------------------
    # Message handler (mirrors example_bot.py logging exactly)
    # ------------------------------------------------------------------

    async def _handle_message(self, ws, msg: dict) -> None:
        t = msg.get("type")

        if t == "waiting":
            self.log.info(
                "Lobby: %d/%d players",
                msg["current_players"], msg["max_players"],
            )

        elif t == "game_start":
            for i, name in enumerate(msg["player_names"]):
                if name == self.name:
                    self.my_seat = i
            self.log.info(
                "Tournament starting! I am seat %d. Players: %s",
                self.my_seat, msg["player_names"],
            )

        elif t == "hand_start":
            self.log.info(
                "Hand #%d | Dealer seat %d | Blinds %d/%d | Your hand: %s",
                msg["hand_number"],
                msg["dealer_seat"],
                msg["small_blind_amount"],
                msg["big_blind_amount"],
                fmt_cards(msg.get("hole_cards", [])),
            )

        elif t == "action_request":
            actor_seat = msg["actor_seat"]
            gs = msg["game_state"]

            if actor_seat != self.my_seat:
                return  # not our turn; action_result will log it

            self._print_game_state(gs)
            action = await self._prompt_action(gs)
            self.log.info(
                "Hand #%d | %s | Acting: %s",
                gs["hand_number"],
                gs["street"],
                action,
            )
            await ws.send(json.dumps({"type": "action", "action": action}))

        elif t == "action_result":
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

    # ------------------------------------------------------------------
    # Game state display
    # ------------------------------------------------------------------

    def _print_game_state(self, gs: dict) -> None:
        W = 54
        div = "─" * W

        street = gs["street"].upper()
        hand_n = gs["hand_number"]
        pot    = gs["pot"]["total"]
        board  = fmt_cards(gs["community_cards"])

        print(f"\n{div}")
        print(f"  Hand #{hand_n}  |  {street}  |  Pot: {pot}")
        print(f"  Board: {board}")
        print(div)

        for p in gs["players"]:
            tags = []
            if p["is_dealer"]:        tags.append("BTN")
            if p["is_small_blind"]:   tags.append("SB")
            if p["is_big_blind"]:     tags.append("BB")
            if p["seat"] == self.my_seat: tags.append("YOU")
            tag_str = f"[{'/'.join(tags)}]" if tags else ""

            bet_str = f"Bet: {p['current_bet']}" if p["current_bet"] else ""
            allin   = " ALL-IN" if p["is_all_in"] else ""
            folded  = " (folded)" if not p["is_active"] and not p["is_all_in"] else ""

            print(
                f"  Seat {p['seat']}  {p['name']:<12} {tag_str:<12}"
                f"  Stack: {p['stack']:<6}  {bet_str}{allin}{folded}"
            )

        print(div)

        my_cards = next(
            (p["hole_cards"] for p in gs["players"] if p["seat"] == self.my_seat),
            [],
        )
        print(f"  Your hand: {fmt_cards(my_cards)}")
        print(div)

    # ------------------------------------------------------------------
    # Action prompt
    # ------------------------------------------------------------------

    async def _prompt_action(self, gs: dict) -> dict:
        """
        Display the action menu and read one valid input from the single
        stdin-reader thread.  Stale input from a previous timed-out prompt
        is discarded before waiting.  A 29-second deadline fires before the
        server's 30-second auto-fold, giving the user a clear message.
        """
        valid      = gs.get("valid_actions", [])
        valid_types = {a["type"] for a in valid}

        # Discard any lines buffered while we were not waiting (e.g. the user
        # pressed Enter after a previous timeout but before this prompt appeared)
        self._stdin.drain()

        loop     = asyncio.get_running_loop()
        deadline = loop.time() + 29

        # Build and print the menu
        menu: list[str] = []
        for a in valid:
            if a["type"] == "fold":
                menu.append("  f        fold")
            elif a["type"] == "check":
                menu.append("  k        check")
            elif a["type"] == "call":
                menu.append(f"  c        call  {a['amount']}")
            elif a["type"] == "raise":
                menu.append(f"  r <amt>  raise  (min {a['min_amount']}, max {a['max_amount']})")
        menu.append("  (29 seconds to act)")
        print("\n".join(menu))

        async def read_line(prompt: str) -> str:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            print(prompt, end="", flush=True)
            return await self._stdin.readline(timeout=remaining)

        while True:
            try:
                raw = await read_line("\n> ")
            except asyncio.TimeoutError:
                print("\n*** TIME'S UP — auto-folding. ***")
                return {"type": "fold"}
            except EOFError:
                return {"type": "fold"}

            parts = raw.strip().lower().split()
            if not parts:
                continue
            cmd = parts[0]

            if cmd in ("f", "fold") and "fold" in valid_types:
                return {"type": "fold"}

            if cmd in ("k", "check") and "check" in valid_types:
                return {"type": "check"}

            if cmd in ("c", "call") and "call" in valid_types:
                return {"type": "call"}

            if cmd in ("r", "raise") and "raise" in valid_types:
                raise_action = next(a for a in valid if a["type"] == "raise")
                min_r = raise_action["min_amount"]
                max_r = raise_action["max_amount"]

                if len(parts) >= 2:
                    try:
                        amount = int(parts[1])
                    except ValueError:
                        print("  Amount must be a number.")
                        continue
                else:
                    try:
                        amt_raw = await read_line(
                            f"  Raise to (min {min_r}, max {max_r}): "
                        )
                        amount = int(amt_raw.strip())
                    except asyncio.TimeoutError:
                        print("\n*** TIME'S UP — auto-folding. ***")
                        return {"type": "fold"}
                    except (ValueError, EOFError):
                        print("  Invalid amount.")
                        continue

                if amount < min_r:
                    print(f"  Minimum raise is {min_r}.")
                    continue
                if amount > max_r:
                    print(f"  Maximum raise is {max_r}.")
                    continue

                return {"type": "raise", "amount": amount}

            # Unknown input — show hint
            opts = []
            if "fold"  in valid_types: opts.append("f")
            if "check" in valid_types: opts.append("k")
            if "call"  in valid_types: opts.append("c")
            if "raise" in valid_types: opts.append("r <amount>")
            print(f"  Valid inputs: {', '.join(opts)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="AlgoPoker interactive human player")
    parser.add_argument("--name", default="Human", help="Your player name (must be unique)")
    parser.add_argument("--host", default="localhost", help="Server host")
    parser.add_argument("--port", type=int, default=8765, help="Server port")
    args = parser.parse_args()

    bot = HumanBot(name=args.name, host=args.host, port=args.port)
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
