from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from ..models import (
    BETRIEBSHOF_LAT,
    BETRIEBSHOF_LON,
    Auftrag,
    Stop,
    StopTyp,
    Techniker,
    Tour,
    Tourenplan,
)
from .base import PlanInput


SCHICHT_DAUER_MIN = 480  # 8h
NETTO_ARBEITS_MIN = 420  # 8h minus 60min Pausen
HORIZON_MIN = 900  # Puffer: großzügig genug für Replan-Szenarien mit spätem Start
DEPOT_IDX = 0


@dataclass
class PenaltyWeights:
    """Gewichte für die Disjunction-Penalties. Je höher, desto teurer Aufträge zu droppen."""
    base_penalty: int = 50_000
    dringlichkeit_multiplier: int = 10_000
    notfall_bonus: int = 100_000
    rollover_multiplier: int = 5_000
    sla_today_bonus: int = 50_000
    sla_soon_bonus: int = 15_000  # SLA in ≤ 2 Tagen
    travel_weight_pct: int = 100  # 100 = Fahrtzeit zählt 1:1, 50 = halbiert, 200 = verdoppelt


DEFAULT_WEIGHTS = PenaltyWeights()

# Vorsichtige Worst-Case-Kalibrierung: hohe Penalties überall, damit auch bei
# Stress nichts leichtfertig gedroppt wird. Typisch, wenn ein Disponent ahnt,
# dass die Woche wild wird, aber nicht täglich nachstellt.
CHAOS_SAFE_WEIGHTS = PenaltyWeights(
    base_penalty=80_000,
    dringlichkeit_multiplier=20_000,
    notfall_bonus=200_000,
    rollover_multiplier=15_000,
    sla_today_bonus=120_000,
    sla_soon_bonus=40_000,
    travel_weight_pct=80,
)

# SLA-fokussierte Kalibrierung: gleiche Basis, aber SLA-Boni stark erhöht.
SLA_BOOST_WEIGHTS = PenaltyWeights(
    base_penalty=50_000,
    dringlichkeit_multiplier=10_000,
    notfall_bonus=150_000,
    rollover_multiplier=5_000,
    sla_today_bonus=100_000,
    sla_soon_bonus=30_000,
    travel_weight_pct=100,
)

# Naive Default-Kalibrierung: die ursprünglichen Penalty-Werte aus einer
# frühen Projektphase, bevor wir manuell nachkalibriert haben. Entspricht
# plausibel der Einstellung, die ein Disponent ohne OR-Hintergrund wählen
# würde: Penalties in der Größenordnung der Fahrzeit-Kosten, "wirkt
# vernünftig" beim ersten Hinsehen, führt aber dazu, dass der Solver
# systematisch Aufträge droppt, sobald die Fahrzeit einen kleinen Umweg
# verursacht. Dient als Vergleichspunkt für das "niemand kalibriert nach"-
# Argument in §6 des Hauptreports.
NAIVE_WEIGHTS = PenaltyWeights(
    base_penalty=2_000,
    dringlichkeit_multiplier=1_000,
    notfall_bonus=20_000,
    rollover_multiplier=500,
    sla_today_bonus=10_000,
    sla_soon_bonus=3_000,
    travel_weight_pct=100,
)


def _prio_penalty(auftrag: Auftrag, heute, weights: PenaltyWeights = DEFAULT_WEIGHTS) -> int:
    base = weights.base_penalty + weights.dringlichkeit_multiplier * int(auftrag.dringlichkeit)
    if auftrag.notfall:
        base += weights.notfall_bonus
    base += weights.rollover_multiplier * auftrag.rollover_count
    if auftrag.sla_frist:
        tage = (auftrag.sla_frist - heute).days
        if tage <= 0:
            base += weights.sla_today_bonus
        elif tage <= 2:
            base += weights.sla_soon_bonus
    return base


