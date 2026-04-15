from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from ..geo import RouteProvider
from ..models import (
    Auftrag,
    Stop,
    StopTyp,
    Techniker,
    Tour,
    Tourenplan,
)
from .base import PlanInput, TechSnapshot


FRUEHSTUECK_START = time(10, 0)
MITTAG_START = time(12, 0)


def prio_score(auftrag: Auftrag, heute: date) -> float:
    score = 10.0 * int(auftrag.dringlichkeit)
    if auftrag.notfall:
        score += 50.0
    score += 5.0 * auftrag.rollover_count
    if auftrag.sla_frist:
        tage_bis_frist = (auftrag.sla_frist - heute).days
        score += max(0.0, 10.0 - tage_bis_frist)
    return score


@dataclass
class _TechState:
    techniker: Techniker
    datum: date
    stops: list[Stop] = field(default_factory=list)
    fruehstueck_gesetzt: bool = False
    mittag_gesetzt: bool = False
    arbeit_seit_letzter_pause_min: int = 0

    @property
    def next_free(self) -> datetime:
        if not self.stops:
            return datetime.combine(self.datum, self.techniker.schichtbeginn)
        return self.stops[-1].ende

    @property
    def next_lat(self) -> float:
        if not self.stops:
            return self.techniker.home_lat
        return self.stops[-1].lat if self.stops[-1].lat is not None else self.techniker.home_lat

    @property
    def next_lon(self) -> float:
        if not self.stops:
            return self.techniker.home_lon
        return self.stops[-1].lon if self.stops[-1].lon is not None else self.techniker.home_lon

    def schichtende_dt(self) -> datetime:
        return datetime.combine(self.datum, self.techniker.schichtende)


def _place_pausen_if_due(state: _TechState, bevor: datetime) -> None:
    fruehstueck_zeit = datetime.combine(state.datum, FRUEHSTUECK_START)
    mittag_zeit = datetime.combine(state.datum, MITTAG_START)

    if not state.fruehstueck_gesetzt and bevor >= fruehstueck_zeit:
        start = max(state.next_free, fruehstueck_zeit)
        ende = start + timedelta(minutes=state.techniker.pause_fruehstueck_min)
        state.stops.append(
            Stop(
                typ=StopTyp.PAUSE_FRUEHSTUECK,
                start=start,
                ende=ende,
                lat=state.next_lat,
                lon=state.next_lon,
            )
        )
        state.fruehstueck_gesetzt = True
        state.arbeit_seit_letzter_pause_min = 0

    if not state.mittag_gesetzt and bevor >= mittag_zeit:
        start = max(state.next_free, mittag_zeit)
        ende = start + timedelta(minutes=state.techniker.pause_mittag_min)
        state.stops.append(
            Stop(
                typ=StopTyp.PAUSE_MITTAG,
                start=start,
                ende=ende,
                lat=state.next_lat,
                lon=state.next_lon,
            )
        )
        state.mittag_gesetzt = True
        state.arbeit_seit_letzter_pause_min = 0


def _force_pause_if_needed(state: _TechState, needed_work_min: int) -> None:
    if state.arbeit_seit_letzter_pause_min + needed_work_min > state.techniker.max_arbeit_ohne_pause_min:
        if not state.mittag_gesetzt:
            start = state.next_free
            ende = start + timedelta(minutes=state.techniker.pause_mittag_min)
            state.stops.append(
                Stop(
                    typ=StopTyp.PAUSE_MITTAG,
                    start=start,
                    ende=ende,
                    lat=state.next_lat,
                    lon=state.next_lon,
                )
            )
            state.mittag_gesetzt = True
            state.arbeit_seit_letzter_pause_min = 0
        elif not state.fruehstueck_gesetzt:
            start = state.next_free
            ende = start + timedelta(minutes=state.techniker.pause_fruehstueck_min)
            state.stops.append(
                Stop(
                    typ=StopTyp.PAUSE_FRUEHSTUECK,
                    start=start,
                    ende=ende,
                    lat=state.next_lat,
                    lon=state.next_lon,
                )
            )
            state.fruehstueck_gesetzt = True
            state.arbeit_seit_letzter_pause_min = 0


