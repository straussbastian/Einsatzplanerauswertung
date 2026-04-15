from __future__ import annotations

import random

from dotenv import load_dotenv

load_dotenv()

import folium
import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_folium import st_folium

from einsatzplaner.disruptions import list_scenarios, load_scenario
from einsatzplaner.evaluator import compute_metriken, tages_df
from einsatzplaner.generator import (
    Szenarioprofil,
    generate_auftraege,
    generate_techniker,
    generate_woche,
    naechster_montag,
)
from einsatzplaner.geo import HaversineRouteProvider
from einsatzplaner.models import (
    BETRIEBSHOF_LAT,
    BETRIEBSHOF_LON,
    BETRIEBSHOF_NAME,
    StopTyp,
    Tourenplan,
)
from einsatzplaner.scheduler.base import PlanInput
from einsatzplaner.scheduler.heuristic import HeuristicScheduler, prio_score
from einsatzplaner.visualization import realistic_gantt_stops
from einsatzplaner.scheduler.hybrid import LLMGuidedVRPScheduler
from einsatzplaner.scheduler.llm import LLMScheduler
from einsatzplaner.scheduler.ortools_vrp import ORToolsScheduler
from einsatzplaner.simulator import WochenErgebnis, run_woche


st.set_page_config(page_title="Einsatzplaner", layout="wide")
st.title("Einsatzplaner — Heizungsservice Oldenburg")

mode = st.sidebar.radio("Modus", ["Tag", "Woche"], horizontal=True)
seed = st.sidebar.number_input("Random Seed", value=42, step=1)
n_techs = st.sidebar.slider("Techniker", 5, 15, 10)
scheduler_name = st.sidebar.selectbox(
    "Scheduler", ["Heuristik", "OR-Tools", "LLM", "Hybrid (LLM+OR-Tools)"]
)
llm_model = st.sidebar.selectbox(
    "LLM Modell",
    ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
    index=1,
    help="Nur relevant wenn Scheduler LLM oder Hybrid nutzt",
)
run = st.sidebar.button("Planen", type="primary")


def _build_scheduler(name: str | None = None):
    n = name or scheduler_name
    if n == "OR-Tools":
        return ORToolsScheduler(time_limit_sec=5)
    if n == "LLM":
        return LLMScheduler(model=llm_model)
    if n == "Hybrid (LLM+OR-Tools)":
        return LLMGuidedVRPScheduler(model=llm_model, time_limit_sec=5)
    return HeuristicScheduler()

TECH_COLORS = px.colors.qualitative.Set3 + px.colors.qualitative.Pastel


def _gantt_df(tp: Tourenplan, techs: list | None = None) -> pd.DataFrame:
    tech_map = {t.id: t for t in techs} if techs else {}
    rows = []
    for tid, tour in tp.touren.items():
        tech = tech_map.get(tid)
        if tech is not None:
            stops = realistic_gantt_stops(tour, tech, tp.datum)
        else:
            stops = tour.stops
        for stop in stops:
            if stop.typ in (StopTyp.DEPOT_START, StopTyp.DEPOT_ENDE):
                label, typ, farbe = "Depot", "Depot", "depot"
            elif stop.typ in (StopTyp.PAUSE_FRUEHSTUECK, StopTyp.PAUSE_MITTAG):
                label, typ, farbe = "Pause", "Pause", "pause"
            else:
                label, typ, farbe = stop.auftrag_id or "?", "Auftrag", stop.status
            rows.append(
                dict(
                    Techniker=tid,
                    Start=stop.start,
                    Ende=stop.ende if stop.ende > stop.start else stop.start + pd.Timedelta(minutes=1),
                    Typ=typ,
                    Status=farbe,
                    Label=label,
                )
            )
    return pd.DataFrame(rows)


