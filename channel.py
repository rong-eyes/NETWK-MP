import dataclasses
import threading
import time

from protocol import (FLAG_ACK, FLAG_RETRANSMIT, TYPE_BARE_ACK, UNRELIABLE_TYPES, MsgType, Packet,
                      Timing)

RECV_BUFFER_LIMIT = 64

@dataclass
class _Pending:
    pkt: Packet
    deadline: float

class Channel:
    def __init__(self, peer_id: int, my_id: int, session_id: bytes,
                 transmit: Callable[[Packet], None], timing: Optional[Timing] = None,
                 on_failed: Optional[Callable[[int], None]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.peer_id, self.my_id, self.session_id = peer_id, my_id, session_id
        self.t = timing or Timing()
        self._transmit, self._on_failed, self._clock = transmit, on_failed, clock
        self._lock = threading.RLock()
        # sending
        self.next_seq = 1
        self.inflight: "OrderedDict[int, _Pending]" = OrderedDict()
        self.backlog: deque = deque()          
        self.base_rto = self.t.rto_init        
        self.rto = self.t.rto_init            
        self.timeouts = 0                    
        self.failed = False
        # receiving
        self.last_delivered = 0                
        self._ooo: dict = {}                  
        self._ack_due: Optional[float] = None  
        self.srtt: Optional[float] = None
        self.last_pong: Optional[float] = None
        self.stats = {"sent": 0, "retransmits": 0, "dups_dropped": 0, "bare_acks": 0, "buffered_ooo": 0}

    def send(self, msg_type: MsgType, payload: dict) -> bool:
        """Queue a reliable message. Returns False if the channel has permanently failed."""
        if msg_type in UNRELIABLE_TYPES:
            raise ValueError(f"{msg_type.name} is not carried by the reliability layer")
        with self._lock:
            if self.failed:
                return False
            if self.backlog or len(self.inflight) >= self.t.window:
                self.backlog.append((msg_type, payload))
            else:
                self._send_now(msg_type, payload)
            return True

    def send_unreliable(self, msg_type: MsgType, payload: dict) -> bool:
        """PING / PONG / DISCONNECT: seq = 0, never retransmitted, never ACKed."""
        with self._lock:
            if self.failed:
                return False
            self._transmit(self._make(msg_type, payload, seq=0))
            return True

    def _make(self, msg_type, payload, seq, extra_flags=0) -> Packet:
        flags = extra_flags | (FLAG_ACK if self.last_delivered > 0 else 0)
        return Packet(msg_type, self.session_id, self.my_id, seq, self.last_delivered, flags, payload)

    def _send_now(self, msg_type, payload) -> None:
        seq = self.next_seq
        self.next_seq += 1
        pkt = self._make(msg_type, payload, seq)
        self.inflight[seq] = _Pending(pkt, self._clock() + self.rto)
        self._ack_due = None                   
        self.stats["sent"] += 1
        self._transmit(pkt)

    def _fill_window(self) -> None:
        while self.backlog and len(self.inflight) < self.t.window:
            self._send_now(*self.backlog.popleft())

    def on_packet(self, pkt: Packet) -> list:
        with self._lock:
            if pkt.flags & FLAG_ACK:
                self._handle_ack(pkt.ack)
            if pkt.msg_type == TYPE_BARE_ACK:
                return []
            if pkt.seq == 0:                   
                return [pkt]
            if pkt.seq <= self.last_delivered:  
                self.stats["dups_dropped"] += 1
                self._send_bare_ack()          
                return []
            if pkt.seq > self.last_delivered + RECV_BUFFER_LIMIT:
                return []                      
            if pkt.seq in self._ooo:
                self.stats["dups_dropped"] += 1
            elif pkt.seq != self.last_delivered + 1:
                self.stats["buffered_ooo"] += 1
            self._ooo[pkt.seq] = pkt
            ready = []
            while self.last_delivered + 1 in self._ooo:
                self.last_delivered += 1
                ready.append(self._ooo.pop(self.last_delivered))
            if self._ack_due is None:
                self._ack_due = self._clock() + self.t.t_ack_delay
            return ready

    def _handle_ack(self, ack: int) -> None:
        if ack >= self.next_seq:             
            return
        acked = [s for s in self.inflight if s <= ack]
        if not acked:
            return
        for s in acked:
            del self.inflight[s]
        self.timeouts = 0                      
        self.rto = self.base_rto
        self._fill_window()

    def _send_bare_ack(self) -> None:
        self._ack_due = None
        self.stats["bare_acks"] += 1
        self._transmit(Packet(TYPE_BARE_ACK, self.session_id, self.my_id, 0, self.last_delivered,
                              FLAG_ACK, None))

    

