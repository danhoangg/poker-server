# AlgoPoker — Bot Protocol Reference

This document describes everything a bot needs to connect to the server, play a tournament, and interpret the results.

---

## Connection

Connect via WebSocket to:

```
ws://localhost:8765
```

The server accepts **2–9 players**. Once the minimum (2) is reached, the tournament starts after a 5-second lobby window. Connecting after the tournament has started results in an error and the connection is closed.

---

## Message Format

All messages are JSON objects sent as text frames over the WebSocket. Every message has a `"type"` field that identifies the message.

---

## Bot → Server Messages

### `join`

**Send this immediately after connecting.** It is the only message the bot sends before the tournament begins.

```json
{
  "type": "join",
  "name": "MyBot"
}
```

| Field  | Type   | Rules                              |
|--------|--------|------------------------------------|
| `name` | string | 1–32 characters, must be unique    |

The server responds with a `waiting` message (and an `error` + close if registration fails).

---

### `action`

**Send only when you receive an `action_request` with your seat as `actor_seat`.** Sending at any other time is silently ignored.

```json
{
  "type": "action",
  "action": {
    "type": "fold"
  }
}
```

```json
{
  "type": "action",
  "action": {
    "type": "check"
  }
}
```

```json
{
  "type": "action",
  "action": {
    "type": "call"
  }
}
```

```json
{
  "type": "action",
  "action": {
    "type": "raise",
    "amount": 400
  }
}
```

| `action.type` | `action.amount` | When available               |
|---------------|-----------------|------------------------------|
| `"fold"`      | omit            | always                       |
| `"check"`     | omit            | when no bet to call          |
| `"call"`      | omit            | when there is a bet to call  |
| `"raise"`     | required (int)  | when raising is legal        |

For `raise`, `amount` is the **total bet size** (not the raise increment). It is clamped server-side to `[min_amount, max_amount]` from `valid_actions`, so sending a slightly off value won't be rejected.

You have **30 seconds** to respond. Exceeding the timeout results in an automatic fold.

---

## Server → Bot Messages

### `waiting`

Sent to all connected bots after each new player joins. Shows the current lobby state.

```json
{
  "type": "waiting",
  "current_players": 2,
  "min_players": 2,
  "max_players": 9
}
```

---

### `game_start`

Broadcast once to all players when the tournament begins.

```json
{
  "type": "game_start",
  "player_names": ["Alice", "Bob", "Charlie"],
  "starting_stacks": [10000, 10000, 10000],
  "small_blind": 50,
  "big_blind": 100
}
```

`player_names[i]` has permanent **seat index `i`** for the entire tournament.

---

### `hand_start`

Broadcast at the beginning of each hand.

```json
{
  "type": "hand_start",
  "hand_number": 7,
  "dealer_seat": 2,
  "small_blind_seat": 0,
  "big_blind_seat": 1,
  "small_blind_amount": 100,
  "big_blind_amount": 200,
  "player_names": ["Alice", "Bob", "Charlie"],
  "stacks": [8500, 11200, 10300],
  "hole_cards": ["Ah", "Kd"]
}
```

`player_names` and `stacks` contain only active (non-eliminated) players in seat order. `hole_cards` contains the receiving bot's own two cards — each bot receives a different version of this message. Cards are dealt and revealed at the start of the hand, before any action is requested.

---

### `action_request`

Sent to **all** active players whenever any player must act. Each bot receives a version personalised to their perspective (their own hole cards are visible; others are hidden).

```json
{
  "type": "action_request",
  "actor_seat": 1,
  "timeout_seconds": 30,
  "game_state": { ... }
}
```

**Only the player with `seat == actor_seat` should reply.** All other bots use this to update their internal game model.

