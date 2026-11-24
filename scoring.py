import json
import time

CATEGORIES = ("military", "treasury", "wonder", "civilian", "scientific", "commercial", "guilds")
VICTORY_TOKEN = {1: 1, 2: 3, 3: 5}    
DEFEAT_TOKEN = -1

@dataclass(frozen=True)
class CardInfo:
    name: str
    color: str                      
    shields: int = 0               
    vp: int = 0                    
    science: Optional[str] = None 

@dataclass(frozen=True)
class BoardStage:
    vp: int = 0
    shields: int = 0
    wild_science: int = 0

@dataclass
class SeatView:
    seat: int
    built: list                     
    stages: list                    
    stage_ages: list               
    coins: int
    military_tokens: list = field(default_factory=list)   

CardDB = dict  

def load_card_db(path: str) -> CardDB:
    with open(path, encoding="utf-8") as fh:
        return {c["name"]: CardInfo(**c) for c in json.load(fh)}

def neighbours(ring: list, seat: int) -> tuple:
    i, k = ring.index(seat), len(ring)
    return ring[(i - 1) % k], ring[(i + 1) % k]

def shield_total(view: SeatView, db: CardDB, upto_age: int = 3) -> int:
    cards = sum(db[c].shields for c, age in view.built if age <= upto_age)
    stages = sum(view.stages[i].shields for i, age in enumerate(view.stage_ages) if age <= upto_age)
    return cards + stages

dataclass
class AgeResult:
    shields: int
    vs_left: int     
    vs_right: int

    @property
    def tokens(self) -> list:
        return [self.vs_left, self.vs_right]


def _duel(mine: int, theirs: int, age: int) -> int:
    return VICTORY_TOKEN[age] if mine > theirs else DEFEAT_TOKEN if mine < theirs else 0


def resolve_age(age: int, views: dict, db: CardDB, ring: list) -> dict:
    shields = {s: shield_total(views[s], db, age) for s in ring}
    out = {}
    for s in ring:
        left, right = neighbours(ring, s)
        out[s] = AgeResult(shields[s], _duel(shields[s], shields[left], age),
                           _duel(shields[s], shields[right], age))
    return out


def apply_age_end(age: int, views: dict, db: CardDB, ring: list) -> dict:
    res = resolve_age(age, views, db, ring)
    for s, r in res.items():
        views[s].military_tokens.extend(r.tokens)
    return res


@dataclass
class Violation:
    code: str                
    offending_seat: int
    detail: str


class AgeEndTracker:
    def __init__(self, age: int, views: dict, db: CardDB, live_seats: list):
        self.age, self.live = age, set(live_seats)
        self.expected = {s: shield_total(views[s], db, age) for s in live_seats}
        self.reported: dict = {}

    def my_payload(self, my_seat: int) -> dict:
        return {"age": self.age, "shields": self.expected[my_seat]}

    def on_age_end(self, seat: int, payload: dict) -> Optional[Violation]:
        shields = payload.get("shields")
        if payload.get("age") != self.age or not isinstance(shields, int) or isinstance(shields, bool):
            return Violation("MALFORMED_MESSAGE", seat, f"bad AGE_END payload for age {self.age}: {payload}")
        self.reported[seat] = shields
        if seat in self.expected and shields != self.expected[seat]:
            return Violation("AGE_MISMATCH", seat,
                             f"seat {seat} reported {shields} shields, we computed {self.expected[seat]}")
        return None
    
    def complete(self) -> bool:
        return self.live <= set(self.reported)

@dataclass(frozen=True)
class ScoreBreakdown:
    military: int = 0
    treasury: int = 0
    wonder: int = 0
    civilian: int = 0
    scientific: int = 0
    commercial: int = 0
    guilds: int = 0

    @property
    def total(self) -> int:
        return sum(getattr(self, c) for c in CATEGORIES)

    def to_payload(self) -> dict:
        """The SCORE_REPORT payload (Section 8.3)."""
        d = {c: getattr(self, c) for c in CATEGORIES}
        d["total"] = self.total
        return d

    @staticmethod
    def from_payload(p: dict) -> "ScoreBreakdown":
        vals = {}
        for c in CATEGORIES:
            v = p.get(c)
            if not isinstance(v, int) or isinstance(v, bool):
                raise ValueError(f"SCORE_REPORT field '{c}' missing or not an integer")
            vals[c] = v
        return ScoreBreakdown(**vals)

    def diff(self, other: "ScoreBreakdown") -> dict:
        return {c: (getattr(self, c), getattr(other, c)) for c in CATEGORIES if getattr(self, c) != getattr(other, c)}