def _try_assign(
    state: _TechState, auftrag: Auftrag, rp: RouteProvider
) -> tuple[int, datetime, int] | None:
    if not state.techniker.kann_uebernehmen(auftrag):
        return None
    fahrzeit = rp.travel_time_min(state.next_lat, state.next_lon, auftrag.lat, auftrag.lon)
    ankunft = state.next_free + timedelta(minutes=fahrzeit)

    fruehstueck_zeit = datetime.combine(state.datum, FRUEHSTUECK_START)
    mittag_zeit = datetime.combine(state.datum, MITTAG_START)
    pausen_vor_auftrag = 0
    if not state.fruehstueck_gesetzt and ankunft >= fruehstueck_zeit:
        pausen_vor_auftrag += state.techniker.pause_fruehstueck_min
    if not state.mittag_gesetzt and ankunft >= mittag_zeit:
        pausen_vor_auftrag += state.techniker.pause_mittag_min
    ankunft += timedelta(minutes=pausen_vor_auftrag)

    if auftrag.fenster_von:
        fenster_start = datetime.combine(state.datum, auftrag.fenster_von)
        if ankunft < fenster_start:
            ankunft = fenster_start
    if auftrag.fenster_bis:
        fenster_ende = datetime.combine(state.datum, auftrag.fenster_bis)
        if ankunft + timedelta(minutes=auftrag.dauer_min) > fenster_ende:
            return None

    ende = ankunft + timedelta(minutes=auftrag.dauer_min)

    ausstehende_pausen_nach_auftrag = 0
    if not state.fruehstueck_gesetzt and ankunft < fruehstueck_zeit:
        ausstehende_pausen_nach_auftrag += state.techniker.pause_fruehstueck_min
    if not state.mittag_gesetzt and ankunft < mittag_zeit:
        ausstehende_pausen_nach_auftrag += state.techniker.pause_mittag_min
    fahrzeit_heim = rp.travel_time_min(auftrag.lat, auftrag.lon, state.techniker.home_lat, state.techniker.home_lon)
    reserviertes_ende = ende + timedelta(minutes=ausstehende_pausen_nach_auftrag + fahrzeit_heim)

    if reserviertes_ende > state.schichtende_dt():
        return None

    idle_min = int((ankunft - state.next_free).total_seconds() // 60) - fahrzeit - pausen_vor_auftrag
    cost = fahrzeit + max(0, idle_min)
    return cost, ankunft, fahrzeit


def _commit(state: _TechState, auftrag: Auftrag, ankunft: datetime, fahrzeit: int) -> None:
    _place_pausen_if_due(state, ankunft)
    _force_pause_if_needed(state, auftrag.dauer_min)

    if ankunft < state.next_free:
        ankunft = state.next_free + timedelta(minutes=fahrzeit)
        if auftrag.fenster_von:
            ankunft = max(ankunft, datetime.combine(state.datum, auftrag.fenster_von))

    ende = ankunft + timedelta(minutes=auftrag.dauer_min)
    state.stops.append(
        Stop(
            typ=StopTyp.AUFTRAG,
            start=ankunft,
            ende=ende,
            auftrag_id=auftrag.id,
            fahrzeit_min=fahrzeit,
            lat=auftrag.lat,
            lon=auftrag.lon,
        )
    )
    state.arbeit_seit_letzter_pause_min += auftrag.dauer_min


def _state_from_snapshot(tech: Techniker, datum, snap: TechSnapshot) -> _TechState:
    """Initialisiert _TechState aus einem WorldSnapshot — für den Replan-Modus."""
    state = _TechState(
        techniker=tech,
        datum=datum,
        stops=list(snap.bereits_erledigte_stops),
        fruehstueck_gesetzt=snap.fruehstueck_done,
        mittag_gesetzt=snap.mittag_done,
        arbeit_seit_letzter_pause_min=snap.arbeit_seit_letzter_pause_min,
    )
    # Falls bisher gar nichts erledigt wurde, der Replan aber später startet
    # (z.B. Tech war den ganzen Vormittag idle), brauchen wir trotzdem die
    # richtige Position und Zeit. Das stellen wir über einen virtuellen Stop sicher.
    if not snap.bereits_erledigte_stops:
        state.stops.append(
            Stop(
                typ=StopTyp.DEPOT_START,
                start=snap.next_free,
                ende=snap.next_free,
                lat=snap.pos_lat,
                lon=snap.pos_lon,
            )
        )
    return state


def _finalize_tour(state: _TechState, rp: RouteProvider, ist_replan: bool = False) -> Tour:
    schichtanfang = datetime.combine(state.datum, state.techniker.schichtbeginn)

    has_depot_start = any(s.typ == StopTyp.DEPOT_START for s in state.stops)
    has_depot_ende = any(s.typ == StopTyp.DEPOT_ENDE for s in state.stops)

    if not state.mittag_gesetzt:
        start = max(state.next_free, datetime.combine(state.datum, MITTAG_START))
        ende = start + timedelta(minutes=state.techniker.pause_mittag_min)
        if ende <= state.schichtende_dt():
            state.stops.append(
                Stop(
                    typ=StopTyp.PAUSE_MITTAG,
                    start=start,
                    ende=ende,
                    lat=state.next_lat,
                    lon=state.next_lon,
                )
            )
            state.mittag_gesetzt = True

    if not state.fruehstueck_gesetzt:
        start = state.next_free
        ende = start + timedelta(minutes=state.techniker.pause_fruehstueck_min)
        if ende <= state.schichtende_dt():
            state.stops.append(
                Stop(
                    typ=StopTyp.PAUSE_FRUEHSTUECK,
                    start=start,
                    ende=ende,
                    lat=state.next_lat,
                    lon=state.next_lon,
                )
            )
            state.fruehstueck_gesetzt = True

    final_stops: list[Stop] = []
    if not has_depot_start and not ist_replan:
        final_stops.append(
            Stop(
                typ=StopTyp.DEPOT_START,
                start=schichtanfang,
                ende=schichtanfang,
                lat=state.techniker.home_lat,
                lon=state.techniker.home_lon,
            )
        )
    final_stops.extend(state.stops)

    if not has_depot_ende:
        fahrzeit_rueck = rp.travel_time_min(
            state.next_lat, state.next_lon, state.techniker.home_lat, state.techniker.home_lon
        )
        depot_ende_time = state.next_free + timedelta(minutes=fahrzeit_rueck)
        final_stops.append(
            Stop(
                typ=StopTyp.DEPOT_ENDE,
                start=depot_ende_time,
                ende=depot_ende_time,
                fahrzeit_min=fahrzeit_rueck,
                lat=state.techniker.home_lat,
                lon=state.techniker.home_lon,
            )
        )

    return Tour(
        techniker_id=state.techniker.id,
        datum=state.datum,
        stops=final_stops,
    )


class HeuristicScheduler:
    name = "heuristic"

    def plan(self, pin: PlanInput) -> Tourenplan:
        sortiert = sorted(pin.auftraege, key=lambda a: prio_score(a, pin.datum), reverse=True)

        states: dict[str, _TechState] = {}
        for t in pin.techniker:
            if t.id in pin.ausgeschlossene_techs:
                continue
            if pin.tech_anfangszustand and t.id in pin.tech_anfangszustand:
                states[t.id] = _state_from_snapshot(t, pin.datum, pin.tech_anfangszustand[t.id])
            else:
                states[t.id] = _TechState(techniker=t, datum=pin.datum)

        tp = Tourenplan(datum=pin.datum)

        for auftrag in sortiert:
            beste: tuple[str, int, datetime, int] | None = None
            for tid, state in states.items():
                ergebnis = _try_assign(state, auftrag, pin.route_provider)
                if ergebnis is None:
                    continue
                cost, ankunft, fahrzeit = ergebnis
                if beste is None or cost < beste[1]:
                    beste = (tid, cost, ankunft, fahrzeit)
            if beste is None:
                tp.nicht_zugewiesen.append(auftrag.id)
                continue
            tid, _, ankunft, fahrzeit = beste
            _commit(states[tid], auftrag, ankunft, fahrzeit)

        for tid, state in states.items():
            tp.touren[tid] = _finalize_tour(state, pin.route_provider, ist_replan=pin.ist_replan)

        return tp