See [Game State Object](#game-state-object) below for the full `game_state` schema.

---

### `action_result`

Broadcast to all players after every action.

```json
{
  "type": "action_result",
  "actor_seat": 1,
  "player_name": "Bob",
  "action": {
    "type": "raise",
    "amount": 400
  },
  "timed_out": false,
  "game_state": { ... }
}
```

`timed_out: true` means the player did not respond in time and was auto-folded.

---

### `hand_end`

Broadcast when the hand is complete (after the river or when all but one player folds).

```json
{
  "type": "hand_end",
  "hand_number": 7,
  "winners": [
    { "seat": 2, "name": "Charlie", "amount_won": 600 }
  ],
  "hole_cards_revealed": [
    { "seat": 1, "name": "Bob",     "hole_cards": ["Kd", "Ks"] },
    { "seat": 2, "name": "Charlie", "hole_cards": ["Ac", "Ad"] }
  ],
  "final_stacks": [8500, 10600, 10900],
  "player_names": ["Alice", "Bob", "Charlie"],
  "eliminated_seats": []
}
```

- `winners` — each entry is a player who won chips, with how much (`amount_won` = net gain, not total pot).
- `hole_cards_revealed` — cards are revealed for players who went to showdown. Folded players' cards remain hidden.
- `final_stacks` — chip counts for all registered players (index = seat). Eliminated players have `0`.
- `eliminated_seats` — seat indices of players who busted out this hand.

---

### `game_end`

Broadcast when only one player remains.

```json
{
  "type": "game_end",
  "winner": "Charlie",
  "winner_seat": 2,
  "final_stacks": [0, 0, 30000],
  "player_names": ["Alice", "Bob", "Charlie"],
  "total_hands": 42
}
```

---

### `error`

Sent when something goes wrong. The connection is closed after registration errors; it stays open for in-game errors.

```json
{
  "type": "error",
  "code": "BAD_NAME",
  "message": "Name 'Alice' is already taken."
}
```

| Code                  | Cause                                            | Connection |
|-----------------------|--------------------------------------------------|------------|
| `BAD_JOIN`            | Missing or malformed join message                | Closed     |
| `BAD_NAME`            | Name empty, >32 chars, or duplicate              | Closed     |
| `TOURNAMENT_FULL`     | Table already at max players (9)                 | Closed     |
| `TOURNAMENT_STARTED`  | Tournament already in progress                   | Closed     |
| `BAD_JSON`            | Message is not valid JSON                        | Open       |
| `UNKNOWN_TYPE`        | Unrecognised `"type"` field                      | Open       |
| `BAD_ACTION`          | Action type not in `valid_actions`, or `raise` missing/non-integer `amount` | Open — auto-fold applied |

---

## Game State Object

Embedded in `action_request` and `action_result`. Contains the full observable state of the hand.

```json
{
  "street": "flop",
  "hand_number": 7,
  "community_cards": ["Jc", "3d", "5c"],

  "pot": {
    "total": 600,
    "pots": [
      { "amount": 600, "eligible_seats": [0, 1, 2] }
    ]
  },

  "players": [
    {
      "seat": 0,
      "name": "Alice",
      "stack": 9700,
      "current_bet": 0,
      "is_active": true,
      "is_all_in": false,
      "is_dealer": false,
      "is_small_blind": true,
      "is_big_blind": false,
      "hole_cards": ["As", "Kd"],
      "hole_cards_known": true
    },
    {
      "seat": 1,
      "name": "Bob",
      "stack": 9800,
      "current_bet": 0,
      "is_active": true,
      "is_all_in": false,
      "is_dealer": false,
      "is_small_blind": false,
      "is_big_blind": true,
      "hole_cards": ["??", "??"],
      "hole_cards_known": false
    },
    {
      "seat": 2,
      "name": "Charlie",
      "stack": 10000,
      "current_bet": 0,
      "is_active": true,
      "is_all_in": false,
      "is_dealer": true,
      "is_small_blind": false,
      "is_big_blind": false,
      "hole_cards": ["??", "??"],
      "hole_cards_known": false
    }
  ],

  "actor_seat": 0,

  "valid_actions": [
    { "type": "fold" },
    { "type": "check" },
    { "type": "raise", "min_amount": 100, "max_amount": 9700 }
  ],

  "dealer_seat": 2,
  "small_blind_seat": 0,
  "big_blind_seat": 1,
  "small_blind_amount": 50,
  "big_blind_amount": 100
}
```

### Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `street` | `"preflop"` \| `"flop"` \| `"turn"` \| `"river"` | Current betting round |
| `hand_number` | int | Monotonically increasing across the tournament |
| `community_cards` | string[] | Dealt board cards e.g. `["Jc","3d","5c"]`. Empty on preflop. |
| `pot.total` | int | Total chips in play (pot + all outstanding bets) |
| `pot.pots` | object[] | Main pot and any side pots; each lists eligible winners |
| `players[i].seat` | int | Permanent seat index (stable for entire tournament) |
| `players[i].stack` | int | Chips not yet wagered this street |
| `players[i].current_bet` | int | Chips wagered in the current street (not yet in pot) |
| `players[i].is_active` | bool | `false` if folded this hand |
| `players[i].is_all_in` | bool | `true` if stack is 0 and still in the hand |
| `players[i].is_dealer` | bool | Dealer button |
| `players[i].is_small_blind` | bool | Posted the small blind this hand |
| `players[i].is_big_blind` | bool | Posted the big blind this hand |
| `players[i].hole_cards` | string[] | Your own cards e.g. `["As","Kd"]`; `["??","??"]` for opponents |
| `players[i].hole_cards_known` | bool | `true` only for the receiving player |
| `actor_seat` | int | Seat that must act now |
| `valid_actions` | object[] | All legal actions for the actor (see below) |
| `dealer_seat` | int | Current dealer button position |
| `small_blind_seat` | int | Who posted the small blind |
| `big_blind_seat` | int | Who posted the big blind |
| `small_blind_amount` | int | Current SB size |
| `big_blind_amount` | int | Current BB size |

### `valid_actions` Entries

| Type | Extra fields | Meaning |
|------|-------------|---------|
| `"fold"` | — | Muck your hand |
| `"check"` | — | Pass (only when no bet to call) |
| `"call"` | `"amount"` | Put in `amount` chips to match the current bet |
| `"raise"` | `"min_amount"`, `"max_amount"` | Bet/raise to any total between min and max |

Only actions present in `valid_actions` are legal. The array is always from the actor's perspective; non-actors can use the same structure to understand what the actor is permitted to do.

---

## Card Notation

Cards are represented as a two-character string: **rank** followed by **suit**.

| Ranks | `2 3 4 5 6 7 8 9 T J Q K A` |
|-------|------------------------------|
| Suits | `c` (clubs) `d` (diamonds) `h` (hearts) `s` (spades) |

Examples: `"Ah"` = Ace of Hearts, `"Td"` = Ten of Diamonds, `"2c"` = Two of Clubs.

Opponent hole cards are always `"??"`.

---

## Blind Schedule

Blinds increase automatically based on hand number.

| Starting hand | Small blind | Big blind |
|:---:|---:|---:|
| 1  | 50   | 100  |
| 10 | 100  | 200  |
| 20 | 200  | 400  |
| 30 | 400  | 800  |
| 40 | 800  | 1,600 |
| 50 | 1,600 | 3,200 |

Starting stack: **10,000 chips**.

---

## Typical Message Sequence

```
Bot connects
Bot  →  Server    {"type":"join","name":"MyBot"}
Server → Bot      {"type":"waiting","current_players":1,...}

[other bots join]

Server → All      {"type":"game_start",...}

--- Hand loop ---

Server → All      {"type":"hand_start","hand_number":1,...}

Server → All      {"type":"action_request","actor_seat":0,"game_state":{...}}
Bot(0) → Server   {"type":"action","action":{"type":"call"}}
Server → All      {"type":"action_result","actor_seat":0,...}

Server → All      {"type":"action_request","actor_seat":1,"game_state":{...}}
Bot(1) → Server   {"type":"action","action":{"type":"raise","amount":300}}
Server → All      {"type":"action_result","actor_seat":1,...}

[... more actions ...]

Server → All      {"type":"hand_end","hand_number":1,...}

--- next hand ---

[... hands repeat until one player remains ...]

Server → All      {"type":"game_end","winner":"MyBot",...}
```

---

## Minimal Bot Template (Python)

```python
import asyncio, json, random
import websockets

async def run():
    async with websockets.connect("ws://localhost:8765") as ws:
        await ws.send(json.dumps({"type": "join", "name": "MyBot"}))

        my_seat = None

        async for raw in ws:
            msg = json.loads(raw)

            if msg["type"] == "game_start":
                my_seat = msg["player_names"].index("MyBot")

            elif msg["type"] == "action_request":
                if msg["actor_seat"] != my_seat:
                    continue   # not our turn

                gs = msg["game_state"]
                action = pick_action(gs["valid_actions"])
                await ws.send(json.dumps({"type": "action", "action": action}))

            elif msg["type"] == "game_end":
                break

def pick_action(valid_actions):
    choice = random.choice(valid_actions)
    if choice["type"] == "raise":
        return {"type": "raise", "amount": choice["min_amount"]}
    return {"type": choice["type"]}

asyncio.run(run())
```

See [example_bot.py](example_bot.py) for a complete, annotated version.
