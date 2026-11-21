from enum import StrEnum

"""
This file contains all the constants and enums ineeded in the project
Time here is relresented in seconds
"""

T_ACK_DELAY = 0.05 #50MS 
RTO_INIT = 0.2
RTO_MAX = 3.2
RETRY_MAX = 6 #not a time
T_JOIN_TIMEOUT = 10
T_SEED_TIMEOUT = 10
T_TURN_TIMEOUT = 8
T_RESOURCE_TIMEOUT = 5
T_SCORE_TIMEOUT = 10
T_LOBBY_ANNOUNCE_INTERVAL = 2
T_LOBBY_STALE = 6
T_LOBBY_QUIET = 3
T_LOBBY_TIMEOUT = 120  

#ENUMERATIONS: made easier for JSON validation

class ErrorCode(StrEnum):
    COMMITMENT_MISMATCH = "COMMITMENT_MISMATCH"
    RING_MISMATCH = "RING_MISMATCH"
    AGE_MISMATCH = "AGE_MISMATCH"
    SCORE_MISMATCH = "SCORE_MISMATCH"
    MALFORMED_MESSAGE = "MALFORMED_MESSAGE"
    OUT_OF_PHASE = "OUT_OF_PHASE"
    TIMEOUT = "TIMEOUT"

class DisconnectReason(StrEnum):
    USER_QUIT = "USER_QUIT"
    QUORUM_LOST = "QUORUM_LOST"
    PROTOCOL_VIOLATION = "PROTOCOL_VIOLATION"
    TIMEOUT = "TIMEOUT"

# CARDS
class Action(StrEnum):
    BUILD = "BUILD"
    WONDER_STAGE = "WONDER_STAGE"
    DISCARD = "DISCARD"

class Resource(StrEnum):
    WOOD = "WOOD"
    ORE = "ORE"
    CLAY = "CLAY"
    STONE = "STONE"
    GLASS = "GLASS"
    PAPYRUS = "PAPYRUS"
    TEXTILE = "TEXTILE"

class ScienceSymbol(StrEnum):
    TABLET = "TABLET"
    COMPASS = "COMPASS"
    GEAR = "GEAR"

class CardColor(StrEnum):
    BROWN = "BROWN"
    GREY = "GREY"
    BLUE = "BLUE"
    YELLOW = "YELLOW"
    GREEN = "GREEN"
    RED = "RED"
    PURPLE = "PURPLE"

# Helper sets for game rules (since Resource is now unified)
RAW_MATERIALS = frozenset({
    Resource.WOOD, Resource.ORE, Resource.CLAY, Resource.STONE
})
MANUFACTURED_GOODS = frozenset({
    Resource.GLASS, Resource.PAPYRUS, Resource.TEXTILE
})

# Hex string length constants (for validating incoming JSON payloads)
INCIDENT_ID_HEX_LEN = 16     # 64-bit
LOBBY_ID_HEX_LEN = 32        # 128-bit
LOBBY_PEER_ID_HEX_LEN = 32   # 128-bit
SHA256_HEX_LEN = 64          # 256-bit