def _time_window_min(auftrag: Auftrag, schichtbeginn: time) -> tuple[int, int]:
    tag_start_min = schichtbeginn.hour * 60 + schichtbeginn.minute

    def _mins(t: time) -> int:
        return t.hour * 60 + t.minute - tag_start_min

    if auftrag.fenster_von and auftrag.fenster_bis:
        v = max(0, _mins(auftrag.fenster_von))
        b = min(NETTO_ARBEITS_MIN, _mins(auftrag.fenster_bis) - auftrag.dauer_min)
        b = max(v, b)
        return v, b
    return 0, NETTO_ARBEITS_MIN - auftrag.dauer_min


@dataclass
class _TechStart:
    """Pro-Vehicle Start-Spezifikation — unterscheidet Initial- und Replan-Modus."""
    tech: Techniker
    start_lat: float
    start_lon: float
    start_offset_min: int  # Minuten nach Zeit-0-Referenz bis dieser Tech losgeht
    pause_f_min: int  # 0 wenn Frühstück schon erledigt (Replan)
    pause_m_min: int  # 0 wenn Mittag schon erledigt (Replan)
    netto_rest_min: int  # verfügbare Nettoarbeitszeit bis Schichtende


class ORToolsScheduler:
    name = "ortools"

    def __init__(self, time_limit_sec: int = 8, weights: PenaltyWeights | None = None):
        self.time_limit_sec = time_limit_sec
        self.weights = weights or DEFAULT_WEIGHTS

    def plan(self, pin: PlanInput) -> Tourenplan:
        techniker = pin.techniker
        auftraege = pin.auftraege
        rp = pin.route_provider
        datum = pin.datum

        active_techs = [t for t in techniker if t.id not in pin.ausgeschlossene_techs]
        n_techs = len(active_techs)

        if not auftraege or n_techs == 0:
            return _empty_plan_mit_snapshot(datum, techniker, pin)

        tech_starts = self._build_tech_starts(active_techs, pin)
        ref_time_min = self._ref_time_min(active_techs, pin)

        # Knoten: [Depot, TechStart_0..N-1, Auftrag_0..M-1]
        # Tech-Start-Knoten haben als "Service-Time" den Start-Offset — diese Zeit
        # muss der Techniker an seinem Startknoten "warten" bevor er losziehen kann
        # (z.B. weil er noch 15 min mit einem laufenden Stop fertig wird).
        nodes: list[tuple[float, float, int]] = [(BETRIEBSHOF_LAT, BETRIEBSHOF_LON, 0)]
        tech_start_offset = 1
        for ts in tech_starts:
            nodes.append((ts.start_lat, ts.start_lon, ts.start_offset_min))
        auftrag_node_offset = len(nodes)
        for a in auftraege:
            nodes.append((a.lat, a.lon, a.dauer_min))
        n_nodes = len(nodes)

        matrix = [[0] * n_nodes for _ in range(n_nodes)]
        for i in range(n_nodes):
            for j in range(n_nodes):
                if i == j:
                    continue
                matrix[i][j] = rp.travel_time_min(nodes[i][0], nodes[i][1], nodes[j][0], nodes[j][1])

        starts = [tech_start_offset + i for i in range(n_techs)]
        ends = [DEPOT_IDX] * n_techs
        manager = pywrapcp.RoutingIndexManager(n_nodes, n_techs, starts, ends)
        routing = pywrapcp.RoutingModel(manager)

        travel_mult = max(1, self.weights.travel_weight_pct)

        def travel_cb(from_idx, to_idx):
            from_node = manager.IndexToNode(from_idx)
            to_node = manager.IndexToNode(to_idx)
            return matrix[from_node][to_node] * travel_mult // 100

        def time_cb(from_idx, to_idx):
            from_node = manager.IndexToNode(from_idx)
            to_node = manager.IndexToNode(to_idx)
            return matrix[from_node][to_node] + nodes[from_node][2]

        travel_index = routing.RegisterTransitCallback(travel_cb)
        time_index = routing.RegisterTransitCallback(time_cb)
        routing.SetArcCostEvaluatorOfAllVehicles(travel_index)

        routing.AddDimension(time_index, HORIZON_MIN, HORIZON_MIN, True, "Time")
        time_dim = routing.GetDimensionOrDie("Time")

        # Zeitfenster für Aufträge: relative Minuten zu ref_time_min
        for node_idx, auftrag in enumerate(auftraege, start=auftrag_node_offset):
            v, b = _auftrag_window_relative(auftrag, ref_time_min)
            time_dim.CumulVar(manager.NodeToIndex(node_idx)).SetRange(v, b)

        # Start-CumulVar bleibt auf [0,0] (default). Der Start-Offset wird über
        # die Service-Time am Tech-Start-Knoten als Wait-Time vor dem ersten
        # Auftrag verrechnet (siehe nodes-Setup oben).
        # End-Cap: Start-Offset + verfügbare Nettoarbeitszeit.
        for vid, ts in enumerate(tech_starts):
            end_max = min(HORIZON_MIN, ts.start_offset_min + max(1, ts.netto_rest_min))
            time_dim.CumulVar(routing.End(vid)).SetRange(0, end_max)

        for node_idx, auftrag in enumerate(auftraege, start=auftrag_node_offset):
            routing.AddDisjunction([manager.NodeToIndex(node_idx)], _prio_penalty(auftrag, datum, self.weights))

        search_params = pywrapcp.DefaultRoutingSearchParameters()
        search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        search_params.time_limit.seconds = self.time_limit_sec

        solution = routing.SolveWithParameters(search_params)
        if solution is None:
            return _empty_plan_mit_snapshot(
                datum, techniker, pin, nicht_zugewiesen=[a.id for a in auftraege]
            )

        return _solution_to_plan(
            solution=solution,
            routing=routing,
            manager=manager,
            time_dim=time_dim,
            all_techs=techniker,
            tech_starts=tech_starts,
            auftraege=auftraege,
            datum=datum,
            matrix=matrix,
            auftrag_node_offset=auftrag_node_offset,
            ref_time_min=ref_time_min,
            pin=pin,
        )

    def _build_tech_starts(self, active_techs: list[Techniker], pin: PlanInput) -> list["_TechStart"]:
        if not pin.ist_replan:
            return [
                _TechStart(
                    tech=t,
                    start_lat=t.home_lat,
                    start_lon=t.home_lon,
                    start_offset_min=0,
                    pause_f_min=t.pause_fruehstueck_min,
                    pause_m_min=t.pause_mittag_min,
                    netto_rest_min=NETTO_ARBEITS_MIN,
                )
                for t in active_techs
            ]

        replan_ab = pin.replan_ab
        assert replan_ab is not None
        replan_ab_min = replan_ab.hour * 60 + replan_ab.minute

        out: list[_TechStart] = []
        for t in active_techs:
            snap = pin.tech_anfangszustand.get(t.id) if pin.tech_anfangszustand else None
            if snap is None:
                pos_lat, pos_lon = t.home_lat, t.home_lon
                next_free_min = replan_ab_min
                f_min = t.pause_fruehstueck_min
                m_min = t.pause_mittag_min
            else:
                pos_lat, pos_lon = snap.pos_lat, snap.pos_lon
                next_free_min = snap.next_free.hour * 60 + snap.next_free.minute
                f_min = 0 if snap.fruehstueck_done else t.pause_fruehstueck_min
                m_min = 0 if snap.mittag_done else t.pause_mittag_min
            start_offset = max(0, next_free_min - replan_ab_min)
            schicht_ende_min = t.schichtende.hour * 60 + t.schichtende.minute
            # Brutto-Rest = ab tatsächlicher Weiterarbeitsfähigkeit (next_free)
            # bis Schichtende, nicht ab Replan-Zeitpunkt
            rest_brutto = max(1, schicht_ende_min - next_free_min)
            rest_netto = max(1, rest_brutto - f_min - m_min)
            out.append(
                _TechStart(
                    tech=t,
                    start_lat=pos_lat,
                    start_lon=pos_lon,
                    start_offset_min=start_offset,
                    pause_f_min=f_min,
                    pause_m_min=m_min,
                    netto_rest_min=rest_netto,
                )
            )
        return out

    def _ref_time_min(self, active_techs: list[Techniker], pin: PlanInput) -> int:
        """Minutenbasis für relative Zeitangaben im Modell.

        Initial: tech.schichtbeginn (z.B. 480 = 8:00). Replan: replan_ab.
        """
        if pin.ist_replan and pin.replan_ab is not None:
            return pin.replan_ab.hour * 60 + pin.replan_ab.minute
        if active_techs:
            sb = active_techs[0].schichtbeginn
            return sb.hour * 60 + sb.minute
        return 0


