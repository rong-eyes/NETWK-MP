import queue
import random
import socket
import threading
import time
from functools import partial
from typing import Callable, NamedTuple, Optional

import verbose
from channel import Channel
from protocol import (DIRECTED_TYPES, LOBBY_PORT, LOBBY_TYPES, UNRELIABLE_TYPES, MalformedPacket, MsgType,
                      Packet, PeerInfo, Timing, decode, encode)


def local_ip() -> str:
    for target in ("8.8.8.8", "10.255.255.255"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((target, 9))
            return s.getsockname()[0]
        except OSError:
            continue
        finally:
            s.close()
    return "127.0.0.1"


def open_lobby_socket(port: int = LOBBY_PORT) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", port))
    s.settimeout(0.05)
    return s

def open_game_socket(host: str = "0.0.0.0", port: int = 0) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.bind((host, port))
    s.settimeout(0.2)
    return s


class Incoming(NamedTuple):
    peer_id: int
    msg_type: MsgType
    payload: dict
    packet: Packet


class Node:
    def __init__(self, sock: socket.socket, session_id: bytes, my_id: int, manifest: dict,
                 timing: Optional[Timing] = None, drop_rate: float = 0.0,
                 rng: Optional[random.Random] = None,
                 on_channel_failed: Optional[Callable[[int], None]] = None,
                 on_malformed: Optional[Callable[[tuple, str], None]] = None):
        self.sock, self.session_id, self.my_id = sock, session_id, my_id
        self.manifest: dict = dict(manifest)                
        self.t = timing or Timing()
        self.drop_rate, self._rng = drop_rate, rng or random.Random()   
        self.on_channel_failed, self.on_malformed = on_channel_failed, on_malformed
        self.peer_ids = sorted(p for p in self.manifest if p != my_id)
        self.channels = {pid: Channel(pid, my_id, session_id, partial(self._transmit, pid), self.t,
                                      on_failed=self._channel_failed) for pid in self.peer_ids}
        self.live = set(self.peer_ids)                       
        self.failed_channels: set = set()                    
        self.inbox: "queue.Queue[Incoming]" = queue.Queue()  
        self._cond = threading.Condition()
        self._hello_acked: set = set()
        self._hello_sent: set = set()
        self._running = False
        self._threads: list = []

    def start(self) -> None:
        self._running = True
        for fn in (self._recv_loop, self._timer_loop):
            th = threading.Thread(target=fn, daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self) -> None:
        self._running = False
        for th in self._threads:
            th.join(timeout=1)
        try:
            self.sock.close()
        except OSError:
            pass

    def send(self, peer_id: int, msg_type: MsgType, payload: dict) -> bool:
        ch = self.channels[peer_id]
        if msg_type in UNRELIABLE_TYPES:
            return ch.send_unreliable(msg_type, payload)
        return ch.send(msg_type, payload)

    def broadcast(self, msg_type: MsgType, payload: dict) -> list:
        if msg_type in DIRECTED_TYPES or msg_type in LOBBY_TYPES:
            raise ValueError(f"{msg_type.name} is not a game broadcast message")
        return [pid for pid in sorted(self.live) if self.send(pid, msg_type, payload)]

    def ping(self, peer_id: int) -> bool:
        return self.send(peer_id, MsgType.PING, {"sent_at_ms": time.monotonic_ns() // 1_000_000})

    def mark_disconnected(self, peer_id: int) -> None:
        self.live.discard(peer_id)

    def recv(self, timeout: Optional[float] = None) -> Optional[Incoming]:
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def _transmit(self, peer_id: int, pkt: Packet) -> None:
        info: PeerInfo = self.manifest[peer_id]
        verbose.packet("SEND", pkt, f"peer {peer_id}")
        try:
            self.sock.sendto(encode(pkt), (info.host, info.port))
        except OSError as exc:      
            verbose.info(f"sendto peer {peer_id} failed: {exc}")

    def _channel_failed(self, peer_id: int) -> None:
        self.failed_channels.add(peer_id)
        verbose.info(f"channel to peer {peer_id} FAILED after {self.t.retry_max} timeouts")
        if self.on_channel_failed:
            self.on_channel_failed(peer_id)