def _render_gantt(tp: Tourenplan, techs: list | None = None) -> None:
    df = _gantt_df(tp, techs=techs)
    if df.empty:
        st.info("Keine Touren vorhanden.")
        return
    fig = px.timeline(
        df,
        x_start="Start",
        x_end="Ende",
        y="Techniker",
        color="Status",
        hover_data=["Label", "Typ"],
        category_orders={"Techniker": sorted(df["Techniker"].unique())},
        color_discrete_map={
            "geplant": "#1f77b4",
            "erledigt": "#2ca02c",
            "storniert": "#d62728",
            "nicht_ausgefuehrt": "#ff7f0e",
            "pause": "#9aa0a6",
            "depot": "#444444",
        },
    )
    fig.update_yaxes(autorange="reversed")
    fig.update_layout(height=420, margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)


def _render_map(tp: Tourenplan, auftraege_by_id: dict) -> None:
    m = folium.Map(location=[BETRIEBSHOF_LAT, BETRIEBSHOF_LON], zoom_start=11, tiles="cartodbpositron")
    folium.Marker(
        [BETRIEBSHOF_LAT, BETRIEBSHOF_LON],
        popup=BETRIEBSHOF_NAME,
        icon=folium.Icon(color="black", icon="home"),
    ).add_to(m)
    for idx, (tid, tour) in enumerate(tp.touren.items()):
        color = TECH_COLORS[idx % len(TECH_COLORS)]
        punkte = [(BETRIEBSHOF_LAT, BETRIEBSHOF_LON)]
        for stop in tour.stops:
            if stop.typ == StopTyp.AUFTRAG and stop.lat is not None and stop.auftrag_id:
                punkte.append((stop.lat, stop.lon))
                a = auftraege_by_id.get(stop.auftrag_id)
                popup = f"{tid}: {stop.auftrag_id}<br>{a.kunde if a else ''}<br>{a.adresse if a else ''}<br>Status: {stop.status}"
                folium.CircleMarker(
                    [stop.lat, stop.lon],
                    radius=6,
                    color=color,
                    fill=True,
                    fill_opacity=0.9,
                    popup=popup,
                ).add_to(m)
        punkte.append((BETRIEBSHOF_LAT, BETRIEBSHOF_LON))
        folium.PolyLine(punkte, color=color, weight=3, opacity=0.7, tooltip=tid).add_to(m)
    st_folium(m, use_container_width=True, height=520, returned_objects=[])


def _auftraege_df(auftraege, heute) -> pd.DataFrame:
    return pd.DataFrame(
        [
            dict(
                id=a.id,
                kunde=a.kunde,
                typ=a.typ,
                adresse=a.adresse,
                dauer_min=a.dauer_min,
                prio=int(a.dringlichkeit),
                terminart=a.terminart,
                fenster=(f"{a.fenster_von.isoformat()}-{a.fenster_bis.isoformat()}" if a.hat_fenster else ""),
                notfall=a.notfall,
                rollover=a.rollover_count,
                score=round(prio_score(a, heute), 1),
                sla=a.sla_frist.isoformat() if a.sla_frist else "",
            )
            for a in auftraege
        ]
    )


def _tourplan_summary(tp: Tourenplan) -> pd.DataFrame:
    rows = []
    for tid, tour in tp.touren.items():
        arbeit = sum(
            s.dauer_min for s in tour.stops if s.typ == StopTyp.AUFTRAG and s.status != "storniert"
        )
        fahrt = tour.gesamt_fahrzeit_min
        rows.append(
            dict(
                techniker=tid,
                auftraege=len(tour.auftrag_ids),
                arbeit_min=arbeit,
                fahrt_min=fahrt,
                auslastung_pct=round(100 * arbeit / 420, 1),
            )
        )
    return pd.DataFrame(rows)