def _auftrag_window_relative(auftrag: Auftrag, ref_time_min: int) -> tuple[int, int]:
    """Berechnet das Zeitfenster eines Auftrags in Minuten relativ zu ref_time_min."""
    if auftrag.fenster_von and auftrag.fenster_bis:
        v_abs = auftrag.fenster_von.hour * 60 + auftrag.fenster_von.minute
        b_abs = auftrag.fenster_bis.hour * 60 + auftrag.fenster_bis.minute
        v = max(0, v_abs - ref_time_min)
        b = max(v, b_abs - ref_time_min - auftrag.dauer_min)
        return v, b
    return 0, HORIZON_MIN - auftrag.dauer_min


def _empty_plan_mit_snapshot(
    datum,
    techniker: list[Techniker],
    pin: PlanInput,
    nicht_zugewiesen: list[str] | None = None,
) -> Tourenplan:
    """Leerer Plan — für Techniker mit Snapshot werden bereits erledigte Stops behalten."""
    tp = Tourenplan(datum=datum, nicht_zugewiesen=nicht_zugewiesen or [])
    for t in techniker:
        snap = pin.tech_anfangszustand.get(t.id) if pin.tech_anfangszustand else None
        if snap is not None and snap.bereits_erledigte_stops:
            stops = list(snap.bereits_erledigte_stops)
            depot_ende_dt = snap.next_free
            stops.append(
                Stop(
                    typ=StopTyp.DEPOT_ENDE,
                    start=depot_ende_dt,
                    ende=depot_ende_dt,
                    lat=snap.pos_lat,
                    lon=snap.pos_lon,
                )
            )
        else:
            anfang = datetime.combine(datum, t.schichtbeginn)
            stops = [
                Stop(typ=StopTyp.DEPOT_START, start=anfang, ende=anfang, lat=t.home_lat, lon=t.home_lon),
                Stop(typ=StopTyp.DEPOT_ENDE, start=anfang, ende=anfang, lat=t.home_lat, lon=t.home_lon),
            ]
        tp.touren[t.id] = Tour(techniker_id=t.id, datum=datum, stops=stops)
    return tp


