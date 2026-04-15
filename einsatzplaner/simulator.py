from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .generator import Szenarioprofil
from .geo import RouteProvider
from .models import (
    Auftrag,
    EventTyp,
    Stoerung,
    StopTyp,
    Techniker,
    Tourenplan,
)
from .scheduler.base import PlanInput, ReplanKontext, Scheduler, TechSnapshot


@dataclass
class TagesErgebnis:
    datum: date
    tourenplan: Tourenplan
    erledigt: list[str] = field(default_factory=list)
    storniert: list[str] = field(default_factory=list)
    rollover: list[str] = field(default_factory=list)
    angewandte_events: list[Stoerung] = field(default_factory=list)
    scheduler_name: str = ""

    @property
    def anzahl_erledigt(self) -> int:
        return len(self.erledigt)


@dataclass
class WochenErgebnis:
    scheduler_name: str
    szenario: str
    ergebnisse: list[TagesErgebnis] = field(default_factory=list)
    offen_am_ende: list[Auftrag] = field(default_factory=list)

    @property
    def gesamt_erledigt(self) -> int:
        return sum(e.anzahl_erledigt for e in self.ergebnisse)

    @property
    def gesamt_storniert(self) -> int:
        return sum(len(e.storniert) for e in self.ergebnisse)


def _apply_techniker_krank(tp: Tourenplan, event: Stoerung) -> list[str]:
    """Markiert alle Stops des Technikers ab Krank-Zeitpunkt als nicht_ausgefuehrt.

    Inkludiert auch den gerade laufenden Stop (start < zeit < ende) — bei
    akuter Krankmeldung bricht der Techniker sofort ab, der Auftrag rollt.
    """
    if not event.techniker_id or event.techniker_id not in tp.touren:
        return []
    tour = tp.touren[event.techniker_id]
    rollover_ids: list[str] = []
    for stop in tour.stops:
        if stop.status != "geplant":
            continue
        if stop.start >= event.zeitpunkt or (stop.start < event.zeitpunkt < stop.ende):
            if stop.typ == StopTyp.AUFTRAG and stop.auftrag_id:
                rollover_ids.append(stop.auftrag_id)
            stop.status = "nicht_ausgefuehrt"
    return rollover_ids


def _apply_kunde_absage(tp: Tourenplan, event: Stoerung) -> list[str]:
    if not event.auftrag_id:
        return []
    for tour in tp.touren.values():
        for stop in tour.stops:
            if (
                stop.typ == StopTyp.AUFTRAG
                and stop.auftrag_id == event.auftrag_id
                and stop.status == "geplant"
                and stop.start >= event.zeitpunkt
            ):
                stop.status = "storniert"
                return [event.auftrag_id]
    return []


def _apply_stau(tp: Tourenplan, event: Stoerung) -> list[str]:
    rollover: list[str] = []
    delta = timedelta(minutes=event.stau_dauer_min)
    for tid in event.betroffene_techniker:
        tour = tp.touren.get(tid)
        if not tour:
            continue
        schichtende = max((s.ende for s in tour.stops), default=event.zeitpunkt)
        for stop in tour.stops:
            if stop.start < event.zeitpunkt or stop.status != "geplant":
                continue
            stop.start += delta
            stop.ende += delta
            if stop.ende > schichtende:
                if stop.typ == StopTyp.AUFTRAG and stop.auftrag_id:
                    rollover.append(stop.auftrag_id)
                stop.status = "nicht_ausgefuehrt"
    return rollover


def _apply_auftrag_verlaengert(tp: Tourenplan, event: Stoerung) -> list[str]:
    if not event.auftrag_id or event.extra_min <= 0:
        return []
    delta = timedelta(minutes=event.extra_min)
    rollover: list[str] = []
    for tour in tp.touren.values():
        idx = next(
            (i for i, s in enumerate(tour.stops)
             if s.typ == StopTyp.AUFTRAG and s.auftrag_id == event.auftrag_id),
            None,
        )
        if idx is None:
            continue
        betroffener = tour.stops[idx]
        if betroffener.status != "geplant":
            continue
        datum = betroffener.start.date()
        schicht_dt = datetime.combine(datum, time(17, 0))
        betroffener.ende += delta
        if betroffener.ende > schicht_dt:
            if betroffener.auftrag_id:
                rollover.append(betroffener.auftrag_id)
            betroffener.status = "nicht_ausgefuehrt"
        for s in tour.stops[idx + 1:]:
            if s.status != "geplant":
                continue
            s.start += delta
            s.ende += delta
            if s.ende > schicht_dt:
                if s.typ == StopTyp.AUFTRAG and s.auftrag_id:
                    rollover.append(s.auftrag_id)
                s.status = "nicht_ausgefuehrt"
        break
    return rollover


