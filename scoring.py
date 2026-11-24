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

def science_score(tablet: int, compass: int, gear: int, wild: int = 0) -> int:
    best = 0
    for a, b in product(range(wild + 1), repeat=2):
        if a + b > wild:
            continue
        t, c, g = tablet + a, compass + b, gear + (wild - a - b)
        best = max(best, t * t + c * c + g * g + 7 * min(t, c, g))
    return best


def _count(per: str, seat: int, views: dict, db: CardDB) -> int:
    v = views[seat]
    if per == "WONDER_STAGE":
        return len(v.stage_ages)
    if per == "DEFEAT":
        return sum(1 for tok in v.military_tokens if tok == DEFEAT_TOKEN)
    colours = set(per.split("+"))
    return sum(1 for c, _ in v.built if db[c].color in colours)


def _rule_vp(card: CardInfo, seat: int, views: dict, db: CardDB, ring: list) -> int:
    vp = card.vp
    if card.vp_rule:
        left, right = neighbours(ring, seat)
        where = {"own": seat, "left": left, "right": right}
        vp += card.vp_rule["vp"] * sum(_count(card.vp_rule["per"], where[w], views, db)
                                       for w in card.vp_rule["scope"])
    return vp


def score_seat(seat: int, views: dict, db: CardDB, ring: list) -> ScoreBreakdown:
    v = views[seat]
    cards = [db[c] for c, _ in v.built]
    sym = {"TABLET": 0, "COMPASS": 0, "GEAR": 0, "WILD": 0}
    for c in cards:
        if c.science:
            sym[c.science] += 1
    sym["WILD"] += sum(v.stages[i].wild_science for i in range(len(v.stage_ages)))
    return ScoreBreakdown(
        military=sum(v.military_tokens),
        treasury=max(v.coins, 0) // 3,
        wonder=sum(v.stages[i].vp for i in range(len(v.stage_ages))),
        civilian=sum(c.vp for c in cards if c.color == "BLUE"),
        scientific=science_score(sym["TABLET"], sym["COMPASS"], sym["GEAR"], sym["WILD"]),
        commercial=sum(_rule_vp(c, seat, views, db, ring) for c in cards if c.color == "YELLOW"),
        guilds=sum(_rule_vp(c, seat, views, db, ring) for c in cards if c.color == "PURPLE"),
    )

def score_all(views: dict, db: CardDB, ring: list) -> dict:
    return {s: score_seat(s, views, db, ring) for s in ring}


def build_game_end(scores: dict, coins: dict) -> dict:
    seats = sorted(scores)
    winner = min(seats, key=lambda s: (-scores[s].total, -coins[s], s))
    return {"winner_seat": winner,
            "final_scores": [{"seat": s, "total": scores[s].total, "coins": coins[s]} for s in seats]}


class ScoreConsensus:
    def __init__(self, expected: dict, coins: dict, live_seats, timeout: float,
                 clock: Callable[[], float] = time.monotonic):
        self.expected, self.coins = dict(expected), dict(coins)    
        self.live = set(live_seats)
        self.timeout, self._clock = timeout, clock
        self.started = clock()
        self.reports: dict = {}
        self.disputes: dict = {}     
        self.dropped: list = []       
        self._sent = False

    def on_report(self, seat: int, payload: dict) -> Optional[Violation]:
        if seat not in self.live:
            return None
        try:
            reported = ScoreBreakdown.from_payload(payload)
        except ValueError as exc:
            return Violation("MALFORMED_MESSAGE", seat, str(exc))
        if payload.get("total") != reported.total:
            self.disputes[seat] = {"total": (reported.total, payload.get("total"))}
            return Violation("SCORE_MISMATCH", seat,
                             f"total {payload.get('total')} != sum of categories {reported.total}")
        self.reports[seat] = reported
        delta = self.expected[seat].diff(reported)
        if delta:                     
            self.disputes[seat] = delta
            return Violation("SCORE_MISMATCH", seat, "; ".join(
                f"{c}: we computed {a}, seat reported {b}" for c, (a, b) in delta.items()))
        self.disputes.pop(seat, None)
        return None

    def expire(self) -> list:
        if self._clock() - self.started < self.timeout:
            return []
        late = sorted(self.live - set(self.reports))
        for s in late:
            if s not in self.disputes:
                self.live.discard(s)
                self.dropped.append(s)
        return [s for s in late if s not in self.disputes]

    def ready(self) -> bool:
        return not self.disputes and self.live <= set(self.reports)

    def take_game_end(self) -> Optional[dict]:
        if self._sent or not self.ready():
            return None
        self._sent = True
        return build_game_end({s: self.expected[s] for s in self.live}, {s: self.coins[s] for s in self.live})
