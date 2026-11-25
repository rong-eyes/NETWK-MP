import enum # IntEnum is used below. This allows message types be used directly as integers in struct.pack and in comparisons
import hashlib # only used by lobby_id_from_room SHA-256 per RFC 7.2
import json # UTF-8 JSON required according to RFC section 5
import struct # structs will be used for the headers
from dataclasses import dataclass, field # dataclasses provide a shortcut for classes. In CCPROG3 terms, it creates automatic constructors for classes
from typing import Optional # used later for Packet.payload (none means no payload bytes) !

VERSION = 1 # RFC 5: version must be 1; decode() rejects anything else rather than guessing at a future format
LOBBY_PORT = 7777 # RFC 7.1 default; used only for lobby broadcast, never for the game channels
LOBBY_SENTINEL = 0xFFFF  # Sender Peer ID during the lobby (no manifest yet); can't collide with real IDs since 0 is reserved and manifest IDs are 1 to 7
MAX_DATAGRAM = 1200 # RFC 5 cap: stays under a 1500-byte Maximum Transmission Unit minus IP/UDP headers, avoiding IP fragmentation (losing one fragment loses the whole datagram)
MIN_PLAYERS = 3 # based on RFCC 1.3 minimum players are 3, lobby will not vote if less than 3
MAX_PLAYERS = 7 # RFC 1.3

HEADER = struct.Struct("!BBBB16sHHII")  # format for the 32-byte header; "!" ensures that there is a strict ordering scheme followed and no padding allowed (strictly 32 bytes); BBBB = ver/type/flags/reserved, 16s = session id, H = sender, H = payload len, I = seq, I = ack; precompiled once at import
HEADER_SIZE = HEADER.size # 4+16+2+2+4+4 = 32 bits
assert HEADER_SIZE == 32 # guard against someone editing the format string

FLAG_ACK = 0x01 # Acknowledgement indicator; RFC says "bit 0" without saying which end of the byte
FLAG_RETRANSMIT = 0x02 # Retransmission indicator; bit 1; set on resends so a receiver can tell duplicates from first transmissions

TYPE_BARE_ACK = 0x00 # indicators for bare acknowledgements; Not indicated in the RFC, so we give it 00

class MsgType(enum.IntEnum): # message catalog in RFC section 8.1; enum.IntEnum allows us to represent each message type using a prefix like 0x05 for HELLO
    LOBBY_PRESENCE = 0x01 # broadcasting a players presence, unreliable: periodic beacon, a lost one is superseded by the next
    LOBBY_LEAVE = 0x02 # broadcast a player leaving, unreliable: best effort;
    LOBBY_START_VOTE = 0x03 # broadcast start vote, unreliable
    LOBBY_START_ACK = 0x04 # broadcast for officially starting the game;
    HELLO = 0x05  # directed handshake with another player, reliable: should be the FIRST reliable message on each channel
    HELLO_ACK = 0x06  # directed handshake confirmation, reliable: sent only after the matching HELLO arrived
    SEED_COMMIT = 0x07  # broadcast, reliable: commit half of the seed commit-reveal
    SEED_REVEAL = 0x08  # broadcast, reliable: must follow collection of ALL commits.
    RING_CONFIRM = 0x09  # broadcast, reliable: cross checking of independently derived seat/board
    CARD_COMMIT = 0x0A  # broadcast, reliable: per-turn commit
    CARD_REVEAL = 0x0B  # broadcast, reliable: per-turn reveal (card_id present only for BUILD)
    ACTION_ANNOUNCE = 0x0C  # broadcast, reliable: feeds every peer's identical Public Log
    HAND_PASS = 0x0D  # directed to the playor on left/right depending on waht age are you in, reliable: the largest payload (it contains 7 card names), still under MAX_DATAGRAM tho
    RESOURCE_QUERY = 0x0E  # directed, reliable: private trade step between two plauers
    RESOURCE_OFFER = 0x0F  # directed, reliable: seller computes the price unilaterally
    RESOURCE_BUY = 0x10  # directed, reliable: coins must equal qty * offered price
    AGE_END = 0x11  # broadcast, reliable: shield checkpoint
    SCORE_REPORT = 0x12  # broadcast, reliable: the 7-category score plus total
    GAME_END = 0x13  # broadcast, reliable: sent exactly once, after all reports agree
    ERROR = 0x14  # broadcast, reliable: so every peer logs the same incident
    PING = 0x15  # directed, unreliable: it exists to measure RTT
    PONG = 0x16  # directed, unreliable: echo of PING's timestamp
    DISCONNECT = 0x17  # broadcast, "best-effort" in the RFC table; we treat it as unreliable (seq 0)
    DIRECT_MESSAGE = 0x18  # directed, reliable: chat, no game-state meaning
    GROUP_MESSAGE = 0x19  # broadcast, reliable: chat, never in the Public Log

M = MsgType # short alias so the frozenset literals below stay readable
LOBBY_TYPES = frozenset({M.LOBBY_PRESENCE, M.LOBBY_LEAVE, M.LOBBY_START_VOTE, M.LOBBY_START_ACK}) # frozenset: immutable; used to drop lobby messages that strays onto the game socket