def _apply_events(tp: Tourenplan, events: list[Stoerung]) -> tuple[list[str], list[str], list[Stoerung]]:
    angewandt: list[Stoerung] = []
    rollover: list[str] = []
    storniert: list[str] = []
    for event in sorted(events, key=lambda e: e.zeitpunkt):
        if event.typ == EventTyp.TECHNIKER_KRANK:
            rollover.extend(_apply_techniker_krank(tp, event))
        elif event.typ == EventTyp.KUNDE_ABSAGE:
            storniert.extend(_apply_kunde_absage(tp, event))
        elif event.typ == EventTyp.STAU:
            rollover.extend(_apply_stau(tp, event))
        elif event.typ == EventTyp.AUFTRAG_VERLAENGERT:
            rollover.extend(_apply_auftrag_verlaengert(tp, event))
        elif event.typ == EventTyp.NOTFALL and event.notfall_auftrag:
            rollover.append(event.notfall_auftrag.id)
        angewandt.append(event)
    return rollover, storniert, angewandt


def snapshot_tech_states(
    tp: Tourenplan,
    zeit: datetime,
    techniker: list[Techniker],
) -> dict[str, TechSnapshot]:
    """Sammelt für jeden Techniker den Zustand zum Zeitpunkt `zeit`.

    - bereits beendete Stops (s.ende ≤ zeit) gelten als erledigt und werden eingefroren.
    - ein gerade laufender Stop (s.start < zeit < s.ende) wird zu Ende geführt;
      `next_free` springt auf dessen `ende`.
    - ab `next_free` darf der Replanner den Techniker neu disponieren.
    """
    techs_by_id = {t.id: t for t in techniker}
    out: dict[str, TechSnapshot] = {}
    for tid, tour in tp.touren.items():
        tech = techs_by_id.get(tid)
        if tech is None:
            continue
        next_free = datetime.combine(tour.datum, tech.schichtbeginn)
        pos_lat, pos_lon = tech.home_lat, tech.home_lon
        fruehstueck_done = False
        mittag_done = False
        arbeit_seit_pause = 0
        erledigte: list = []
        for stop in tour.stops:
            # Abgebrochene oder stornierte Stops werden NICHT als erledigt gezählt —
            # sie sind durch ein früheres Event bereits markiert worden.
            if stop.status in ("nicht_ausgefuehrt", "storniert"):
                continue
            if stop.ende <= zeit or (stop.start < zeit < stop.ende):
                if stop.lat is not None:
                    pos_lat, pos_lon = stop.lat, stop.lon
                next_free = max(next_free, stop.ende)
                if stop.typ == StopTyp.PAUSE_FRUEHSTUECK:
                    fruehstueck_done = True
                    arbeit_seit_pause = 0
                elif stop.typ == StopTyp.PAUSE_MITTAG:
                    mittag_done = True
                    arbeit_seit_pause = 0
                elif stop.typ == StopTyp.AUFTRAG:
                    arbeit_seit_pause += stop.dauer_min
                    erledigte.append(stop)
                elif stop.typ in (StopTyp.DEPOT_START, StopTyp.DEPOT_ENDE):
                    erledigte.append(stop)
        out[tid] = TechSnapshot(
            tech_id=tid,
            next_free=next_free,
            pos_lat=pos_lat,
            pos_lon=pos_lon,
            fruehstueck_done=fruehstueck_done,
            mittag_done=mittag_done,
            arbeit_seit_letzter_pause_min=arbeit_seit_pause,
            bereits_erledigte_stops=erledigte,
        )
    return out