def render_tag_mode() -> None:
    n_auftraege = st.sidebar.slider("Aufträge am Tag", 20, 80, 45)
    planungstag = st.sidebar.date_input("Planungstag", value=naechster_montag())

    if run or st.session_state.get("_last_tag") is None:
        rng = random.Random(int(seed))
        techs = generate_techniker(n_techs, rng)
        auftraege = generate_auftraege(int(n_auftraege), planungstag, rng=rng)
        rp = HaversineRouteProvider()
        sched = _build_scheduler()
        with st.spinner(f"Planung mit {sched.name}…"):
            tp = sched.plan(
                PlanInput(datum=planungstag, techniker=techs, auftraege=auftraege, route_provider=rp)
            )
        st.session_state["_last_tag"] = dict(
            tp=tp, auftraege=auftraege, datum=planungstag, scheduler=sched.name, techs=techs
        )

    state = st.session_state["_last_tag"]
    tp: Tourenplan = state["tp"]
    auftraege = state["auftraege"]
    auftraege_by_id = {a.id: a for a in auftraege}
    techs_state = state.get("techs")

    col_a, col_b, col_c, col_d = st.columns(4)
    zugewiesen = sum(len(t.auftrag_ids) for t in tp.touren.values())
    col_a.metric("Aufträge", len(auftraege))
    col_b.metric("Zugewiesen", zugewiesen)
    col_c.metric("Nicht zugewiesen", len(tp.nicht_zugewiesen))
    col_d.metric("Gesamtfahrzeit", f"{sum(t.gesamt_fahrzeit_min for t in tp.touren.values())} min")

    tab1, tab2, tab3, tab4 = st.tabs(["Aufträge", "Gantt", "Karte", "Techniker"])
    with tab1:
        st.dataframe(_auftraege_df(auftraege, state["datum"]), use_container_width=True, height=520)
        if tp.nicht_zugewiesen:
            st.warning(f"Nicht zugewiesen: {', '.join(tp.nicht_zugewiesen)}")
    with tab2:
        _render_gantt(tp, techs=techs_state)
    with tab3:
        _render_map(tp, auftraege_by_id)
    with tab4:
        st.dataframe(_tourplan_summary(tp), use_container_width=True)


def _events_df(we: WochenErgebnis) -> pd.DataFrame:
    rows = []
    for tag in we.ergebnisse:
        for e in tag.angewandte_events:
            rows.append(
                dict(
                    datum=tag.datum,
                    zeit=e.zeitpunkt.strftime("%H:%M"),
                    typ=e.typ.value,
                    techniker=e.techniker_id or "",
                    auftrag=e.auftrag_id or "",
                    stau_min=e.stau_dauer_min or "",
                    betroffene=", ".join(e.betroffene_techniker),
                )
            )
    return pd.DataFrame(rows)


def _profil_slider() -> Szenarioprofil:
    presets = Szenarioprofil.presets()
    preset_name = st.sidebar.selectbox(
        "Intensitäts-Preset",
        ["Manuell"] + list(presets.keys()),
        index=1,
        help="Preset setzt die Slider unten; 'Manuell' lässt dich frei regeln.",
    )
    base = presets[preset_name] if preset_name != "Manuell" else Szenarioprofil()
    key_suffix = preset_name

    with st.sidebar.expander("Szenario-Intensität", expanded=True):
        notfall_pct = st.slider(
            "Notfall-Rate (%)", 0, 50, int(base.notfall_rate * 100), key=f"nf_{key_suffix}",
            help="Anteil der Aufträge mit Notfall-Flag.",
        )
        sla_pct = st.slider(
            "SLA-Druck (%)", 0, 60, int(base.sla_druck_rate * 100), key=f"sla_{key_suffix}",
            help="Anteil mit SLA-Frist ≤ heute.",
        )
        ueberlast = st.slider(
            "Überlast (%)", 50, 300, int(base.ueberlast_pct), step=10, key=f"ul_{key_suffix}",
            help="100% = Standard (45/Tag), 200% = doppelte Auftragsmenge.",
        )
        rollover = st.slider(
            "Rollover-Altlast (Aufträge)", 0, 40, int(base.rollover_vorbelastung), key=f"ro_{key_suffix}",
            help="Wieviel überfällige Aufträge (rollover_count 1-3) am Montag schon im Backlog sind.",
        )
        st.caption(
            f"Dringlichkeit-Gewichte (niedrig/mittel/hoch): {base.dringlichkeit_gewichte}"
        )

    return Szenarioprofil(
        notfall_rate=notfall_pct / 100,
        sla_druck_rate=sla_pct / 100,
        ueberlast_pct=ueberlast,
        rollover_vorbelastung=rollover,
        dringlichkeit_gewichte=base.dringlichkeit_gewichte,
    )


