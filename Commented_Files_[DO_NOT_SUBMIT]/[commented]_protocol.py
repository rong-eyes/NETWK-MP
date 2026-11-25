import enum # IntEnum is used below. This allows message types be used directly as integers in struct.pack and in comparisons
import hashlib # only used by lobby_id_from_room SHA-256 per RFC 7.2
import json # UTF-8 JSON required according to RFC section 5
import struct # structs will be used for the headers
from dataclasses import dataclass, field # dataclasses provide a shortcut for classes. In CCPROG3 terms, it creates automatic constructors for classes
from typing import Optional # Optional is a helper to enable certain variables to equal None; used later for Packet.payload (none means no payload bytes) where certain packets are "empty" (bare ACK)

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
    host: str  # the advertised game-socket host (7.1): connect here, not to the lobby source address
    port: int  # advertised game-socket port 


class MalformedPacket(Exception):  # dedicated type so callers separate "bad remote input" (recoverable, maps to ERROR MALFORMED_MESSAGE) from genuine bugs (ValueError/TypeError)
    """Datagram that cannot be parsed""" # a docstring 

@dataclass
class Packet: # a dedicated struct for a packet sent 
    msg_type: int # plain integer, not MsgType: the bare ACK's type, 0x00, isn't an enum member
    session_id: bytes  # exactly 16 raw bytes (not hex); encode() does not validate the length
    sender: int  # 16-bit manifest peer_id, or 0xFFFF if in the lobby
    seq: int = 0  # 0 = "unreliable, outside the Automatic Repeat Request (ARQ) space"; reliable seqs start at 1, so 0 is a safe in-band marker
    ack: int = 0  # cumulative: highest seq consumed in order, 0 if none
    flags: int = 0  # ACK/RETRANSMIT bits; reserved bits MUST be zero on send, ignored on receipt
    payload: Optional[dict] = field(default_factory=dict) # this means that payload can either contain a dictionary OR nothing (bare ACK); If nothing is passed, then the default payload is a dictionary
    version: int = VERSION  # last on purpose: decode() passes `ver` as the 8th argument

    @property # this gives the Packet an "innate" ability to know if it is an ACK itself; kind of acts like a hypothalamus; you don't have to be explicitly call a function to determine if it is an ACK or not
    def is_ack(self) -> bool: # easily identifies acknowledgement flags; convenience for logs/tests
        return bool(self.flags & FLAG_ACK)  # bool() converts the masked int to True/False
    
    @property # read-only computed attribute;
    def is_retransmit(self) -> bool: # lets verbose mode and tests spot retransmissions
        return bool(self.flags & FLAG_RETRANSMIT) # purely informational; deduplication should not rely on this flag, instead it should rely on the SEQ number
def lobby_id_from_room(room_code: str) -> bytes: # RFC 7.2: lobby_id == session_id for the life of the exchange; think of this like a VIP badge maker
    return hashlib.sha256(room_code.encode("utf-8")).digest()[:16] # .digest() = converts into 32 raw bytes, [:16] keeps 128 bits or 16 bytes; low-entropy room codes are guessable (RFC 12.5), and the same code always gives the same ID (RFC 12.6)

def encode(pkt: Packet) -> bytes:  # function that literally encodes packets to be securely shipped across the network
    body = b"" if pkt.payload is None else json.dumps(  # b"" means <bytes><empty>, which happens if the packet is a bare ACK, otherwise 
        pkt.payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")  # we use commas and colons as separators to save space; ensure_ascii=False ensure that all non-ascii can travel freely;
    data = HEADER.pack(pkt.version, int(pkt.msg_type), pkt.flags & 0xFF, 0, pkt.session_id,  # int() flattens the message to ensure an integer gets "header-ed"; 0xFF ensures that unexpected flags >255 will be trimmed; add a 0 to follow what the RFC says; 
                       pkt.sender, len(body), pkt.seq, pkt.ack) + body  # add the sender, body length, SEQ, ACK, and the body itself
    if len(data) > MAX_DATAGRAM:  # check if the packet exceeds our maximum; checked after building because the JSON size is unknown until dumps() runs
        raise ValueError(f"datagram is {len(data)} bytes, limit is {MAX_DATAGRAM}")  # ValueError on purpose, unlike MalformedPacket; the RFC says split into several messages, but this code refuses instead
    return data  # bytes ready for sock.sendto

def decode(data: bytes) -> Packet:  # function for decoding data; runs on untrusted network input, so every failure must become MalformedPacket
    if len(data) < HEADER_SIZE: # The header size is the smallest packet we could ever receive, therfore receiving packet less than that is obviously a malformed packet
        raise MalformedPacket(f"datagram too short ({len(data)} bytes)")  # raise a malformed packet error
    ver, mtype, flags, _reserved, sid, sender, plen, seq, ack = HEADER.unpack_from(data)  # unpack_from reads in place with no slice copy and tolerates trailing bytes (unpack would not); `_reserved` is discarded per RFC, and unknown flag bits pass through untouched
    if ver != VERSION:  # check for an appropriate version
        raise MalformedPacket(f"unsupported version {ver}")  # reject rather than best-effort parse
    if plen != len(data) - HEADER_SIZE:  # UDP preserves datagram boundaries, so a mismatch means truncation, corruption, or trailing junk; strict equality rejects all three
        raise MalformedPacket(f"payload length {plen} != {len(data) - HEADER_SIZE} bytes present")  # the message gives both numbers for debugging
    if mtype == TYPE_BARE_ACK:  # check if the packet is a bare ACK (payload is 0); handled before MsgType() lookup, which would raise ValueError for 0
        if not flags & FLAG_ACK or plen:  # precedence: `&` binds tighter than `not`, so this is (not (flags & FLAG_ACK)) or plen; a bare ACK must carry the ACK flag and an empty payload
            raise MalformedPacket("type 0x00 must be an ACK-flagged datagram with empty payload")  # stops a type-0 datagram from being treated as a free-form message
        return Packet(TYPE_BARE_ACK, sid, sender, seq, ack, flags, None, ver)  # positional args must match field order; payload=None marks "no body"
    try:  # IntEnum(value) raises ValueError for non-members
        msg_type = MsgType(mtype)  # converts the raw byte to the enum so downstream code can use .name in logs
    except ValueError:  # unknown type byte
        raise MalformedPacket(f"unknown message type 0x{mtype:02x}") from None  # raise a MalformedPacket error; `from None` is used to respond with MalformedError instead of ValueError
    try:  # parse stage: two different failure modes (bytes -> str, str -> object)
        payload = json.loads(data[HEADER_SIZE:].decode("utf-8"))  # unpack the JSON file and place it into payload
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:  # catch any errors in decoding
        raise MalformedPacket(f"bad JSON payload: {exc}") from None  # surface the parser's message (line/column) for verbose logs; Again, "from None" is used to hide other types of error, ensuring that malformedpacket is the error raised
    if not isinstance(payload, dict):  # check if the payload is valid; valid JSON can still be a list, number, or string; every 7WD payload is an object
        raise MalformedPacket("payload is not a JSON object")  # raise a malformed packet error if payload turns out invalid
    return Packet(msg_type, sid, sender, seq, ack, flags, payload, ver)  # return the packet with all its proper attributes





