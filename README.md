# AlgoPoker — Bot Development Guide

A WebSocket-based No-Limit Texas Hold'em tournament server.  You write a bot, connect it, and it plays automatically.

---

## Quick Start

**1. Install dependencies**
```bash
pip install -r requirements.txt
```

**2. Start the server**
```bash
python run.py
```

**3. Connect bots** (each in its own terminal)
```bash
python example_bot.py --name Bot1
python example_bot.py --name Bot2
```

The tournament starts automatically once 2 players have joined.  Up to 9 can play.

---

## Files

| File | Purpose |
|------|---------|
| `example_bot.py` | **Start here.** Annotated bot covering every message type |
| `human_bot.py` | Play interactively from the terminal |
| `PROTOCOL.md` | Full protocol reference (all message schemas) |
| `run.py` | Server entry point |

---

## Writing Your Own Bot

Copy `example_bot.py` and replace the `decide_action` function at the top.  That's the only part you need to change.

```python
def decide_action(game_state: dict, my_seat: int) -> dict:
    valid_actions = game_state["valid_actions"]

    # Your logic here.  Return one of the valid actions.
    ...
```

### The `game_state` object

Every time it's your turn you receive a `game_state` dict with the full observable state of the hand:

```python
game_state = {
    "street": "flop",                        # preflop / flop / turn / river
    "hand_number": 7,
    "community_cards": ["Jc", "3d", "5c"],   # empty list on preflop
    "pot": {
        "total": 600,
        "pots": [{"amount": 600, "eligible_seats": [0, 1, 2]}]
    },
    "players": [...],       # see below
    "valid_actions": [...], # see below — only act on these
}
```

**Players list** — one entry per active seat:
```python
{
    "seat": 0,
    "name": "Alice",
    "stack": 9500,          # chips not yet in the pot
    "current_bet": 200,     # chips wagered this street (not yet in pot)
    "is_active": True,      # False = folded this hand
    "is_all_in": False,
    "is_dealer": False,
    "is_small_blind": True,
    "is_big_blind": False,
    "hole_cards": ["As", "Kd"],  # YOUR cards only; opponents show ["??", "??"]
    "hole_cards_known": True,    # True only for your own seat
}
```

**Valid actions** — the server tells you exactly what you're allowed to do:
```python
{"type": "fold"}
{"type": "check"}                           # only when no bet to call
{"type": "call",  "amount": 200}            # exact chips to call
{"type": "raise", "min_amount": 400, "max_amount": 9500}
```

### Returning an action

```python
# Fold
return {"type": "fold"}

# Check
return {"type": "check"}

# Call
return {"type": "call"}

# Raise — amount is the TOTAL bet size, not the raise increment
return {"type": "raise", "amount": 800}
```

Raise amounts outside `[min_amount, max_amount]` are clamped server-side, so slightly off values still work.

You have **30 seconds** to respond.  Exceeding the limit results in an automatic fold.

---

## Example Strategies

### Always check or call (never fold)
```python
def decide_action(game_state, my_seat):
    types = {a["type"] for a in game_state["valid_actions"]}
    if "check" in types:
        return {"type": "check"}
    if "call" in types:
        return {"type": "call"}
    return {"type": "fold"}
```

### Minimum raise every turn
```python
def decide_action(game_state, my_seat):
    for action in game_state["valid_actions"]:
        if action["type"] == "raise":
            return {"type": "raise", "amount": action["min_amount"]}
    # Fall back to call/check if raise isn't available
    types = {a["type"] for a in game_state["valid_actions"]}
    if "call" in types:
        return {"type": "call"}
    return {"type": "check"}
```

### Use position — tighten up out of position
```python
def decide_action(game_state, my_seat):
    # Find the dealer seat
    dealer_seat = next(p["seat"] for p in game_state["players"] if p["is_dealer"])

    # Simple heuristic: play aggressively in position (on or near the button)
    seats = [p["seat"] for p in game_state["players"] if p["is_active"]]
    n = len(seats)
    dealer_pos = seats.index(dealer_seat)
    my_pos = seats.index(my_seat)
    distance_from_button = (my_pos - dealer_pos) % n  # 0 = button, 1 = CO, etc.

    types = {a["type"] for a in game_state["valid_actions"]}

    if distance_from_button <= 1:
        # In position — raise if possible
        for action in game_state["valid_actions"]:
            if action["type"] == "raise":
                return {"type": "raise", "amount": action["min_amount"]}

    # Out of position — check or call, avoid raising
    if "check" in types:
        return {"type": "check"}
    if "call" in types:
        return {"type": "call"}
    return {"type": "fold"}
```

### Reading your hole cards
```python
def decide_action(game_state, my_seat):
    # Get your hole cards
    my_player = next(p for p in game_state["players"] if p["seat"] == my_seat)
    hole_cards = my_player["hole_cards"]  # e.g. ["Ah", "Kd"]

    rank1, suit1 = hole_cards[0][0], hole_cards[0][1]
    rank2, suit2 = hole_cards[1][0], hole_cards[1][1]

    strong_ranks = {"A", "K", "Q", "J", "T"}
    is_strong = rank1 in strong_ranks and rank2 in strong_ranks

    types = {a["type"] for a in game_state["valid_actions"]}

    if is_strong:
        # Raise with strong hands
        for action in game_state["valid_actions"]:
            if action["type"] == "raise":
                return {"type": "raise", "amount": action["min_amount"]}

    if "check" in types:
        return {"type": "check"}
    if "call" in types:
        return {"type": "call"}
    return {"type": "fold"}
```

---

## Connecting to a Remote Server

```bash
# Plain WebSocket
python example_bot.py --name MyBot --host 192.168.1.10 --port 8765

# Secure WebSocket (self-signed certs are accepted)
python example_bot.py --name MyBot --host myserver.example.com --port 443
```

You can also pass a full URI directly:
```bash
python example_bot.py --name MyBot --host wss://myserver.example.com
```

---

## Tournament Rules

- **Starting stack:** 10,000 chips
- **Action timeout:** 30 seconds (auto-fold on expiry)
- **Players:** 2–9 per tournament
- **Blind schedule:**

| Starting hand | Small blind | Big blind |
|:---:|---:|---:|
| 1  | 50   | 100   |
| 10 | 100  | 200   |
| 20 | 200  | 400   |
| 30 | 400  | 800   |
| 40 | 800  | 1,600 |
| 50 | 1,600 | 3,200 |

---

## Full Protocol Reference

See [PROTOCOL.md](PROTOCOL.md) for the complete schema of every message the server sends and expects.
