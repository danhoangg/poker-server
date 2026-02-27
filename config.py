HOST = "localhost"
PORT = 8765

MIN_PLAYERS = 2
MAX_PLAYERS = 9

STARTING_STACK = 10_000
SMALL_BLIND = 50
BIG_BLIND = 100
ANTE = 0

ACTION_TIMEOUT_SECONDS = 30
LOBBY_WAIT_SECONDS = 5

# Blind schedule: hand_number -> (small_blind, big_blind)
# Blinds increase at the start of the given hand number.
BLIND_SCHEDULE = {
    0:  (50,   100),
    10: (100,  200),
    20: (200,  400),
    30: (400,  800),
    40: (800,  1600),
    50: (1600, 3200),
}
