"""
PokerKit integration layer for a single hand of No-Limit Texas Hold'em.

Key design choices:
- All PokerKit automations are enabled so the game engine handles antes,
  blinds, hole dealing, board dealing, burn cards, showdown, and chip
  distribution automatically. The server only needs to supply betting actions.
- active_players is the ordered list of non-eliminated PlayerInfo objects
  for the current hand. Their list index equals their PokerKit player index.
- Dealer is tracked as an index into active_players.
- For heads-up (2 players), PokerKit swaps the blind amounts internally
  (player[i] receives blinds_or_straddles[1-i]).  We compensate by
  reversing the blind tuple so the correct amounts land on each seat.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pokerkit import Automation, Mode, NoLimitTexasHoldem

if TYPE_CHECKING:
    from pokerkit import State

AUTOMATIONS = (
    Automation.ANTE_POSTING,
    Automation.BET_COLLECTION,
    Automation.BLIND_OR_STRADDLE_POSTING,
    Automation.CARD_BURNING,
    Automation.HOLE_DEALING,
    Automation.BOARD_DEALING,
    Automation.HOLE_CARDS_SHOWING_OR_MUCKING,
    Automation.HAND_KILLING,
    Automation.CHIPS_PUSHING,
    Automation.CHIPS_PULLING,
)

STREET_NAMES = ["preflop", "flop", "turn", "river"]


def _build_blind_tuple(
    player_count: int,
    dealer_pk: int,
    sb_amount: int,
    bb_amount: int,
) -> tuple[int, ...]:
    """
    Build the raw_blinds_or_straddles tuple for PokerKit.

    For 3+ players: SB sits at (dealer+1)%n, BB at (dealer+2)%n.
    For heads-up:   PokerKit swaps blind amounts (player[i] gets
                    blinds[1-i]), so we place BB at index dealer and
                    SB at index (1-dealer) to get correct posting.
    """
    blinds = [0] * player_count
    if player_count == 2:
        sb_pk = dealer_pk          # dealer IS the SB in heads-up
        bb_pk = 1 - dealer_pk
        # PokerKit swaps: player[i] posts blinds[1-i]
        # → to make player[sb_pk] post sb_amount: blinds[1-sb_pk] = sb_amount
        # → to make player[bb_pk] post bb_amount: blinds[1-bb_pk] = bb_amount
        blinds[1 - sb_pk] = sb_amount
        blinds[1 - bb_pk] = bb_amount
    else:
        sb_pk = (dealer_pk + 1) % player_count
        bb_pk = (dealer_pk + 2) % player_count
        blinds[sb_pk] = sb_amount
        blinds[bb_pk] = bb_amount
    return tuple(blinds)


@dataclass
class ActionResult:
    action_type: str
    amount: int | None
    timed_out: bool = False


class PokerHand:
    """
    Wraps one hand of NLHE. Construct a fresh instance for each hand.

    Parameters
    ----------
    active_players : list of PlayerInfo
        Ordered list of players in this hand.
        active_players[i].seat_index is their permanent tournament seat.
        PokerKit index i corresponds to active_players[i].
    dealer_pk : int
        Index into active_players identifying the dealer button.
    sb_amount, bb_amount : int
        Current blind levels.
    hand_number : int
        For informational purposes only (included in game state).
    """

    def __init__(
        self,
        active_players: list,          # list[PlayerInfo]
        dealer_pk: int,
        sb_amount: int,
        bb_amount: int,
        hand_number: int,
    ) -> None:
        self.active_players = active_players
        self.dealer_pk = dealer_pk
        self.sb_amount = sb_amount
        self.bb_amount = bb_amount
        self.hand_number = hand_number

        n = len(active_players)
        self.sb_pk = dealer_pk if n == 2 else (dealer_pk + 1) % n
        self.bb_pk = (1 - dealer_pk) if n == 2 else (dealer_pk + 2) % n

        blinds = _build_blind_tuple(n, dealer_pk, sb_amount, bb_amount)
        stacks = tuple(int(p.stack) for p in active_players)

        self.state: State = NoLimitTexasHoldem.create_state(
            automations=AUTOMATIONS,
            ante_trimming_status=True,
            raw_antes=0,
            raw_blinds_or_straddles=blinds,
            min_bet=bb_amount,
            raw_starting_stacks=stacks,
            player_count=n,
            mode=Mode.TOURNAMENT,
        )

        # Snapshot hole cards immediately after dealing (HOLE_DEALING automation
        # already ran inside create_state). Stored so get_hand_result() can reveal
        # ALL showdown participants' cards, even if HOLE_CARDS_SHOWING_OR_MUCKING
        # automation mucked the loser's hand.
        self._dealt_hole_cards: dict[int, list[str]] = {
            pk_i: [repr(c) for c in self.state.hole_cards[pk_i]]
            for pk_i in range(n)
        }

        # Updated at the START of every apply_action() call so that when the hand
        # ends (automations fire mid-call) we still know who was active going in.
        self._active_before_last_action: list[int] = list(range(n))

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_over(self) -> bool:
        """True when the hand has finished (PokerKit state is terminal)."""
        return not self.state.status

    @property
    def actor_pk(self) -> int | None:
        """PokerKit index of the player who must act, or None."""
        return self.state.actor_index

    @property
    def actor_seat(self) -> int | None:
        """Tournament seat index of the player who must act, or None."""
        pk = self.state.actor_index
        if pk is None:
            return None
        return self.active_players[pk].seat_index

    @property
    def current_street(self) -> str:
        idx = self.state.street_index
        if idx is None:
            return "showdown"
        return STREET_NAMES[idx] if idx < len(STREET_NAMES) else "unknown"

    # ------------------------------------------------------------------
    # Action handling
    # ------------------------------------------------------------------

    def apply_action(self, action_type: str, amount: int | None = None) -> None:
        """
        Apply a player action to the PokerKit state.

        For 'raise', amount is clamped to [min, max] so invalid amounts
        are silently corrected rather than errored.
        """
        # Record active players BEFORE calling PokerKit. After the call,
        # automations may have run and statuses will already be updated.
        self._active_before_last_action = [
            pk_i for pk_i in range(len(self.active_players))
            if bool(self.state.statuses[pk_i])
        ]

        if action_type == ACTION_FOLD:
            self.state.fold()
        elif action_type in (ACTION_CHECK, ACTION_CALL):
            self.state.check_or_call()
        elif action_type == ACTION_RAISE:
            if amount is None:
                amount = int(self.state.min_completion_betting_or_raising_to_amount)
            else:
                min_r = int(self.state.min_completion_betting_or_raising_to_amount)
                max_r = int(self.state.max_completion_betting_or_raising_to_amount)
                amount = max(min_r, min(max_r, int(amount)))
            self.state.complete_bet_or_raise_to(amount)
        else:
            raise ValueError(f"Unknown action type: {action_type!r}")

    def get_valid_actions(self) -> list[dict]:
        """Return the list of legal actions for the current actor."""
        actions: list[dict] = []

        if self.state.can_fold():
            actions.append({"type": ACTION_FOLD})

        # checking_or_calling_amount is None when the hand is over or between
        # streets — return what we have so far (fold only, or empty).
        raw_call = self.state.checking_or_calling_amount
        if raw_call is None:
            return actions

        call_amount = int(raw_call)
        if call_amount == 0:
            actions.append({"type": ACTION_CHECK})
        else:
            actions.append({"type": ACTION_CALL, "amount": call_amount})

        min_r = self.state.min_completion_betting_or_raising_to_amount
        max_r = self.state.max_completion_betting_or_raising_to_amount
        if (
            min_r is not None
            and max_r is not None
            and self.state.can_complete_bet_or_raise_to(min_r)
        ):
            actions.append({
                "type": ACTION_RAISE,
                "min_amount": int(min_r),
                "max_amount": int(max_r),
            })

        return actions

    # ------------------------------------------------------------------
    # Game state serialization
    # ------------------------------------------------------------------

    def get_game_state(self, perspective_seat: int) -> dict:
        """
        Build the full game state dict from one player's perspective.

        Only that player's hole cards are revealed; all others are censored
        to "??" so opponents cannot see each other's cards.
        """
        state = self.state
        perspective_pk = self._seat_to_pk(perspective_seat)

        # Community cards: board_cards is a list of single-card lists.
        # Flatten to get e.g. ["Jc", "3d", "5c"] for the flop.
        community_cards = [
            repr(card)
            for sublist in state.board_cards
            for card in sublist
        ]

        # Pot
        total_pot = int(state.total_pot_amount)
        pots_list = [
            {
                "amount": int(pot.amount),
                "eligible_seats": [
                    self.active_players[pk_i].seat_index
                    for pk_i in pot.player_indices
                ],
            }
            for pot in state.pots
        ]

        # Players
        players = []
        for pk_i, player in enumerate(self.active_players):
            stack = int(state.stacks[pk_i])
            bet = int(state.bets[pk_i])
            is_active = bool(state.statuses[pk_i])
            is_all_in = is_active and stack == 0

            # Reveal hole cards only for the perspective player
            raw_cards = state.hole_cards[pk_i]
            if pk_i == perspective_pk:
                hole_cards = [repr(c) for c in raw_cards] if raw_cards else []
                hole_cards_known = True
            else:
                hole_cards = ["??" for _ in raw_cards] if raw_cards else []
                hole_cards_known = False

            players.append({
                "seat": player.seat_index,
                "name": player.name,
                "stack": stack,
                "current_bet": bet,
                "is_active": is_active,
                "is_all_in": is_all_in,
                "is_dealer": pk_i == self.dealer_pk,
                "is_small_blind": pk_i == self.sb_pk,
                "is_big_blind": pk_i == self.bb_pk,
                "hole_cards": hole_cards,
                "hole_cards_known": hole_cards_known,
            })

        actor_pk = state.actor_index
        actor_seat = (
            self.active_players[actor_pk].seat_index
            if actor_pk is not None
            else None
        )

        return {
            "street": self.current_street,
            "hand_number": self.hand_number,
            "community_cards": community_cards,
            "pot": {
                "total": total_pot,
                "pots": pots_list,
            },
            "players": players,
            "actor_seat": actor_seat,
            "valid_actions": self.get_valid_actions(),
            "dealer_seat": self.active_players[self.dealer_pk].seat_index,
            "small_blind_seat": self.active_players[self.sb_pk].seat_index,
            "big_blind_seat": self.active_players[self.bb_pk].seat_index,
            "small_blind_amount": self.sb_amount,
            "big_blind_amount": self.bb_amount,
        }

    def get_spectator_game_state(self) -> dict:
        """
        Same as get_game_state() but every player's hole cards are fully
        revealed.  Used exclusively for spectator connections so the observer
        can see all hands simultaneously.
        """
        state = self.state

        community_cards = [
            repr(card)
            for sublist in state.board_cards
            for card in sublist
        ]

        total_pot = int(state.total_pot_amount)
        pots_list = [
            {
                "amount": int(pot.amount),
                "eligible_seats": [
                    self.active_players[pk_i].seat_index
                    for pk_i in pot.player_indices
                ],
            }
            for pot in state.pots
        ]

        players = []
        for pk_i, player in enumerate(self.active_players):
            stack     = int(state.stacks[pk_i])
            bet       = int(state.bets[pk_i])
            is_active = bool(state.statuses[pk_i])
            is_all_in = is_active and stack == 0

            # All cards revealed for spectators
            raw_cards  = state.hole_cards[pk_i]
            hole_cards = [repr(c) for c in raw_cards] if raw_cards else []

            players.append({
                "seat": player.seat_index,
                "name": player.name,
                "stack": stack,
                "current_bet": bet,
                "is_active": is_active,
                "is_all_in": is_all_in,
                "is_dealer": pk_i == self.dealer_pk,
                "is_small_blind": pk_i == self.sb_pk,
                "is_big_blind": pk_i == self.bb_pk,
                "hole_cards": hole_cards,
                "hole_cards_known": True,
            })

        actor_pk   = state.actor_index
        actor_seat = (
            self.active_players[actor_pk].seat_index
            if actor_pk is not None else None
        )

        return {
            "street": self.current_street,
            "hand_number": self.hand_number,
            "community_cards": community_cards,
            "pot": {"total": total_pot, "pots": pots_list},
            "players": players,
            "actor_seat": actor_seat,
            "valid_actions": self.get_valid_actions(),
            "dealer_seat": self.active_players[self.dealer_pk].seat_index,
            "small_blind_seat": self.active_players[self.sb_pk].seat_index,
            "big_blind_seat": self.active_players[self.bb_pk].seat_index,
            "small_blind_amount": self.sb_amount,
            "big_blind_amount": self.bb_amount,
        }

    def get_hole_cards(self, seat_index: int) -> list[str]:
        """Return the two hole cards for one player as card strings e.g. ['As', 'Kd']."""
        pk_i = self._seat_to_pk(seat_index)
        raw = self.state.hole_cards[pk_i]
        return [repr(c) for c in raw] if raw else []

    def get_hand_result(self) -> dict:
        """
        After is_over is True, return winners, revealed hole cards, community
        cards, and final stacks.
        """
        state = self.state
        n = len(self.active_players)

        # Winners: players whose net payoff is positive.
        winners = []
        for pk_i, player in enumerate(self.active_players):
            payoff = int(state.payoffs[pk_i])
            if payoff > 0:
                winners.append({
                    "seat": player.seat_index,
                    "name": player.name,
                    "amount_won": payoff,
                })

        # Showdown detection: with HOLE_CARDS_SHOWING_OR_MUCKING automation,
        # PokerKit leaves at least one player's hole_cards non-empty when cards
        # were tabled. When the hand was won by everyone folding, all hole_cards
        # are mucked (empty tuples), so the generator is falsy.
        showdown_occurred = any(state.hole_cards[pk_i] for pk_i in range(n))

        if showdown_occurred:
            # Reveal the originally-dealt cards for every player who was still
            # in the hand when the final action was taken. This overrides the
            # automation's muck/show decision so ALL showdown hands are visible.
            hole_cards_revealed = [
                {
                    "seat": self.active_players[pk_i].seat_index,
                    "name": self.active_players[pk_i].name,
                    "hole_cards": self._dealt_hole_cards[pk_i],
                }
                for pk_i in self._active_before_last_action
            ]
        else:
            hole_cards_revealed = []

        # Community cards at hand end (same flattening logic as get_game_state).
        community_cards = [
            repr(card)
            for sublist in state.board_cards
            for card in sublist
        ]

        # Final stacks from PokerKit (after CHIPS_PULLING automation).
        final_stacks_by_pk = [int(state.stacks[i]) for i in range(n)]

        return {
            "winners": winners,
            "hole_cards_revealed": hole_cards_revealed,
            "community_cards": community_cards,
            "final_stacks_by_pk": final_stacks_by_pk,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _seat_to_pk(self, seat_index: int) -> int:
        for pk_i, player in enumerate(self.active_players):
            if player.seat_index == seat_index:
                return pk_i
        raise ValueError(f"Seat {seat_index} not found in active players")


# Action type constants (mirrors protocol.py to keep game.py self-contained)
ACTION_FOLD  = "fold"
ACTION_CHECK = "check"
ACTION_CALL  = "call"
ACTION_RAISE = "raise"