UNRELIABLE_TYPES = LOBBY_TYPES | {M.PING, M.PONG, M.DISCONNECT} # frozenset | set returns the LEFT operand's type (frozenset); these skip sequence numbers and retransmission

DIRECTED_TYPES = frozenset({M.HELLO, M.HELLO_ACK, M.HAND_PASS, M.RESOURCE_QUERY, M.RESOURCE_OFFER,  # the RFC 8.1 "Direction" column; anything absent here is a broadcast
                            M.RESOURCE_BUY, M.PING, M.PONG, M.DIRECT_MESSAGE}) # Node.broadcast() uses this to reject a directed type mistakenly sent to everyone

@dataclass # Spares us from writing "constructors" for initializing, representation, etc.
class Timing: 
    t_ack_delay: float = 0.050  # RFC 10: max wait before a bare ACK; bare ACK is a simple acknowledgement without any datagram inside, it is sent for the sake of saying I got your message
    rto_init: float = 0.200  # initial retransmit timeout; 
    rto_max: float = 3.2  # retransmit timeout will be doubled for each call, so the next will be after 0.400, then 1.600, and finally 3.200 seconds; If 3.2 seconds have passed and no response, then consider it a timeout
    retry_max: int = 6  # consecutive timeouts before a channel is declared failed
    window: int = 4  # RFC 6.1 sliding window; more in flight = less head-of-line blocking, but more spurious resends on loss
    t_join_timeout: float = 10.0  # HELLO collection window (7.5)
    t_seed_timeout: float = 10.0  # each of the two seed barriers (7.6)
    t_turn_timeout: float = 8.0  # CARD_COMMIT barrier; two consecutive misses disconnect (11.3)
    t_resource_timeout: float = 5.0  # RESOURCE_OFFER wait, and the PING-corroboration wait in 7.9
    t_score_timeout: float = 10.0  # SCORE_REPORT barrier (7.11)
    t_lobby_announce_interval: float = 2.0  # presence beacon period
    t_lobby_stale: float = 6.0  # = 3 intervals, so 2 consecutive lost beacons are tolerated before pruning
    t_lobby_quiet: float = 3.0  # roster must be unchanged this long before proposing a start
    t_lobby_timeout: float = 120.0  # per-peer local deadline; MUST be enforced (7.4)

class PeerInfo: # dedicated struct for a peer 
    peer_id: int  # manifest ID (1..k), assigned by sorting lobby_peer_ids ascending
    display_name: str  # carried over unchanged from LOBBY_PRESENCE
    host: str  # the ADVERTISED game-socket host (7.1): connect here, not to the lobby source address
    port: int  # advertised game-socket port 


class MalformedPacket(Exception):
    """Datagram that cannot be parsed"""

@dataclass
class Packet:
    msg_type: int
    session_id: bytes
    sender: int
    seq: int = 0
    ack: int = 0
    flags: int = 0
    payload: Optional[dict] = field(default_factory=dict)  
    version: int = VERSION

    @property
    def is_ack(self) -> bool:
        return bool(self.flags & FLAG_ACK)
    
    @property
    def is_retransmit(self) -> bool:
        return bool(self.flags & FLAG_RETRANSMIT)

def lobby_id_from_room(room_code: str) -> bytes:
    return hashlib.sha256(room_code.encode("utf-8")).digest()[:16]


def encode(pkt: Packet) -> bytes:
    body = b"" if pkt.payload is None else json.dumps(
        pkt.payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    data = HEADER.pack(pkt.version, int(pkt.msg_type), pkt.flags & 0xFF, 0, pkt.session_id,
                       pkt.sender, len(body), pkt.seq, pkt.ack) + body
    if len(data) > MAX_DATAGRAM:
        raise ValueError(f"datagram is {len(data)} bytes, limit is {MAX_DATAGRAM}")
    return data

def decode(data: bytes) -> Packet:
    if len(data) < HEADER_SIZE:
        raise MalformedPacket(f"datagram too short ({len(data)} bytes)")
    ver, mtype, flags, _reserved, sid, sender, plen, seq, ack = HEADER.unpack_from(data)
    if ver != VERSION:
        raise MalformedPacket(f"unsupported version {ver}")
    if plen != len(data) - HEADER_SIZE:
        raise MalformedPacket(f"payload length {plen} != {len(data) - HEADER_SIZE} bytes present")
    if mtype == TYPE_BARE_ACK:
        if not flags & FLAG_ACK or plen:
            raise MalformedPacket("type 0x00 must be an ACK-flagged datagram with empty payload")
        return Packet(TYPE_BARE_ACK, sid, sender, seq, ack, flags, None, ver)
    try:
        msg_type = MsgType(mtype)
    except ValueError:
        raise MalformedPacket(f"unknown message type 0x{mtype:02x}") from None
    try:
        payload = json.loads(data[HEADER_SIZE:].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedPacket(f"bad JSON payload: {exc}") from None
    if not isinstance(payload, dict):
        raise MalformedPacket("payload is not a JSON object")
    return Packet(msg_type, sid, sender, seq, ack, flags, payload, ver)





