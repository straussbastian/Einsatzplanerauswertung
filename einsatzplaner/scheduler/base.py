from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from ..geo import RouteProvider
from ..models import Auftrag, Stop, Techniker, Tourenplan


@dataclass
class TechSnapshot:
    """Zustand eines Technikers zu einem konkreten Zeitpunkt — Eingabe für Replan.

    Enthält alle Informationen, die der Scheduler braucht, um den Rest des Tages
    für diesen Techniker ab `next_free` weiterzuplanen, ohne den schon gefahrenen
    Teil zu wiederholen oder Pausen doppelt zu vergeben.
    """
    tech_id: str
    next_free: datetime  # ab diesem Zeitpunkt kann der Techniker wieder bewegt werden
    pos_lat: float
    pos_lon: float
    fruehstueck_done: bool = False
    mittag_done: bool = False
    arbeit_seit_letzter_pause_min: int = 0
    bereits_erledigte_stops: list[Stop] = field(default_factory=list)


@dataclass
class ReplanKontext:
    """Tagesverlauf-Statistiken, die dem Scheduler beim Replan zusätzlich zur
    Verfügung stehen.

    Das ist der strukturelle Unterschied zwischen „Scheduler wird nochmal
    aufgerufen" und „Scheduler kann tagesabhängig anders reagieren". Nur der
    Hybrid nutzt diese Felder aktuell — der statische Solver hat per Definition
    die gleichen Gewichte unabhängig davon, ob er am Tag zum 1. oder 4. Mal
    angerufen wurde. Genau diese Asymmetrie misst der Replan-Benchmark.
    """
    replanungen_heute_bisher: int = 0  # 0 = dies ist der erste Replan des Tages
    trigger_event_typ: str | None = None  # "techniker_krank", "notfall", "stau", "auftrag_verlaengert"
    bisher_erledigt_heute: int = 0  # Anzahl abgeschlossener Auftrag-Stops vor Replan-Zeit
    pending_pro_tech_min: dict[str, int] = field(default_factory=dict)
    rest_schicht_pro_tech_min: dict[str, int] = field(default_factory=dict)


@dataclass
class PlanInput:
    datum: date
    techniker: list[Techniker]
    auftraege: list[Auftrag]
    route_provider: RouteProvider
    # Replan-Modus: Felder optional, nur gesetzt wenn das ein Re-Plan zu einem
    # späteren Zeitpunkt am Tag ist (z.B. nach Krankmeldung oder Notfall).
    replan_ab: datetime | None = None
    tech_anfangszustand: dict[str, TechSnapshot] | None = None
    ausgeschlossene_techs: set[str] = field(default_factory=set)
    replan_kontext: ReplanKontext | None = None

    @property
    def ist_replan(self) -> bool:
        return self.replan_ab is not None


class Scheduler(Protocol):
    name: str

    def plan(self, pin: PlanInput) -> Tourenplan: ...