def render_woche_mode() -> None:
    szenarien = list_scenarios("scenarios")
    szen_namen = [p.stem for p in szenarien]
    szenario_name = st.sidebar.selectbox("Störungs-Szenario", szen_namen if szen_namen else ["baseline"])
    start_montag = st.sidebar.date_input("Wochenstart (Montag)", value=naechster_montag())
    auftraege_pro_woche = st.sidebar.slider(
        "Aufträge pro Woche",
        100,
        400,
        210,
        step=10,
        help="Gesamtzahl Aufträge über die Woche (inklusive Rollover-Altlast). "
             "Wird mit ±10 % Variation auf Mo–Fr verteilt. "
             "Wenn explizit gesetzt, **überschreibt** dieser Wert die Überlast-% aus dem Preset — "
             "der Slider bleibt also auch bei 'Chaos' genau bei deiner Eingabe.",
    )
    profil = _profil_slider()

    vergleich_auswahl = st.sidebar.multiselect(
        "Arena: welche Scheduler vergleichen?",
        ["Heuristik", "OR-Tools", "LLM", "Hybrid (LLM+OR-Tools)"],
        default=[],
        help="Leer lassen = nur der oben gewählte Scheduler läuft.",
    )

    if run or st.session_state.get("_last_week") is None:
        rp = HaversineRouteProvider()
        ev = load_scenario(f"scenarios/{szenario_name}.yaml", start_montag)

        ergebnisse: dict[str, WochenErgebnis] = {}
        hybrid_details: dict[str, dict] = {}
        if vergleich_auswahl:
            scheduler_list = [(name, _build_scheduler(name)) for name in vergleich_auswahl]
        else:
            scheduler_list = [(scheduler_name, _build_scheduler())]

        bekannte_global = None
        techs_global = None
        for label, sched in scheduler_list:
            rng_local = random.Random(int(seed))
            techs_local = generate_techniker(n_techs, rng_local)
            woche_local = generate_woche(
                start_montag,
                rng=rng_local,
                profil=profil,
                auftraege_pro_woche=int(auftraege_pro_woche),
            )
            if bekannte_global is None:
                bekannte_global = {a.id: a for tag_a in woche_local.values() for a in tag_a}
                techs_global = techs_local
            with st.spinner(f"Wochenlauf mit {label}…"):
                we_local = run_woche(woche_local, techs_local, sched, rp, ev, szenario_name)
            ergebnisse[label] = we_local
            if hasattr(sched, "last_reasoning") and sched.last_reasoning:
                hybrid_details[label] = {
                    "reasoning": sched.last_reasoning,
                    "weights": getattr(sched, "last_weights", None),
                    "usage": getattr(sched, "last_usage", None),
                }

        st.session_state["_last_week"] = dict(
            ergebnisse=ergebnisse,
            bekannte=bekannte_global,
            techs=techs_global,
            start=start_montag,
            szenario=szenario_name,
            vergleich=bool(vergleich_auswahl),
            hybrid_details=hybrid_details,
            profil=profil,
        )

    state = st.session_state["_last_week"]
    ergebnisse: dict[str, WochenErgebnis] = state["ergebnisse"]
    bekannte = state["bekannte"]

    if len(ergebnisse) > 1:
        _render_vergleich(ergebnisse, bekannte)
        st.divider()
        detail_label = st.selectbox("Detail-Ansicht für Scheduler", list(ergebnisse.keys()))
    else:
        detail_label = next(iter(ergebnisse.keys()))

    hybrid_details = state.get("hybrid_details") or {}
    if detail_label in hybrid_details:
        det = hybrid_details[detail_label]
        with st.expander("🤖 LLM-Entscheidung (Reasoning + Gewichte)", expanded=True):
            if det.get("reasoning"):
                st.markdown(f"**Reasoning:** {det['reasoning']}")
            if det.get("weights"):
                w = det["weights"]
                if hasattr(w, "__dict__"):
                    st.json(w.__dict__)
                else:
                    st.write(w)
            if det.get("usage"):
                st.caption(f"Token-Usage: {det['usage']}")

    we: WochenErgebnis = ergebnisse[detail_label]
    m = compute_metriken(we, bekannte)
    st.subheader(f"Detail: {detail_label}")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Aufträge generiert", m.generiert)
    c2.metric("Erledigt", f"{m.erledigt} ({m.completion_rate}%)")
    c3.metric("Storniert", m.storniert)
    c4.metric("Offen am Ende", m.offen)
    c5.metric("Auslastung", f"{m.auslastung_pct}%")

    c6, c7, c8 = st.columns(3)
    c6.metric("Prio-gew. Completion", f"{m.completion_prio_gewichtet}%")
    c7.metric("Notfälle erledigt", f"{m.notfaelle_erledigt}/{m.notfaelle_gesamt}")
    c8.metric("SLA-Verletzungen", m.sla_verletzungen)

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["Tagesüberblick", "Events", "Offen am Ende", "Gantt pro Tag", "Karte pro Tag"]
    )
    with tab1:
        df = tages_df(we)
        st.dataframe(df, use_container_width=True)
        fig = px.bar(
            df.melt(id_vars="datum", value_vars=["erledigt", "storniert", "rollover"]),
            x="datum",
            y="value",
            color="variable",
            barmode="group",
            height=360,
        )
        st.plotly_chart(fig, use_container_width=True)
    with tab2:
        ev_df = _events_df(we)
        if ev_df.empty:
            st.info("Keine Events in diesem Szenario.")
        else:
            st.dataframe(ev_df, use_container_width=True)
    with tab3:
        if we.offen_am_ende:
            st.dataframe(_auftraege_df(we.offen_am_ende, we.ergebnisse[-1].datum), use_container_width=True)
        else:
            st.success("Alle Aufträge erledigt oder storniert!")
    with tab4:
        tag_wahl = st.selectbox(
            "Tag wählen",
            [e.datum for e in we.ergebnisse],
            format_func=lambda d: d.strftime("%a %d.%m."),
            key="gantt_tag",
        )
        tag_erg = next(e for e in we.ergebnisse if e.datum == tag_wahl)
        _render_gantt(tag_erg.tourenplan, techs=state.get("techs"))
    with tab5:
        tag_wahl_k = st.selectbox(
            "Tag wählen",
            [e.datum for e in we.ergebnisse],
            format_func=lambda d: d.strftime("%a %d.%m."),
            key="karte_tag",
        )
        tag_erg_k = next(e for e in we.ergebnisse if e.datum == tag_wahl_k)
        _render_map(tag_erg_k.tourenplan, bekannte)


def _render_vergleich(ergebnisse: dict, bekannte: dict) -> None:
    st.subheader("Scheduler-Vergleich")
    rows = []
    for label, we in ergebnisse.items():
        m = compute_metriken(we, bekannte)
        rows.append(
            dict(
                scheduler=label,
                erledigt=m.erledigt,
                completion_pct=m.completion_rate,
                prio_completion_pct=m.completion_prio_gewichtet,
                fahrzeit_min=m.gesamtfahrzeit_min,
                auslastung_pct=m.auslastung_pct,
                notfall=f"{m.notfaelle_erledigt}/{m.notfaelle_gesamt}",
                sla_verletzungen=m.sla_verletzungen,
                offen=m.offen,
            )
        )
    df = pd.DataFrame(rows)
    st.dataframe(df, use_container_width=True)
    fig = px.bar(
        df.melt(id_vars="scheduler", value_vars=["completion_pct", "prio_completion_pct", "auslastung_pct"]),
        x="variable",
        y="value",
        color="scheduler",
        barmode="group",
        height=320,
    )
    st.plotly_chart(fig, use_container_width=True)


if mode == "Tag":
    render_tag_mode()
else:
    render_woche_mode()
