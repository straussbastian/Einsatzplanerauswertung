from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from typing import Literal


BETRIEBSHOF_LAT = 53.146661
BETRIEBSHOF_LON = 8.180577
BETRIEBSHOF_NAME = "Betriebshof Oldenburg"


class Dringlichkeit(int, Enum):
    NIEDRIG = 1
    MITTEL = 2
    HOCH = 3


QUAL_KAELTESCHEIN = "kaelteschein"


@dataclass
class Auftrag:
    id: str
    kunde: str
    typ: Literal["privat", "gewerblich"]
    adresse: str
    lat: float
    lon: float
    dauer_min: int
    dringlichkeit: Dringlichkeit
    terminart: Literal["flexibel", "fix"]
    fenster_von: time | None = None
    fenster_bis: time | None = None
    sla_frist: date | None = None
    notfall: bool = False
    rollover_count: int = 0
    benoetigt_qualifikationen: frozenset[str] = field(default_factory=frozenset)
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def hat_fenster(self) -> bool:
        return self.fenster_von is not None and self.fenster_bis is not None


@dataclass
class Techniker:
    id: str
    name: str
    home_lat: float = BETRIEBSHOF_LAT
    home_lon: float = BETRIEBSHOF_LON
    schichtbeginn: time = time(8, 0)
    schichtende: time = time(17, 0)  # 8h Netto-Arbeit + 60 min Pausen = 9h Brutto
    pause_fruehstueck_min: int = 15
    pause_mittag_min: int = 45
    max_arbeit_ohne_pause_min: int = 360
    mittag_fenster_von: time = time(11, 30)
    mittag_fenster_bis: time = time(13, 30)
    qualifikationen: frozenset[str] = field(default_factory=frozenset)

    def kann_uebernehmen(self, auftrag: Auftrag) -> bool:
        """True wenn der Techniker alle vom Auftrag geforderten Qualifikationen besitzt."""
        return auftrag.benoetigt_qualifikationen.issubset(self.qualifikationen)


class StopTyp(str, Enum):
    DEPOT_START = "depot_start"
    DEPOT_ENDE = "depot_ende"
    AUFTRAG = "auftrag"
    PAUSE_FRUEHSTUECK = "pause_fruehstueck"
    PAUSE_MITTAG = "pause_mittag"


@dataclass
class Stop:
    typ: StopTyp
    start: datetime
    ende: datetime
    auftrag_id: str | None = None
    fahrzeit_min: int = 0
    lat: float | None = None
    lon: float | None = None
    status: Literal["geplant", "erledigt", "storniert", "nicht_ausgefuehrt"] = "geplant"

    @property
    def dauer_min(self) -> int:
        return int((self.ende - self.start).total_seconds() // 60)


@dataclass
class Tour:
    techniker_id: str
    datum: date
    stops: list[Stop] = field(default_factory=list)

    @property
    def auftrag_ids(self) -> list[str]:
        return [s.auftrag_id for s in self.stops if s.typ == StopTyp.AUFTRAG and s.auftrag_id]

    @property
    def gesamt_fahrzeit_min(self) -> int:
        return sum(s.fahrzeit_min for s in self.stops)

    @property
    def gesamt_arbeitszeit_min(self) -> int:
        return sum(s.dauer_min for s in self.stops if s.typ == StopTyp.AUFTRAG)


@dataclass
class Tourenplan:
    datum: date
    touren: dict[str, Tour] = field(default_factory=dict)
    nicht_zugewiesen: list[str] = field(default_factory=list)

    def alle_zugewiesenen_auftrag_ids(self) -> list[str]:
        return [aid for tour in self.touren.values() for aid in tour.auftrag_ids]


class EventTyp(str, Enum):
    TECHNIKER_KRANK = "techniker_krank"
    KUNDE_ABSAGE = "kunde_absage"
    NOTFALL = "notfall"
    STAU = "stau"
    AUFTRAG_VERLAENGERT = "auftrag_verlaengert"


@dataclass
class Stoerung:
    typ: EventTyp
    zeitpunkt: datetime
    techniker_id: str | None = None
    auftrag_id: str | None = None
    notfall_auftrag: Auftrag | None = None
    stau_dauer_min: int = 0
    extra_min: int = 0
    betroffene_techniker: list[str] = field(default_factory=list)
    dauer_tage: int = 1  # für TECHNIKER_KRANK: Krankheit erstreckt sich über N Tage