def offene_pending_auftraege(
    tp: Tourenplan,
    zeit: datetime,
    bekannte_auftraege: dict[str, Auftrag],
) -> list[Auftrag]:
    """Sammelt alle Aufträge, die zum Zeitpunkt `zeit` noch nicht begonnen wurden
    und im aktuellen Plan stehen — sie werden für den Replan freigegeben.

    Setzt deren Stop-Status auf 'umgeplant', damit sie im Merge nicht doppelt
    auftauchen. nicht_zugewiesene Aufträge werden ebenfalls einbezogen.
    """
    pending: list[Auftrag] = []
    seen: set[str] = set()
    for tour in tp.touren.values():
        for stop in tour.stops:
            if (
                stop.typ == StopTyp.AUFTRAG
                and stop.auftrag_id
                and stop.status == "geplant"
                and stop.start >= zeit
            ):
                aid = stop.auftrag_id
                a = bekannte_auftraege.get(aid)
                if a and aid not in seen:
                    pending.append(a)
                    seen.add(aid)
                stop.status = "umgeplant"
    for aid in tp.nicht_zugewiesen:
        a = bekannte_auftraege.get(aid)
        if a and aid not in seen:
            pending.append(a)
            seen.add(aid)
    tp.nicht_zugewiesen = [aid for aid in tp.nicht_zugewiesen if aid not in seen]
    return pending


def _classify_stops(tp: Tourenplan) -> list[str]:
    erledigt: list[str] = []
    for tour in tp.touren.values():
        for stop in tour.stops:
            if stop.typ != StopTyp.AUFTRAG or not stop.auftrag_id:
                continue
            if stop.status == "geplant":
                stop.status = "erledigt"
                erledigt.append(stop.auftrag_id)
    return erledigt


REPLAN_TRIGGERING_TYPES = {
    EventTyp.TECHNIKER_KRANK,
    EventTyp.NOTFALL,
    EventTyp.STAU,
    EventTyp.AUFTRAG_VERLAENGERT,
}


