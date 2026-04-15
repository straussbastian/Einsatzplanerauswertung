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

    - Die Mittagspause MUSS im Fenster `tech.mittag_fenster_von .. tech.mittag_fenster_bis`
      liegen (gesetzliche Vorgabe). Die Frühstückspause im Fenster 9:00–11:00.
    - Aufträge, die eine Pausenzielzeit umspannen, werden visuell in Teil 1 +
      Pause + Teil 2 zerlegt (Teil 2 trägt dieselbe auftrag_id).
    - Wenn keine freie Lücke im Fenster existiert und kein Auftrag die Zielzeit
      direkt überspannt, wird ein Auftrag gesucht, der zumindest teilweise
      ins Fenster ragt, und dort gesplittet — die Pause bleibt im Fenster.
    - Die Reihenfolge und das Ende der Tour ändern sich nicht.
    """
    pausen_meta: dict[StopTyp, tuple[time, int, time, time]] = {
        # (ziel_zeit, dauer_min, fenster_von, fenster_bis)
        StopTyp.PAUSE_FRUEHSTUECK: (time(10, 0), tech.pause_fruehstueck_min, time(9, 0), time(11, 0)),
        StopTyp.PAUSE_MITTAG: (time(12, 0), tech.pause_mittag_min, tech.mittag_fenster_von, tech.mittag_fenster_bis),
    }

    original_pausen = {
        typ: next((s for s in tour.stops if s.typ == typ), None)
        for typ in pausen_meta
    }

    # Start: Stops ohne Pausen
    neue_stops: list[Stop] = [s for s in tour.stops if s.typ not in pausen_meta]

    def _lat_lon(idx: int) -> tuple[float, float]:
        if idx > 0 and neue_stops[idx - 1].lat is not None:
            return neue_stops[idx - 1].lat, neue_stops[idx - 1].lon  # type: ignore[return-value]
        return tech.home_lat, tech.home_lon

    for pause_typ, (ziel_t, dauer, fv_t, fb_t) in pausen_meta.items():
        if original_pausen[pause_typ] is None:
            continue
        ziel_dt = datetime.combine(datum, ziel_t)
        fenster_von_dt = datetime.combine(datum, fv_t)
        fenster_bis_dt = datetime.combine(datum, fb_t)
        pause_orig = original_pausen[pause_typ]

        # 1. Versuche ersten freien Slot IM Fenster zu finden
        inserted = False
        for i in range(len(neue_stops) + 1):
            prev_end = neue_stops[i - 1].ende if i > 0 else datetime.combine(datum, tech.schichtbeginn)
            next_start = neue_stops[i].start if i < len(neue_stops) else datetime.combine(datum, tech.schichtende)
            slot_start = max(prev_end, fenster_von_dt)
            slot_ende_max = min(next_start, fenster_bis_dt)
            if slot_ende_max - slot_start >= timedelta(minutes=dauer):
                pause_start = max(slot_start, min(ziel_dt, slot_ende_max - timedelta(minutes=dauer)))
                lat, lon = _lat_lon(i)
                neue_stops.insert(i, Stop(
                    typ=pause_typ,
                    start=pause_start,
                    ende=pause_start + timedelta(minutes=dauer),
                    lat=lat, lon=lon,
                    status=pause_orig.status,
                ))
                inserted = True
                break
        if inserted:
            continue

        # 2. Kein freier Slot → splitte einen Auftrag, der die Zielzeit oder das Fenster überspannt
        split_idx = None
        for i, s in enumerate(neue_stops):
            if s.typ != StopTyp.AUFTRAG:
                continue
            # Priorität 1: Auftrag, der die Zielzeit direkt überspannt
            if s.start < ziel_dt < s.ende:
                split_idx = i
                break
        if split_idx is None:
            # Priorität 2: irgendein Auftrag, der das Fenster überspannt
            for i, s in enumerate(neue_stops):
                if s.typ != StopTyp.AUFTRAG:
                    continue
                if s.start < fenster_bis_dt and s.ende > fenster_von_dt:
                    split_idx = i
                    break

        if split_idx is not None:
            auftrag = neue_stops[split_idx]
            # Split-Zeitpunkt: ins Fenster clampen, so nah wie möglich an Ziel
            split_at = max(fenster_von_dt, min(ziel_dt, fenster_bis_dt - timedelta(minutes=dauer)))
            split_at = max(split_at, auftrag.start)
            split_at = min(split_at, auftrag.ende)
            teil1 = replace(auftrag, ende=split_at)
            pause = Stop(
                typ=pause_typ,
                start=split_at,
                ende=split_at + timedelta(minutes=dauer),
                lat=auftrag.lat, lon=auftrag.lon,
                status="geplant",
                auftrag_id=None,
            )
            teil2 = replace(
                auftrag,
                start=split_at + timedelta(minutes=dauer),
                ende=auftrag.ende + timedelta(minutes=dauer),
            )
            neue_stops[split_idx:split_idx + 1] = [teil1, pause, teil2]
            continue

        # 3. Letzter Fallback: keine Option im Fenster — behalte Original-Position
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
