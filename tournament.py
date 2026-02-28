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
    ERR_BAD_ACTION,
    ACTION_RAISE,
)

log = logging.getLogger("algopoker.tournament")

# Sentinel object placed in action_queue to unblock a waiting get() when
# a bot disconnects mid-hand.
_DISCONNECT_SENTINEL = object()


@dataclass
class SpectatorInfo:
    websocket: object        # websockets ServerConnection
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, msg: dict) -> None:
        """Serialized send; silently ignores closed connections."""
        import websockets
        try:
            async with self.send_lock:
                await self.websocket.send(json.dumps(msg))
        except (websockets.exceptions.ConnectionClosed, Exception):
            pass


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
        self.spectators: list[SpectatorInfo] = []
        self.started: bool = False
        self.hand_number: int = 0
        self.dealer_seat: int = -1            # permanent seat of current dealer
        self._start_task: asyncio.Task | None = None
        self._lobby_ready: asyncio.Event = asyncio.Event()  # set to force-start

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

        # Notify all connected players and spectators of the current lobby state
        waiting_msg = build_waiting(len(self.players), config.MIN_PLAYERS, config.MAX_PLAYERS)
        await self._broadcast(waiting_msg)
        await self._broadcast_spectators(waiting_msg)

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

    async def register_spectator(self, websocket) -> SpectatorInfo:
        """Register a read-only spectator connection."""
        spectator = SpectatorInfo(websocket=websocket)
        self.spectators.append(spectator)
        log.info("Spectator connected. Total spectators: %d", len(self.spectators))
        return spectator

    def remove_spectator(self, spectator: SpectatorInfo) -> None:
        """Remove a spectator on disconnect."""
        try:
            self.spectators.remove(spectator)
        except ValueError:
            pass
        log.info("Spectator disconnected. Total spectators: %d", len(self.spectators))

    # ------------------------------------------------------------------
    # Tournament lifecycle
    # ------------------------------------------------------------------

    async def _delayed_start(self) -> None:
        log.info("Minimum players reached. Waiting for more players or force-start...")
        await self._lobby_ready.wait()
        if not self.started:
            await self._start_tournament()

    async def force_start(self) -> None:
        """Force-start the tournament immediately (triggered by spectator UI)."""
        if self.started:
            return
        if len(self.players) < config.MIN_PLAYERS:
            log.warning("force_start called but not enough players (%d < %d).",
                        len(self.players), config.MIN_PLAYERS)
            return
        log.info("Force-starting tournament via spectator UI.")
        self._lobby_ready.set()
        if self._start_task is None or self._start_task.done():
            self._start_task = asyncio.create_task(self._start_tournament())

    async def _start_tournament(self) -> None:
        if self.started:
            return
        self.started = True
        self.dealer_seat = self.players[0].seat_index

        names = [p.name for p in self.players]
        stacks = [p.stack for p in self.players]
        sb, bb = _get_blinds(0)

        log.info("Tournament starting with %d players.", len(self.players))
        game_start_msg = build_game_start(names, stacks, sb, bb)
        await self._broadcast(game_start_msg)
        await self._broadcast_spectators(game_start_msg)

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
        game_end_msg = build_game_end(
            winner_name=winner.name if winner else "?",
            winner_seat=winner.seat_index if winner else -1,
            final_stacks=stacks,
            player_names=names,
            total_hands=self.hand_number,
        )
        await self._broadcast(game_end_msg)
        await self._broadcast_spectators(game_end_msg)

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

        # Create the hand first so hole cards are available for hand_start
        hand = PokerHand(
            active_players=active,
            dealer_pk=dealer_pk,
            sb_amount=sb,
            bb_amount=bb,
            hand_number=self.hand_number,
        )

        # Send each player a personalised hand_start with their own hole cards
        await self._broadcast_personalized(
            active,
            lambda p: build_hand_start(
                hand_number=self.hand_number,
                dealer_seat=self.dealer_seat,
                small_blind_seat=sb_seat,
                big_blind_seat=bb_seat,
                small_blind_amount=sb,
                big_blind_amount=bb,
                player_names=player_names,
                stacks=stacks,
                hole_cards=hand.get_hole_cards(p.seat_index),
            ),
        )
        # Spectators get a hand_start too — we send a version with all hole cards
        # revealed via the spectator game state (sent as the first action_request).
        # For the hand_start itself we send the same structural info with no hole cards
        # (the UI will populate cards from the first action_request's spectator game_state).
        await self._broadcast_spectators(build_hand_start(
            hand_number=self.hand_number,
            dealer_seat=self.dealer_seat,
            small_blind_seat=sb_seat,
            big_blind_seat=bb_seat,
            small_blind_amount=sb,
            big_blind_amount=bb,
            player_names=player_names,
            stacks=stacks,
            hole_cards=[],
        ))

        # Action loop
        while not hand.is_over:
            actor_pk = hand.actor_pk
            if actor_pk is None:
                # Shouldn't happen with full automations, but guard anyway
                break

            actor_seat = hand.actor_seat
            actor_player = active[actor_pk]

            # Capture valid actions for this actor before broadcasting.
            # These are the exact same actions embedded in the game_state below,
            # and are used to validate the bot's response.
            valid_actions = hand.get_valid_actions()

            # Drain any stale messages from the actor's queue BEFORE broadcasting
            # the action_request.  Draining after would race with a fast bot that
            # responds before _get_action is entered.
            while not actor_player.action_queue.empty():
                actor_player.action_queue.get_nowait()

            # Send personalized action_request to every active player
            await self._broadcast_personalized(
                active,
                lambda p: build_action_request(actor_seat, hand.get_game_state(p.seat_index)),
            )
            await self._broadcast_spectators(
                build_action_request(actor_seat, hand.get_spectator_game_state())
            )

            # Wait for actor's action (with timeout and server-side validation)
            action_type, amount, timed_out = await self._get_action(actor_player, valid_actions)

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
            await self._broadcast_spectators(build_action_result(
                actor_seat=actor_seat,
                player_name=actor_player.name,
                action_type=action_type,
                amount=amount,
                timed_out=timed_out,
                game_state=hand.get_spectator_game_state(),
            ))

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

        hand_end_msg = build_hand_end(
            hand_number=self.hand_number,
            winners=result["winners"],
            hole_cards_revealed=result["hole_cards_revealed"],
            community_cards=result["community_cards"],
            final_stacks=all_stacks,
            player_names=all_names,
            eliminated_seats=newly_eliminated,
        )
        await self._broadcast(hand_end_msg)
        await self._broadcast_spectators(hand_end_msg)

    # ------------------------------------------------------------------
    # Action collection
    # ------------------------------------------------------------------

    async def _get_action(
        self, player: PlayerInfo, valid_actions: list[dict]
    ) -> tuple[str, int | None, bool]:
        """
        Wait for the player to submit an action, validate it, and return it.
        Returns (action_type, amount, timed_out).
        Auto-folds on timeout, disconnect, or an action that fails validation.

        Validation rules:
          - action.type must be a string present in valid_actions
          - raise must include a numeric amount; it is clamped to [min, max]
            (per protocol: "clamped server-side, so a slightly off value won't
            be rejected"), but a missing or non-numeric amount is rejected
        """
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

        # --- Parse ---
        try:
            action     = msg.get("action", {})
            action_type = str(action.get("type", ""))
            raw_amount  = action.get("amount")
            amount      = int(raw_amount) if raw_amount is not None else None
        except (AttributeError, ValueError, TypeError):
            log.warning("Player '%s' sent malformed action — auto-folding.", player.name)
            await player.send(build_error(ERR_BAD_ACTION, "Malformed action object."))
            return "fold", None, True

        # --- Validate action type ---
        valid_types = {a["type"] for a in valid_actions}
        if action_type not in valid_types:
            err = (
                f"Action type {action_type!r} is not valid. "
                f"Valid types right now: {sorted(valid_types)}."
            )
            log.warning("Player '%s' sent invalid action type %r — auto-folding.", player.name, action_type)
            await player.send(build_error(ERR_BAD_ACTION, err))
            return "fold", None, True

        # --- Validate raise amount ---
        if action_type == ACTION_RAISE:
            raise_info = next(a for a in valid_actions if a["type"] == ACTION_RAISE)
            min_r = raise_info["min_amount"]
            max_r = raise_info["max_amount"]

            if amount is None:
                err = f"Raise requires an 'amount'. Valid range: [{min_r}, {max_r}]."
                log.warning("Player '%s' sent raise with no amount — auto-folding.", player.name)
                await player.send(build_error(ERR_BAD_ACTION, err))
                return "fold", None, True

            # Clamp to valid range per protocol spec
            if not (min_r <= amount <= max_r):
                clamped = max(min_r, min(max_r, amount))
                log.info(
                    "Player '%s' raise amount %d clamped to %d (valid range [%d, %d]).",
                    player.name, amount, clamped, min_r, max_r,
                )
                amount = clamped

        return action_type, amount, False

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

    async def _broadcast_spectators(self, msg: dict) -> None:
        """Send a message to all connected spectators."""
        if self.spectators:
            await asyncio.gather(*(s.send(msg) for s in self.spectators))

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
