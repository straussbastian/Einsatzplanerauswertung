"""Darstellungs-Helper für das Gantt-Chart.

Die Core-Tour (einsatzplaner/scheduler/*.py) positioniert Pausen additiv hinter
Aufträgen — die Zeit-Rechnung ist korrekt, aber das Gantt sieht fachlich falsch
aus: ein 3-Stunden-Auftrag, der um 10:30 beginnt, "läuft" dort vermeintlich
durchgehend bis 13:30 ohne Mittagspause. In der Handwerks-Realität unterbricht
der Techniker den Auftrag, macht Pause um 12:00 und arbeitet danach weiter.

`realistic_gantt_stops` transformiert die Stop-Liste einer Tour für die
Darstellung: Pausen werden an ihre natürlichen Zielzeiten (Frühstück ~10:00,
Mittag ~12:00) geschoben; überspannende Aufträge werden visuell in zwei Teile
zerlegt. Die Gesamtdauer und Reihenfolge der Auftragsbearbeitung bleiben
identisch, die Core-Tour wird nicht mutiert.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta

from .models import Stop, StopTyp, Techniker, Tour


_PAUSE_META: dict[StopTyp, tuple[time, str]] = {
    StopTyp.PAUSE_FRUEHSTUECK: (time(10, 0), "fruehstueck"),
    StopTyp.PAUSE_MITTAG: (time(12, 0), "mittag"),
}


def realistic_gantt_stops(tour: Tour, tech: Techniker, datum: date) -> list[Stop]:
    """Liefert eine Stop-Liste mit Pausen an ihren natürlichen Zeitpunkten.

    - Aufträge, die eine Pausenzielzeit umspannen, werden visuell in Teil 1 +
      Pause + Teil 2 zerlegt (Teil 2 trägt dieselbe auftrag_id).
    - Pausen, deren Zielzeit außerhalb laufender Aufträge liegt, bleiben in
      zeitlich passender Position.
    - Die Reihenfolge und das Ende der Tour ändern sich nicht.
    """
    pausen_meta = {
        StopTyp.PAUSE_FRUEHSTUECK: (time(10, 0), tech.pause_fruehstueck_min),
        StopTyp.PAUSE_MITTAG: (time(12, 0), tech.pause_mittag_min),
    }

    original_pausen = {
        typ: next((s for s in tour.stops if s.typ == typ), None)
        for typ in pausen_meta
    }

    # Start: Stops ohne Pausen
    neue_stops: list[Stop] = [s for s in tour.stops if s.typ not in pausen_meta]

    for pause_typ, (ziel_t, dauer) in pausen_meta.items():
        if original_pausen[pause_typ] is None:
            continue
        ziel_dt = datetime.combine(datum, ziel_t)

        conflict_idx = None
        for i, s in enumerate(neue_stops):
            if s.typ == StopTyp.AUFTRAG and s.start < ziel_dt < s.ende:
                conflict_idx = i
                break

        if conflict_idx is not None:
            auftrag = neue_stops[conflict_idx]
            teil1 = replace(auftrag, ende=ziel_dt)
            pause = Stop(
                typ=pause_typ,
                start=ziel_dt,
                ende=ziel_dt + timedelta(minutes=dauer),
                lat=auftrag.lat,
                lon=auftrag.lon,
                status="geplant",
                auftrag_id=None,
            )
            teil2 = replace(
                auftrag,
                start=ziel_dt + timedelta(minutes=dauer),
                ende=auftrag.ende + timedelta(minutes=dauer),
            )
            neue_stops[conflict_idx:conflict_idx + 1] = [teil1, pause, teil2]
        else:
            pause_orig = original_pausen[pause_typ]
            best_spot = None
            for i in range(len(neue_stops) + 1):
                prev_end = neue_stops[i - 1].ende if i > 0 else datetime.combine(datum, tech.schichtbeginn)
                next_start = neue_stops[i].start if i < len(neue_stops) else datetime.combine(datum, tech.schichtende)
                if prev_end <= ziel_dt and ziel_dt + timedelta(minutes=dauer) <= next_start:
                    best_spot = (i, ziel_dt)
                    break
                if prev_end <= next_start and next_start - prev_end >= timedelta(minutes=dauer):
                    start = max(prev_end, ziel_dt)
                    if start + timedelta(minutes=dauer) <= next_start:
                        best_spot = (i, start)
            if best_spot is not None:
                i, start = best_spot
                pause = Stop(
                    typ=pause_typ,
                    start=start,
                    ende=start + timedelta(minutes=dauer),
                    lat=neue_stops[i - 1].lat if i > 0 and neue_stops[i - 1].lat is not None else tech.home_lat,
                    lon=neue_stops[i - 1].lon if i > 0 and neue_stops[i - 1].lon is not None else tech.home_lon,
                    status=pause_orig.status,
                )
                neue_stops.insert(i, pause)
            else:
                neue_stops.append(replace(pause_orig))

    return sorted(neue_stops, key=lambda s: s.start)


def realistic_gantt_all(tourenplan, techs: list[Techniker], datum: date) -> dict[str, list[Stop]]:
    """Wendet realistic_gantt_stops auf alle Touren eines Plans an."""
    tech_map = {t.id: t for t in techs}
    out = {}
    for tid, tour in tourenplan.touren.items():
        tech = tech_map.get(tid)
        if tech is None:
            out[tid] = list(tour.stops)
        else:
            out[tid] = realistic_gantt_stops(tour, tech, datum)
    return out