def run_tag(
    datum: date,
    techniker: list[Techniker],
    auftraege: list[Auftrag],
    scheduler: Scheduler,
    route_provider: RouteProvider,
    events_heute: list[Stoerung] | None = None,
) -> TagesErgebnis:
    """Plant den Tag morgens, arbeitet Events in Zeit-Reihenfolge ab.

    Intraday-Events (Krankmeldung, Notfall, Stau, Auftragsverlängerung) lösen
    einen Replan aus: der Scheduler bekommt einen Snapshot des aktuellen
    Welt-Zustands und die offenen Aufträge und plant den Rest des Tages neu.
    Kundenabsagen lösen keinen Replan aus (Tech hat nur eine Lücke).
    """
    pin = PlanInput(
        datum=datum,
        techniker=techniker,
        auftraege=auftraege,
        route_provider=route_provider,
    )
    tp = scheduler.plan(pin)

    events_heute = sorted(events_heute or [], key=lambda e: e.zeitpunkt)
    angewandt: list[Stoerung] = []
    storniert_ids: list[str] = []
    bekannte: dict[str, Auftrag] = {a.id: a for a in auftraege}
    replanungen_bisher = 0

    for event in events_heute:
        if event.typ == EventTyp.KUNDE_ABSAGE:
            storniert_ids.extend(_apply_kunde_absage(tp, event))
            angewandt.append(event)
            continue

        if event.typ not in REPLAN_TRIGGERING_TYPES:
            angewandt.append(event)
            continue

        # Events, die den Plan strukturell verändern — zuerst anwenden, dann replanen.
        zusatz_rollover: list[str] = []
        if event.typ == EventTyp.TECHNIKER_KRANK:
            zusatz_rollover = _apply_techniker_krank(tp, event)
        elif event.typ == EventTyp.STAU:
            zusatz_rollover = _apply_stau(tp, event)
        elif event.typ == EventTyp.AUFTRAG_VERLAENGERT:
            zusatz_rollover = _apply_auftrag_verlaengert(tp, event)
        elif event.typ == EventTyp.NOTFALL and event.notfall_auftrag:
            bekannte[event.notfall_auftrag.id] = event.notfall_auftrag

        snapshot = snapshot_tech_states(tp, event.zeitpunkt, techniker)
        pending = offene_pending_auftraege(tp, event.zeitpunkt, bekannte)
        for aid in zusatz_rollover:
            a = bekannte.get(aid)
            if a is not None and a not in pending:
                pending.append(a)
        if event.typ == EventTyp.NOTFALL and event.notfall_auftrag:
            if event.notfall_auftrag not in pending:
                pending.append(event.notfall_auftrag)

        ausgeschlossen: set[str] = set()
        if event.typ == EventTyp.TECHNIKER_KRANK and event.techniker_id:
            ausgeschlossen.add(event.techniker_id)

        bisher_erledigt_heute = sum(
            1
            for tour in tp.touren.values()
            for stop in tour.stops
            if stop.typ == StopTyp.AUFTRAG
            and stop.status in ("erledigt", "geplant")
            and stop.ende <= event.zeitpunkt
        )
        pending_pro_tech_min: dict[str, int] = {}
        rest_schicht_pro_tech_min: dict[str, int] = {}
        schicht_ende_dt = datetime.combine(datum, time(17, 0))
        for tech in techniker:
            tour = tp.touren.get(tech.id)
            offen_min = 0
            if tour is not None:
                offen_min = sum(
                    stop.dauer_min
                    for stop in tour.stops
                    if stop.typ == StopTyp.AUFTRAG
                    and stop.status == "geplant"
                    and stop.start >= event.zeitpunkt
                )
            pending_pro_tech_min[tech.id] = offen_min
            if tech.id in ausgeschlossen:
                rest_schicht_pro_tech_min[tech.id] = 0
            else:
                rest_schicht_pro_tech_min[tech.id] = max(
                    0, int((schicht_ende_dt - event.zeitpunkt).total_seconds() // 60)
                )

        replan_kontext = ReplanKontext(
            replanungen_heute_bisher=replanungen_bisher,
            trigger_event_typ=event.typ.value,
            bisher_erledigt_heute=bisher_erledigt_heute,
            pending_pro_tech_min=pending_pro_tech_min,
            rest_schicht_pro_tech_min=rest_schicht_pro_tech_min,
        )

        replan_pin = PlanInput(
            datum=datum,
            techniker=techniker,
            auftraege=pending,
            route_provider=route_provider,
            replan_ab=event.zeitpunkt,
            tech_anfangszustand=snapshot,
            ausgeschlossene_techs=ausgeschlossen,
            replan_kontext=replan_kontext,
        )
        tp = scheduler.plan(replan_pin)
        angewandt.append(event)
        replanungen_bisher += 1

    erledigt = _classify_stops(tp)

    # Alles was weder erledigt noch storniert ist = Rollover
    alle_auftrag_ids = set(bekannte.keys())
    erledigt_set = set(erledigt)
    storniert_set = set(storniert_ids)
    rollover = sorted(alle_auftrag_ids - erledigt_set - storniert_set)

    return TagesErgebnis(
        datum=datum,
        tourenplan=tp,
        erledigt=erledigt,
        storniert=list(storniert_set),
        rollover=rollover,
        angewandte_events=angewandt,
        scheduler_name=getattr(scheduler, "name", "?"),
    )


def run_woche(
    woche_auftraege: dict[date, list[Auftrag]],
    techniker: list[Techniker],
    scheduler: Scheduler,
    route_provider: RouteProvider,
    stoerungen: list[Stoerung] | None = None,
    szenario: str = "baseline",
    profil_pro_tag: dict[date, Szenarioprofil] | None = None,
    intraday_seed: int | None = None,
) -> WochenErgebnis:
    """Führt einen Wochenlauf aus.

    Neue Parameter:
    - profil_pro_tag: Wenn gesetzt, wird pro Tag das Szenarioprofil genutzt,
      um zusätzliche stochastische Störungen zu würfeln (Intraday-Raten).
      Die YAML-Events aus stoerungen werden additiv übernommen.
    - intraday_seed: Seed für den Random-Generator der Intraday-Events.
      Gleicher Seed ⇒ identische Event-Sequenz, für Reproduzierbarkeit.
    """
    stoerungen = stoerungen or []
    backlog: list[Auftrag] = []
    bekannte_auftraege: dict[str, Auftrag] = {}
    ergebnisse: list[TagesErgebnis] = []
    intraday_rng = random.Random(intraday_seed) if intraday_seed is not None else None
    # Kumulierte Netto-Arbeitsminuten pro Techniker über die Woche — für das
    # gesetzliche Wochen-Cap (ArbZG: 48 h Schnitt, max 60 h kurzzeitig).
    wochen_arbeitszeit: dict[str, int] = {t.id: 0 for t in techniker}

    for tag in sorted(woche_auftraege.keys()):
        neue = woche_auftraege[tag]
        for a in neue:
            bekannte_auftraege[a.id] = a
        auftraege_heute = list(backlog) + list(neue)

        # Mehrtägige Krankheit: Techniker, deren Krankmeldung an einem Vortag begann
        # und deren `dauer_tage`-Periode den heutigen Tag noch einschließt, sind heute
        # ganztägig nicht verfügbar — kein intraday Ereignis, sondern bekannt seit Vortag.
        ausgeschlossen_ganztaegig: set[str] = set()
        for s in stoerungen:
            if s.typ != EventTyp.TECHNIKER_KRANK or not s.techniker_id:
                continue
            ende_krank = s.zeitpunkt.date() + timedelta(days=max(0, s.dauer_tage - 1))
            if s.zeitpunkt.date() < tag <= ende_krank:
                ausgeschlossen_ganztaegig.add(s.techniker_id)

        # Wochen-Arbeitszeit-Cap: Techniker, die ihre wöchentliche Obergrenze
        # bereits erreicht haben, fallen heute aus. Trigger-Schwelle kommt aus
        # dem Profil-pro-Tag (default 2400 min = 40 h, bei Überstunden z.B. 3600 = 60 h).
        cap_heute = 2400
        if profil_pro_tag is not None:
            p = profil_pro_tag.get(tag)
            if p is not None:
                cap_heute = p.wochen_netto_max_min
        wochen_cap_erreicht = {
            tid for tid, min_gesamt in wochen_arbeitszeit.items()
            if min_gesamt >= cap_heute
        }
        ausgeschlossen_ganztaegig |= wochen_cap_erreicht

        techniker_heute = [t for t in techniker if t.id not in ausgeschlossen_ganztaegig]

        events_heute = [e for e in stoerungen if e.zeitpunkt.date() == tag]

        if profil_pro_tag is not None and intraday_rng is not None:
            profil = profil_pro_tag.get(tag)
            if profil is not None and any(
                r > 0 for r in [
                    profil.intraday_krank_rate,
                    profil.intraday_verlaengerung_rate,
                    profil.intraday_absage_rate,
                    profil.intraday_stau_rate,
                ]
            ):
                from .disruptions import generate_intraday_events
                events_heute = events_heute + generate_intraday_events(
                    tag, auftraege_heute, techniker_heute, profil, intraday_rng
                )

        for e in events_heute:
            if e.typ == EventTyp.NOTFALL and e.notfall_auftrag:
                bekannte_auftraege[e.notfall_auftrag.id] = e.notfall_auftrag
                auftraege_heute.append(e.notfall_auftrag)

        ergebnis = run_tag(tag, techniker_heute, auftraege_heute, scheduler, route_provider, events_heute)
        ergebnisse.append(ergebnis)

        # Wochen-Arbeitszeit pro Techniker fortschreiben (nur tatsächlich erledigte Stops)
        for tid, tour in ergebnis.tourenplan.touren.items():
            arbeit_tag = sum(
                s.dauer_min for s in tour.stops
                if s.typ == StopTyp.AUFTRAG and s.status == "erledigt"
            )
            wochen_arbeitszeit[tid] = wochen_arbeitszeit.get(tid, 0) + arbeit_tag

        new_backlog: list[Auftrag] = []
        for aid in ergebnis.rollover:
            a = bekannte_auftraege.get(aid)
            if a is None:
                continue
            a.rollover_count += 1
            new_backlog.append(a)
        backlog = new_backlog

    return WochenErgebnis(
        scheduler_name=getattr(scheduler, "name", "?"),
        szenario=szenario,
        ergebnisse=ergebnisse,
        offen_am_ende=list(backlog),
    )
