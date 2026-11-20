from __future__ import annotations

import enum
import hashlib
import json
import struct
from dataclasses import dataclass, field
from typing import Optional

VERSION = 1
LOBBY_PORT = 7777
LOBBY_SENTINEL = 0xFFFF
MAX_DATAGRAM = 1200
MIN_PLAYERS = 3
MAX_PLAYERS = 7

HEADER = struct.Struct("!BBBB16sHHII")
HEADER_SIZE = HEADER.size
assert HEADER_SIZE == 32

FLAG_ACK = 0x01
FLAG_RETRANSMIT = 0x02

TYPE_BARE_ACK = 0x00

class MsgType(enum.IntEnum):
    LOBBY_PRESENCE = 0x01
    LOBBY_LEAVE = 0x02
    LOBBY_START_VOTE = 0x03
    LOBBY_START_ACK = 0x04
    HELLO = 0x05
    HELLO_ACK = 0x06
    SEED_COMMIT = 0x07
    SEED_REVEAL = 0x08
    RING_CONFIRM = 0x09
    CARD_COMMIT = 0x0A
    CARD_REVEAL = 0x0B
    ACTION_ANNOUNCE = 0x0C
    HAND_PASS = 0x0D
    RESOURCE_QUERY = 0x0E
    RESOURCE_OFFER = 0x0F
    RESOURCE_BUY = 0x10
    AGE_END = 0x11
    SCORE_REPORT = 0x12
    GAME_END = 0x13
    ERROR = 0x14
    PING = 0x15
    PONG = 0x16
    DISCONNECT = 0x17
    DIRECT_MESSAGE = 0x18
    GROUP_MESSAGE = 0x19

M = MsgType
LOBBY_TYPES = frozenset({M.LOBBY_PRESENCE, M.LOBBY_LEAVE, M.LOBBY_START_VOTE, M.LOBBY_START_ACK})

UNRELIABLE_TYPES = LOBBY_TYPES | {M.PING, M.PONG, M.DISCONNECT}

DIRECTED_TYPES = frozenset({M.HELLO, M.HELLO_ACK, M.HAND_PASS, M.RESOURCE_QUERY, M.RESOURCE_OFFER,
                            M.RESOURCE_BUY, M.PING, M.PONG, M.DIRECT_MESSAGE})

class Timing:
    t_ack_delay: float = 0.050
    rto_init: float = 0.200
    rto_max: float = 3.2
    retry_max: int = 6
    window: int = 4                     
    t_join_timeout: float = 10.0
    t_seed_timeout: float = 10.0
    t_turn_timeout: float = 8.0
    t_resource_timeout: float = 5.0
    t_score_timeout: float = 10.0
    t_lobby_announce_interval: float = 2.0
    t_lobby_stale: float = 6.0
    t_lobby_quiet: float = 3.0
    t_lobby_timeout: float = 120.0

class PeerInfo:
    peer_id: int
    display_name: str
    host: str
    port: int


class MalformedPacket(Exception):
    """Datagram that cannot be parsed"""

class Packet:
    msg_type: int
    session_id: bytes
    sender: int
    seq: int = 0
    ack: int = 0
    flags: int = 0
    payload: Optional[dict] = field(default_factory=dict)   # None = no payload bytes (bare ACK)
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





