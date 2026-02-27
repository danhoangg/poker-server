"""
Tournament management: player registration, hand lifecycle, elimination,
dealer rotation, blind scheduling, and action timeout handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import config
from game import PokerHand
from protocol import (
    build_action_request,
    build_action_result,
    build_game_end,
    build_game_start,
    build_hand_end,
    build_hand_start,
    build_waiting,
    build_error,
    ERR_BAD_NAME,
    ERR_TOURNAMENT_FULL,
    ERR_TOURNAMENT_STARTED,
)

log = logging.getLogger("algopoker.tournament")

# Sentinel object placed in action_queue to unblock a waiting get() when
# a bot disconnects mid-hand.
_DISCONNECT_SENTINEL = object()


@dataclass
class PlayerInfo:
    websocket: object        # websockets ServerConnection
    name: str
    seat_index: int
    stack: int
    is_eliminated: bool = False
    # action_queue receives raw action dicts (or _DISCONNECT_SENTINEL)
    action_queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, msg: dict) -> None:
        """Thread-safe serialized send; silently ignores closed connections."""
        import websockets
        try:
            async with self.send_lock:
                await self.websocket.send(json.dumps(msg))
        except (websockets.exceptions.ConnectionClosed, Exception):
            pass


class TournamentManager:
    def __init__(self) -> None:
        self.players: list[PlayerInfo] = []   # all registered players, ordered by seat
        self.started: bool = False
        self.hand_number: int = 0
        self.dealer_seat: int = -1            # permanent seat of current dealer
        self._start_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Player registration (called from server connection handler)
    # ------------------------------------------------------------------

    async def register_player(self, websocket, name: str) -> PlayerInfo | None:
        """
        Register a new player.  Returns the PlayerInfo on success, or None
        if registration was rejected (an error message is sent before returning).
        """
        import websockets

        async def reject(code: str, msg: str) -> None:
            try:
                await websocket.send(json.dumps(build_error(code, msg)))
            except websockets.exceptions.ConnectionClosed:
                pass

        if self.started:
            await reject(ERR_TOURNAMENT_STARTED, "Tournament already in progress.")
            return None

        if len(self.players) >= config.MAX_PLAYERS:
            await reject(ERR_TOURNAMENT_FULL, f"Table is full ({config.MAX_PLAYERS} players).")
            return None

        if any(p.name == name for p in self.players):
            await reject(ERR_BAD_NAME, f"Name '{name}' is already taken.")
            return None

        seat = len(self.players)
        player = PlayerInfo(
            websocket=websocket,
            name=name,
            seat_index=seat,
            stack=config.STARTING_STACK,
        )
        self.players.append(player)
        log.info("Player '%s' joined (seat %d). %d/%d players.",
                 name, seat, len(self.players), config.MAX_PLAYERS)

        # Notify all connected players of the current lobby state
        await self._broadcast(build_waiting(len(self.players), config.MIN_PLAYERS, config.MAX_PLAYERS))

        # Start immediately if full; schedule start if min reached
        if len(self.players) == config.MAX_PLAYERS:
            if self._start_task is None or self._start_task.done():
                self._start_task = asyncio.create_task(self._start_tournament())
        elif len(self.players) == config.MIN_PLAYERS:
            if self._start_task is None or self._start_task.done():
                self._start_task = asyncio.create_task(self._delayed_start())

        return player

    def handle_disconnect(self, player: PlayerInfo) -> None:
        """
        Called when a player's WebSocket connection closes.  Places the
        disconnect sentinel in the action queue so any pending
        get_action_from_bot() returns immediately as a fold.
        """
        try:
            player.action_queue.put_nowait(_DISCONNECT_SENTINEL)
        except asyncio.QueueFull:
            pass
        log.info("Player '%s' disconnected.", player.name)

    # ------------------------------------------------------------------
    # Tournament lifecycle
    # ------------------------------------------------------------------

    async def _delayed_start(self) -> None:
        log.info("Minimum players reached. Starting in %ds...", config.LOBBY_WAIT_SECONDS)
        await asyncio.sleep(config.LOBBY_WAIT_SECONDS)
        if not self.started:
            await self._start_tournament()

    async def _start_tournament(self) -> None:
        if self.started:
            return
        self.started = True
        self.dealer_seat = self.players[0].seat_index

        names = [p.name for p in self.players]
        stacks = [p.stack for p in self.players]
        sb, bb = _get_blinds(0)

        log.info("Tournament starting with %d players.", len(self.players))
        await self._broadcast(build_game_start(names, stacks, sb, bb))

        await self._run_tournament()

    async def _run_tournament(self) -> None:
        while len(self._active_players()) > 1:
            self.hand_number += 1
            await self._run_hand()

        active = self._active_players()
        winner = active[0] if active else None
        log.info("Tournament over. Winner: %s", winner.name if winner else "?")

        names = [p.name for p in self.players]
        stacks = [p.stack for p in self.players]
        await self._broadcast(build_game_end(
            winner_name=winner.name if winner else "?",
            winner_seat=winner.seat_index if winner else -1,
            final_stacks=stacks,
            player_names=names,
            total_hands=self.hand_number,
        ))

    async def _run_hand(self) -> None:
        active = self._active_players()     # sorted by seat_index
        n = len(active)
        if n < 2:
            return

        # Advance dealer to next active player
        self._rotate_dealer(active)

        sb, bb = _get_blinds(self.hand_number)
        dealer_pk = _find_pk(active, self.dealer_seat)
        sb_seat = active[(dealer_pk + 1) % n if n > 2 else dealer_pk].seat_index
        bb_seat = active[((dealer_pk + 2) % n) if n > 2 else (1 - dealer_pk)].seat_index

        log.info(
            "Hand #%d | Players: %d | Dealer: seat %d | Blinds: %d/%d",
            self.hand_number, n, self.dealer_seat, sb, bb,
        )

        player_names = [p.name for p in active]
        stacks = [p.stack for p in active]

        hand_start_msg = build_hand_start(
            hand_number=self.hand_number,
            dealer_seat=self.dealer_seat,
            small_blind_seat=sb_seat,
            big_blind_seat=bb_seat,
            small_blind_amount=sb,
            big_blind_amount=bb,
            player_names=player_names,
            stacks=stacks,
        )
        await self._broadcast(hand_start_msg)

        hand = PokerHand(
            active_players=active,
            dealer_pk=dealer_pk,
            sb_amount=sb,
            bb_amount=bb,
            hand_number=self.hand_number,
        )

        # Action loop
        while not hand.is_over:
            actor_pk = hand.actor_pk
            if actor_pk is None:
                # Shouldn't happen with full automations, but guard anyway
                break

            actor_seat = hand.actor_seat
            actor_player = active[actor_pk]

            # Send personalized action_request to every active player
            await self._broadcast_personalized(
                active,
                lambda p: build_action_request(actor_seat, hand.get_game_state(p.seat_index)),
            )

            # Wait for actor's action (with timeout)
            action_type, amount, timed_out = await self._get_action(actor_player)

            # Apply action
            hand.apply_action(action_type, amount)

            # Broadcast action result to everyone
            await self._broadcast_personalized(
                active,
                lambda p: build_action_result(
                    actor_seat=actor_seat,
                    player_name=actor_player.name,
                    action_type=action_type,
                    amount=amount,
                    timed_out=timed_out,
                    game_state=hand.get_game_state(p.seat_index),
                ),
            )

        # Hand complete — collect results
        result = hand.get_hand_result()

        # Update tournament stacks from PokerKit results
        newly_eliminated: list[int] = []
        for pk_i, player in enumerate(active):
            player.stack = result["final_stacks_by_pk"][pk_i]
            if player.stack == 0 and not player.is_eliminated:
                player.is_eliminated = True
                newly_eliminated.append(player.seat_index)
                log.info("Player '%s' eliminated.", player.name)

        all_names = [p.name for p in self.players]
        all_stacks = [p.stack for p in self.players]

        await self._broadcast(build_hand_end(
            hand_number=self.hand_number,
            winners=result["winners"],
            hole_cards_revealed=result["hole_cards_revealed"],
            final_stacks=all_stacks,
            player_names=all_names,
            eliminated_seats=newly_eliminated,
        ))

    # ------------------------------------------------------------------
    # Action collection
    # ------------------------------------------------------------------

    async def _get_action(
        self, player: PlayerInfo
    ) -> tuple[str, int | None, bool]:
        """
        Wait for the player to submit an action, with timeout.
        Returns (action_type, amount, timed_out).
        Auto-folds on timeout or disconnect.
        """
        # Drain stale messages before waiting
        while not player.action_queue.empty():
            player.action_queue.get_nowait()

        try:
            msg = await asyncio.wait_for(
                player.action_queue.get(),
                timeout=config.ACTION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            log.info("Player '%s' timed out — auto-folding.", player.name)
            return "fold", None, True

        if msg is _DISCONNECT_SENTINEL:
            log.info("Player '%s' disconnected — auto-folding.", player.name)
            return "fold", None, True

        try:
            action = msg.get("action", {})
            action_type = action.get("type", "fold")
            amount = action.get("amount")
            if amount is not None:
                amount = int(amount)
            return action_type, amount, False
        except (AttributeError, ValueError, TypeError):
            return "fold", None, True

    # ------------------------------------------------------------------
    # Broadcast helpers
    # ------------------------------------------------------------------

    async def _broadcast(self, msg: dict) -> None:
        """Send the same message to all connected (non-eliminated) players."""
        targets = [p for p in self.players if not p.is_eliminated]
        await asyncio.gather(*(p.send(msg) for p in targets))

    async def _broadcast_personalized(
        self,
        players: list[PlayerInfo],
        builder,           # Callable[[PlayerInfo], dict]
    ) -> None:
        """Send a per-player message built by builder(player)."""
        await asyncio.gather(*(p.send(builder(p)) for p in players))

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _active_players(self) -> list[PlayerInfo]:
        """Return non-eliminated players sorted by seat_index."""
        return sorted(
            (p for p in self.players if not p.is_eliminated),
            key=lambda p: p.seat_index,
        )

    def _rotate_dealer(self, active: list[PlayerInfo]) -> None:
        """Advance dealer_seat to the next active player."""
        seats = [p.seat_index for p in active]
        try:
            current = seats.index(self.dealer_seat)
        except ValueError:
            current = -1
        self.dealer_seat = seats[(current + 1) % len(seats)]

    # ------------------------------------------------------------------
    # Public accessor for server.py
    # ------------------------------------------------------------------

    @property
    def is_started(self) -> bool:
        return self.started


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _get_blinds(hand_number: int) -> tuple[int, int]:
    """
    Return (small_blind, big_blind) for the given hand number based on
    the configured blind schedule.
    """
    schedule = config.BLIND_SCHEDULE
    applicable_key = max(k for k in schedule if k <= hand_number)
    return schedule[applicable_key]


def _find_pk(active: list[PlayerInfo], seat: int) -> int:
    """Return the PokerKit index of the player with the given seat_index."""
    for i, p in enumerate(active):
        if p.seat_index == seat:
            return i
    # Fallback: return 0 if not found (shouldn't happen)
    return 0
