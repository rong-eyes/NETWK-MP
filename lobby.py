import hashlib
import json
import random
import secrets
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import verbose
from node import open_lobby_socket
from protocol import (LOBBY_PORT, LOBBY_SENTINEL, LOBBY_TYPES, MAX_PLAYERS, MIN_PLAYERS, MalformedPacket,
                      MsgType, Packet, PeerInfo, Timing, decode, encode, lobby_id_from_room)

_HEX = set("0123456789abcdef")


def _is_hex(value, length: int) -> bool:
    return isinstance(value, str) and len(value) == length and set(value) <= _HEX


def participants_hash(ids) -> str:
    return hashlib.sha256(json.dumps(sorted(ids), separators=(",", ":")).encode("utf-8")).hexdigest()

@dataclass
class _Entry:
    display_name: str
    host: str
    port: int
    last_seen: float

@dataclass
class LobbyResult:
    session_id: bytes          
    my_peer_id: int            
    manifest: dict   

class Lobby:
    def __init__(self, room_code: str, display_name: str, game_host: str, game_port: int, *,
                 timing: Optional[Timing] = None, bcast_addr: str = "255.255.255.255",
                 lobby_port: int = LOBBY_PORT, min_players: int = MIN_PLAYERS,
                 max_players: int = MAX_PLAYERS, on_status: Optional[Callable[[str], None]] = None,
                 drop_rate: float = 0.0, rng: Optional[random.Random] = None):
        self.t = timing or Timing()
        self.lobby_id = lobby_id_from_room(room_code)
        self.my_id = secrets.token_hex(16)              
        self.min_players, self.max_players = min_players, max_players
        self.bcast = (bcast_addr, lobby_port)
        self.on_status = on_status or (lambda s: None)
        self.drop_rate, self._rng = drop_rate, rng or random.Random()
        self.sock = open_lobby_socket(lobby_port)
        now = time.monotonic()
        self.roster = {self.my_id: _Entry(display_name, game_host, game_port, now)}
        self.last_change = now                          
        self.acks: dict = {}                            
        self._proposal: Optional[tuple] = None          
        self._forced = False
        self._forced_at = 0.0
        self.forced_wait = 3 * self.t.t_lobby_quiet     
        self.ack_ttl = 4 * self.t.t_lobby_quiet
        self.linger = 2 * self.t.t_lobby_quiet
        self._frozen = False
        self._lingering = False

    def run(self, cancel: Optional[threading.Event] = None) -> Optional[LobbyResult]:
        start = time.monotonic()
        next_announce = start
        self._status(f"searching for players... (1/{self.min_players} needed)")
        try:
            while True:
                now = time.monotonic()
                if cancel is not None and cancel.is_set():
                    return self._give_up("cancelled")
                if now >= next_announce:
                    self._announce()
                    next_announce = now + self.t.t_lobby_announce_interval
                self._prune(now)
                self._recv_once()
                now = time.monotonic()
                result = self._try_close(now)
                if result is not None:
                    self._start_linger()
                    return result
                self._drive_proposals(now)
                if now - start >= self.t.t_lobby_timeout:               
                    if not self._forced:
                        if len(self.roster) < self.min_players:
                            return self._give_up("not enough players were found")
                        self._forced, self._forced_at, self._proposal = True, now, None
                        self._status("timeout reached: forcing a final vote on the current roster")
                    elif now - self._forced_at >= self.forced_wait:
                        return self._give_up("the forced vote did not reach agreement")
        except KeyboardInterrupt:
            return self._give_up("cancelled")
        finally:
            if not self._lingering:
                self.sock.close()

    def _give_up(self, reason: str) -> None:
        self._send(MsgType.LOBBY_LEAVE, {"lobby_peer_id": self.my_id})
        self._status(f"left the lobby: {reason}")
        return None

    def _send(self, msg_type: MsgType, payload: dict) -> None:
        pkt = Packet(msg_type, self.lobby_id, LOBBY_SENTINEL, 0, 0, 0, payload)
        verbose.packet("SEND", pkt, "broadcast")
        try:
            self.sock.sendto(encode(pkt), self.bcast)
        except OSError as exc:
            verbose.info(f"lobby broadcast failed: {exc}")

    def _announce(self) -> None:
        me = self.roster[self.my_id]
        me.last_seen = time.monotonic()
        self._send(MsgType.LOBBY_PRESENCE, {"lobby_peer_id": self.my_id, "display_name": me.display_name,
                                            "host": me.host, "port": me.port})

    def _send_vote(self, ids: list, h: str, now: float) -> None:
        self._proposal = (h, now)
        self.acks.setdefault(h, {})[self.my_id] = now    
        self._send(MsgType.LOBBY_START_VOTE, {"participants": ids})

    def _send_ack(self, h: str) -> None:
        self._send(MsgType.LOBBY_START_ACK, {"participants_hash": h, "lobby_peer_id": self.my_id})

    def _recv_once(self) -> None:
        try:
            data, addr = self.sock.recvfrom(2048)
        except (socket.timeout, ConnectionResetError, OSError):
            return
        if self.drop_rate and self._rng.random() < self.drop_rate:
            return
        try:
            pkt = decode(data)
        except MalformedPacket as exc:
            verbose.info(f"MALFORMED lobby datagram from {addr[0]}: {exc}")
            return
        if pkt.session_id != self.lobby_id or pkt.msg_type not in LOBBY_TYPES or pkt.sender != LOBBY_SENTINEL:
            return                                       
        verbose.packet("RECV", pkt, f"{addr[0]}")
        now, body = time.monotonic(), pkt.payload or {}
        if pkt.msg_type == MsgType.LOBBY_START_VOTE:
            self._on_vote(body, now)
        elif self._frozen:
            return                                       
        elif pkt.msg_type == MsgType.LOBBY_PRESENCE:
            self._on_presence(body, addr, now)
        elif pkt.msg_type == MsgType.LOBBY_LEAVE:
            pid = body.get("lobby_peer_id")
            if pid in self.roster and pid != self.my_id:
                del self.roster[pid]
                self._roster_changed(now)
        elif pkt.msg_type == MsgType.LOBBY_START_ACK:
            self._on_ack(body, addr, now)

    def _on_presence(self, body: dict, addr: tuple, now: float) -> None:
        pid, name, host, port = (body.get(k) for k in ("lobby_peer_id", "display_name", "host", "port"))
        if (not _is_hex(pid, 32) or pid == self.my_id or not isinstance(name, str)
                or not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536):
            return
        if not isinstance(host, str) or host in ("", "0.0.0.0"):
            host = addr[0]
        entry = self.roster.get(pid)
        if entry is None:
            self.roster[pid] = _Entry(name, host, port, now)
            self._roster_changed(now)
        else:
            entry.display_name, entry.host, entry.port, entry.last_seen = name, host, port, now

    def _on_vote(self, body: dict, now: float) -> None:
        parts = body.get("participants")
        if not isinstance(parts, list) or sorted(parts) != sorted(self.roster):
            return       
        h = participants_hash(parts)
        self.acks.setdefault(h, {})[self.my_id] = now
        self._send_ack(h)                               

    def _on_ack(self, body: dict, addr: tuple, now: float) -> None:
        h, sender = body.get("participants_hash"), body.get("lobby_peer_id")
        if not _is_hex(h, 64):
            return
        if not _is_hex(sender, 32):                      
            by_ip = [p for p, e in self.roster.items() if e.host == addr[0]]
            if len(by_ip) != 1:                          
                verbose.info(f"cannot tell who sent LOBBY_START_ACK from {addr[0]}; ignored")
                return
            sender = by_ip[0]
        self.acks.setdefault(h, {})[sender] = now

    
    def _roster_changed(self, now: float) -> None:
        self.last_change = now
        self._proposal = None                            
        names = ", ".join(e.display_name for e in self.roster.values())
        self._status(f"{len(self.roster)} player(s) in lobby: {names}")

    def _prune(self, now: float) -> None:
        stale = [p for p, e in self.roster.items() if p != self.my_id and now - e.last_seen > self.t.t_lobby_stale]
        for p in stale:
            del self.roster[p]
        if stale:
            self._roster_changed(now)

    def _drive_proposals(self, now: float) -> None:
        if not self.min_players <= len(self.roster) <= self.max_players:
            self._proposal = None
            return
        ids = sorted(self.roster)
        if self._proposal is None:
            if self._forced or now - self.last_change >= self.t.t_lobby_quiet:  
                self._send_vote(ids, participants_hash(ids), now)
        elif now - self._proposal[1] >= self.t.t_lobby_quiet:                   
            self._send_vote(ids, self._proposal[0], now)

    def _try_close(self, now: float) -> Optional[LobbyResult]:
        ids = sorted(self.roster)
        if not self.min_players <= len(ids) <= self.max_players:
            return None
        got = {p for p, ts in self.acks.get(participants_hash(ids), {}).items()
               if p == self.my_id or now - ts <= self.ack_ttl}
        if not set(ids) <= got:
            return None
        
        manifest = {i + 1: PeerInfo(i + 1, self.roster[p].display_name, self.roster[p].host, self.roster[p].port)
                    for i, p in enumerate(ids)}
        self._frozen = True
        self._status(f"lobby closed with {len(ids)} players; you are peer {ids.index(self.my_id) + 1}")
        return LobbyResult(self.lobby_id, ids.index(self.my_id) + 1, manifest)

    def _start_linger(self) -> None:
        self._lingering = True
        threading.Thread(target=self._linger, daemon=True).start()

    def _linger(self) -> None:
        end, next_announce = time.monotonic() + self.linger, 0.0
        while time.monotonic() < end:
            if time.monotonic() >= next_announce:
                self._announce()
                next_announce = time.monotonic() + self.t.t_lobby_announce_interval
            self._recv_once()
        self.sock.close()

    def _status(self, text: str) -> None:
        self.on_status(text)
