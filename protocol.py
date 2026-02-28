"""
Message type constants and JSON builder functions for the AlgoPoker protocol.

All builders return plain dicts. Callers serialize with json.dumps().

Server -> Bot messages:
  waiting        - lobby state update after a player joins
  game_start     - tournament is starting
  hand_start     - new hand is beginning
  action_request - it is your turn to act
  action_result  - a player has acted (broadcast to all)
  hand_end       - hand is over, results revealed
  game_end       - tournament is over, winner declared
  error          - something went wrong

Bot -> Server messages:
  join   - register with a player name (sent once on connect)
  action - fold / check / call / raise
"""

# --- Server -> Bot message types ---
MSG_WAITING        = "waiting"
MSG_GAME_START     = "game_start"
MSG_HAND_START     = "hand_start"
MSG_ACTION_REQUEST = "action_request"
MSG_ACTION_RESULT  = "action_result"
MSG_HAND_END       = "hand_end"
MSG_GAME_END       = "game_end"
MSG_ERROR          = "error"

# --- Bot -> Server message types ---
MSG_JOIN   = "join"
MSG_ACTION = "action"

# --- Action type strings ---
ACTION_FOLD  = "fold"
ACTION_CHECK = "check"
ACTION_CALL  = "call"
ACTION_RAISE = "raise"

# --- Error codes ---
ERR_BAD_JOIN          = "BAD_JOIN"
ERR_BAD_NAME          = "BAD_NAME"
ERR_TOURNAMENT_FULL   = "TOURNAMENT_FULL"
ERR_TOURNAMENT_STARTED = "TOURNAMENT_STARTED"
ERR_BAD_JSON          = "BAD_JSON"
ERR_UNKNOWN_TYPE      = "UNKNOWN_TYPE"


def build_waiting(current_count: int, min_players: int, max_players: int) -> dict:
    return {
        "type": MSG_WAITING,
        "current_players": current_count,
        "min_players": min_players,
        "max_players": max_players,
    }


def build_game_start(
    player_names: list[str],
    starting_stacks: list[int],
    small_blind: int,
    big_blind: int,
) -> dict:
    return {
        "type": MSG_GAME_START,
        "player_names": player_names,
        "starting_stacks": starting_stacks,
        "small_blind": small_blind,
        "big_blind": big_blind,
    }


def build_hand_start(
    hand_number: int,
    dealer_seat: int,
    small_blind_seat: int,
    big_blind_seat: int,
    small_blind_amount: int,
    big_blind_amount: int,
    player_names: list[str],
    stacks: list[int],
    hole_cards: list[str],
) -> dict:
    return {
        "type": MSG_HAND_START,
        "hand_number": hand_number,
        "dealer_seat": dealer_seat,
        "small_blind_seat": small_blind_seat,
        "big_blind_seat": big_blind_seat,
        "small_blind_amount": small_blind_amount,
        "big_blind_amount": big_blind_amount,
        "player_names": player_names,
        "stacks": stacks,
        "hole_cards": hole_cards,
    }


def build_action_request(actor_seat: int, game_state: dict) -> dict:
    return {
        "type": MSG_ACTION_REQUEST,
        "actor_seat": actor_seat,
        "timeout_seconds": 30,
        "game_state": game_state,
    }


def build_action_result(
    actor_seat: int,
    player_name: str,
    action_type: str,
    amount: int | None,
    timed_out: bool,
    game_state: dict,
) -> dict:
    return {
        "type": MSG_ACTION_RESULT,
        "actor_seat": actor_seat,
        "player_name": player_name,
        "action": {
            "type": action_type,
            "amount": amount,
        },
        "timed_out": timed_out,
        "game_state": game_state,
    }


def build_hand_end(
    hand_number: int,
    winners: list[dict],
    hole_cards_revealed: list[dict],
    final_stacks: list[int],
    player_names: list[str],
    eliminated_seats: list[int],
) -> dict:
    """
    winners: [{"seat": int, "name": str, "amount_won": int}]
    hole_cards_revealed: [{"seat": int, "name": str, "hole_cards": ["As","Kd"]}]
    """
    return {
        "type": MSG_HAND_END,
        "hand_number": hand_number,
        "winners": winners,
        "hole_cards_revealed": hole_cards_revealed,
        "final_stacks": final_stacks,
        "player_names": player_names,
        "eliminated_seats": eliminated_seats,
    }


def build_game_end(
    winner_name: str,
    winner_seat: int,
    final_stacks: list[int],
    player_names: list[str],
    total_hands: int,
) -> dict:
    return {
        "type": MSG_GAME_END,
        "winner": winner_name,
        "winner_seat": winner_seat,
        "final_stacks": final_stacks,
        "player_names": player_names,
        "total_hands": total_hands,
    }


def build_error(code: str, message: str) -> dict:
    return {
        "type": MSG_ERROR,
        "code": code,
        "message": message,
    }