def _solution_to_plan(
    solution,
    routing,
    manager,
    time_dim,
    all_techs: list[Techniker],
    tech_starts: list[_TechStart],
    auftraege: list[Auftrag],
    datum,
    matrix: list[list[int]],
    auftrag_node_offset: int,
    ref_time_min: int,
    pin: PlanInput,
) -> Tourenplan:
    """Parst die OR-Tools-Lösung in einen Tourenplan.

    Funktioniert für Initial- und Replan-Modus:
    - Initial: Start-Knoten = Depot (Node 0), Schichtanfang = 8:00
    - Replan: Start-Knoten = Tech-Position (Node 1..N), Schichtanfang = replan_ab
      Bereits erledigte Stops aus dem Snapshot werden am Tour-Anfang einkopiert.
    """
    tp = Tourenplan(datum=datum)
    besucht: set[int] = set()
    ref_dt = datetime.combine(datum, time(ref_time_min // 60, ref_time_min % 60))

    for v_id, ts in enumerate(tech_starts):
        tech = ts.tech

        snap = pin.tech_anfangszustand.get(tech.id) if (pin.ist_replan and pin.tech_anfangszustand) else None
        bereits_erledigt = list(snap.bereits_erledigte_stops) if snap else []

        # Pausen-Zielzeiten relativ zu ref_dt:
        # Frühstück-Ziel = 10:00, Mittag-Ziel-Fenster = tech.mittag_fenster
        fruehstueck_ziel = time(10, 0).hour * 60 + time(10, 0).minute - ref_time_min
        mittag_von = tech.mittag_fenster_von.hour * 60 + tech.mittag_fenster_von.minute - ref_time_min
        mittag_bis = tech.mittag_fenster_bis.hour * 60 + tech.mittag_fenster_bis.minute - ref_time_min

        # Lese rohe Stops (Ankunftszeit in Minuten relativ zu ref_dt)
        rohe_stops: list[tuple[int, int, int, int, int]] = []
        index = routing.Start(v_id)
        # erster Knoten ist der Tech-Start-Knoten (nicht Depot 0 im Replan, sondern
        # der virtuelle Start, lat/lon = ts.start_lat/lon). Wir nutzen seine
        # Koordinaten für die Distanz zum ersten Auftrag.
        prev_node = manager.IndexToNode(index)
        index = solution.Value(routing.NextVar(index))

        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node >= auftrag_node_offset:
                auftrag_idx = node - auftrag_node_offset
                auftrag = auftraege[auftrag_idx]
                besucht.add(node)
                fahrzeit = matrix[prev_node][node]
                ankunft = solution.Value(time_dim.CumulVar(index))
                dauer = auftrag.dauer_min
                rohe_stops.append((ankunft, ankunft + dauer, fahrzeit, node, dauer))
                prev_node = node
            index = solution.Value(routing.NextVar(index))

        end_min = solution.Value(time_dim.CumulVar(routing.End(v_id)))
        fahrzeit_rueck = matrix[prev_node][DEPOT_IDX] if prev_node != DEPOT_IDX else 0

        # Baue Stops: optional vorne die bereits_erledigt Liste, dann Depot_Start
        # (bei Initial) bzw. virtueller Restart-Stop (bei Replan) + neue Aufträge + Pausen.
        stops: list[Stop] = list(bereits_erledigt)
        has_depot_start_vorne = any(s.typ == StopTyp.DEPOT_START for s in stops)

        if not has_depot_start_vorne and not pin.ist_replan:
            # Initial: klassischer Depot_Start am Schichtanfang
            anfang_dt = ref_dt
            stops.append(
                Stop(
                    typ=StopTyp.DEPOT_START,
                    start=anfang_dt,
                    ende=anfang_dt,
                    lat=tech.home_lat,
                    lon=tech.home_lon,
                )
            )

        fruehstueck_eingefuegt = (snap is not None and snap.fruehstueck_done) or ts.pause_f_min == 0
        mittag_eingefuegt = (snap is not None and snap.mittag_done) or ts.pause_m_min == 0
        shift_offset = 0

        for (ankunft, ende, fahrzeit, node, dauer) in rohe_stops:
            auftrag_idx = node - auftrag_node_offset
            auftrag = auftraege[auftrag_idx]
            adjusted_ankunft = ankunft + shift_offset

            if (not fruehstueck_eingefuegt and ts.pause_f_min > 0
                    and adjusted_ankunft >= fruehstueck_ziel):
                p_start = max(fruehstueck_ziel, adjusted_ankunft - dauer - fahrzeit)
                p_start = min(p_start, adjusted_ankunft)
                p_start_dt = ref_dt + timedelta(minutes=p_start)
                pause_lat = stops[-1].lat if stops and stops[-1].lat is not None else ts.start_lat
                pause_lon = stops[-1].lon if stops and stops[-1].lon is not None else ts.start_lon
                stops.append(
                    Stop(
                        typ=StopTyp.PAUSE_FRUEHSTUECK,
                        start=p_start_dt,
                        ende=p_start_dt + timedelta(minutes=ts.pause_f_min),
                        lat=pause_lat,
                        lon=pause_lon,
                    )
                )
                shift_offset += ts.pause_f_min
                adjusted_ankunft += ts.pause_f_min
                fruehstueck_eingefuegt = True

            if (not mittag_eingefuegt and ts.pause_m_min > 0
                    and adjusted_ankunft >= mittag_von):
                p_start = max(mittag_von, min(adjusted_ankunft, mittag_bis - ts.pause_m_min))
                p_start = min(p_start, adjusted_ankunft)
                p_start_dt = ref_dt + timedelta(minutes=p_start)
                pause_lat = stops[-1].lat if stops and stops[-1].lat is not None else ts.start_lat
                pause_lon = stops[-1].lon if stops and stops[-1].lon is not None else ts.start_lon
                stops.append(
                    Stop(
                        typ=StopTyp.PAUSE_MITTAG,
                        start=p_start_dt,
                        ende=p_start_dt + timedelta(minutes=ts.pause_m_min),
                        lat=pause_lat,
                        lon=pause_lon,
                    )
                )
                shift_offset += ts.pause_m_min
                adjusted_ankunft += ts.pause_m_min
                mittag_eingefuegt = True

            start_dt = ref_dt + timedelta(minutes=adjusted_ankunft)
            ende_dt = start_dt + timedelta(minutes=dauer)
            stops.append(
                Stop(
                    typ=StopTyp.AUFTRAG,
                    start=start_dt,
                    ende=ende_dt,
                    auftrag_id=auftrag.id,
                    fahrzeit_min=fahrzeit,
                    lat=auftrag.lat,
                    lon=auftrag.lon,
                )
            )

        final_end = end_min + shift_offset
        last_pos_lat = stops[-1].lat if stops and stops[-1].lat is not None else ts.start_lat
        last_pos_lon = stops[-1].lon if stops and stops[-1].lon is not None else ts.start_lon

        if not mittag_eingefuegt and ts.pause_m_min > 0:
            p_start = max(mittag_von, final_end)
            p_start_dt = ref_dt + timedelta(minutes=p_start)
            stops.append(
                Stop(
                    typ=StopTyp.PAUSE_MITTAG,
                    start=p_start_dt,
                    ende=p_start_dt + timedelta(minutes=ts.pause_m_min),
                    lat=last_pos_lat,
                    lon=last_pos_lon,
                )
            )
            final_end = p_start + ts.pause_m_min

        if not fruehstueck_eingefuegt and ts.pause_f_min > 0:
            p_start_dt = ref_dt + timedelta(minutes=final_end)
            stops.append(
                Stop(
                    typ=StopTyp.PAUSE_FRUEHSTUECK,
                    start=p_start_dt,
                    ende=p_start_dt + timedelta(minutes=ts.pause_f_min),
                    lat=last_pos_lat,
                    lon=last_pos_lon,
                )
            )
            final_end += ts.pause_f_min

        depot_ende_dt = ref_dt + timedelta(minutes=final_end)
        stops.append(
            Stop(
                typ=StopTyp.DEPOT_ENDE,
                start=depot_ende_dt,
                ende=depot_ende_dt,
                fahrzeit_min=fahrzeit_rueck,
                lat=tech.home_lat,
                lon=tech.home_lon,
            )
        )
        tp.touren[tech.id] = Tour(techniker_id=tech.id, datum=datum, stops=stops)

    # Ausgeschlossene oder inaktive Techs bekommen eine minimale Leer-Tour
    for t in all_techs:
        if t.id in tp.touren:
            continue
        snap = pin.tech_anfangszustand.get(t.id) if pin.tech_anfangszustand else None
        if snap is not None and snap.bereits_erledigte_stops:
            leer_stops = list(snap.bereits_erledigte_stops)
            leer_stops.append(
                Stop(
                    typ=StopTyp.DEPOT_ENDE,
                    start=snap.next_free,
                    ende=snap.next_free,
                    lat=snap.pos_lat,
                    lon=snap.pos_lon,
                )
            )
        else:
            anfang = datetime.combine(datum, t.schichtbeginn)
            leer_stops = [
                Stop(typ=StopTyp.DEPOT_START, start=anfang, ende=anfang, lat=t.home_lat, lon=t.home_lon),
                Stop(typ=StopTyp.DEPOT_ENDE, start=anfang, ende=anfang, lat=t.home_lat, lon=t.home_lon),
            ]
        tp.touren[t.id] = Tour(techniker_id=t.id, datum=datum, stops=leer_stops)

    for node_idx, auftrag in enumerate(auftraege, start=auftrag_node_offset):
        if node_idx not in besucht:
            tp.nicht_zugewiesen.append(auftrag.id)

    return tp
