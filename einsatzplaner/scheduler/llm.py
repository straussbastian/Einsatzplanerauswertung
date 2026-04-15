from __future__ import annotations

import json
import os

import anthropic

from ..models import Auftrag, Tourenplan
from .base import PlanInput
from .heuristic import (
    HeuristicScheduler,
    _TechState,
    _commit,
    _finalize_tour,
    _try_assign,
)


DEFAULT_MODEL = "claude-opus-4-6"


SYSTEM_PROMPT = """Du bist ein erfahrener Einsatzdisponent für einen Heizungsbaubetrieb in Oldenburg (Oldb).

DEINE AUFGABE: Verteile die gegebenen Aufträge auf die Servicetechniker so, dass möglichst viele Aufträge erledigt werden — mit hoher Priorität bevorzugt und möglichst wenig Fahrtzeit.

ARBEITSREGELN (hart):
- Alle Techniker sind gleich qualifiziert und starten/enden am Betriebshof (53.1467, 8.1806)
- Schichtzeit: 8:00-16:00 (8h = 480 min)
- Davon 15 min Frühstückspause + 45 min Mittagspause = 60 min Pause
- Nettoarbeitszeit: 420 min pro Techniker
- Frühstückspause ab ~10:00, Mittagspause im Fenster 11:30-13:30
- Fahrzeiten: Luftlinie × 1.3 bei 50 km/h (~1.5 min pro km Luftlinie)
- Zeitfenster (fenster_von/fenster_bis) müssen respektiert werden — Auftrag muss in dem Fenster komplett ausführbar sein

PRIORISIERUNG (in dieser Reihenfolge):
1. Notfälle (notfall=true) — Kunden ohne Heizung, Vorrang vor allem anderen
2. Hohe Dringlichkeit (3) vor mittlerer (2) vor niedriger (1)
3. SLA-Frist ≤ 2 Tage — müssen heute erledigt werden
4. rollover_count > 0 (schon verschoben) bevorzugen
5. Bei gleicher Prio: geografische Nähe / kurze Fahrzeit

STRATEGIE:
- Bilde geografische Cluster pro Techniker — nah beieinander liegende Aufträge zusammen
- Reihenfolge pro Techniker nach Nachbarschaft (Nearest-Neighbor), nicht nach Prio
- Zeitfenster führen die Reihenfolge (fixe Termine sind Anker)
- Lieber 1-2 kleine Aufträge weglassen als einen weiten Umweg fahren
- Techniker gleich stark auslasten — keine Tour nur mit 2 Aufträgen, andere mit 8
- Rollover-Aufträge (rollover_count ≥ 1) unbedingt unterbringen, die werden sonst ewig verschleppt

AUSGABE: Rufe das Tool `submit_plan` genau einmal auf mit allen Zuordnungen.
- Nicht alle Aufträge müssen zugewiesen werden — unzuweisbare rollen auf den nächsten Tag
- Aber: zuweisen ist besser als liegen lassen, wenn es machbar ist
- Keine doppelten Auftrags-IDs über alle Techniker hinweg
- Nur gegebene Techniker-IDs verwenden"""


SUBMIT_PLAN_TOOL = {
    "name": "submit_plan",
    "description": "Reicht den finalen Tagesplan ein. Jeder Techniker bekommt eine Liste von Auftrags-IDs in Ausführungsreihenfolge.",
    "input_schema": {
        "type": "object",
        "properties": {
            "zuordnungen": {
                "type": "object",
                "description": "Map von Techniker-ID (z.B. 'T01') zu Liste von Auftrags-IDs in Reihenfolge.",
                "additionalProperties": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "reasoning": {
                "type": "string",
                "description": "Kurze Begründung der Strategie (max. 200 Wörter).",
            },
        },
        "required": ["zuordnungen", "reasoning"],
    },
}


def _auftrag_to_dict(a: Auftrag, datum) -> dict:
    d = {
        "id": a.id,
        "lat": round(a.lat, 4),
        "lon": round(a.lon, 4),
        "dauer_min": a.dauer_min,
        "prio": int(a.dringlichkeit),
        "notfall": a.notfall,
        "terminart": a.terminart,
        "rollover_count": a.rollover_count,
    }
    if a.fenster_von and a.fenster_bis:
        d["fenster"] = f"{a.fenster_von.strftime('%H:%M')}-{a.fenster_bis.strftime('%H:%M')}"
    if a.sla_frist:
        d["sla_in_tagen"] = (a.sla_frist - datum).days
    return d


def _build_user_prompt(pin: PlanInput) -> str:
    techs = [t.id for t in pin.techniker]
    auftraege = [_auftrag_to_dict(a, pin.datum) for a in pin.auftraege]
    return (
        f"Datum: {pin.datum.isoformat()}\n\n"
        f"TECHNIKER ({len(techs)}): {', '.join(techs)}\n\n"
        f"AUFTRÄGE ({len(auftraege)}):\n"
        f"{json.dumps(auftraege, ensure_ascii=False, indent=None)}\n\n"
        f"Erstelle den optimalen Einsatzplan mit dem Tool submit_plan."
    )


def _extract_tool_call(response) -> dict | None:
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_plan":
            return block.input
    return None


def _build_plan_from_assignments(
    pin: PlanInput,
    zuordnungen: dict[str, list[str]],
) -> Tourenplan:
    auftrag_by_id = {a.id: a for a in pin.auftraege}
    states = {t.id: _TechState(techniker=t, datum=pin.datum) for t in pin.techniker}
    tp = Tourenplan(datum=pin.datum)

    zugewiesen_gesehen: set[str] = set()

    for tech_id, tour_sequence in zuordnungen.items():
        state = states.get(tech_id)
        if state is None:
            continue
        for aid in tour_sequence:
            if aid in zugewiesen_gesehen:
                continue
            auftrag = auftrag_by_id.get(aid)
            if auftrag is None:
                continue
            ergebnis = _try_assign(state, auftrag, pin.route_provider)
            if ergebnis is None:
                continue
            _, ankunft, fahrzeit = ergebnis
            _commit(state, auftrag, ankunft, fahrzeit)
            zugewiesen_gesehen.add(aid)

    for tid, state in states.items():
        tp.touren[tid] = _finalize_tour(state, pin.route_provider)

    for a in pin.auftraege:
        if a.id not in zugewiesen_gesehen:
            tp.nicht_zugewiesen.append(a.id)

    return tp


class LLMScheduler:
    name = "llm"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 16_000,
        api_key: str | None = None,
        fallback: bool = True,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.fallback = fallback
        self.client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.last_reasoning: str | None = None
        self.last_usage: dict | None = None

    def plan(self, pin: PlanInput) -> Tourenplan:
        if not pin.auftraege:
            return _build_plan_from_assignments(pin, {})

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[SUBMIT_PLAN_TOOL],
                tool_choice={"type": "tool", "name": "submit_plan"},
                messages=[{"role": "user", "content": _build_user_prompt(pin)}],
            )
        except anthropic.AnthropicError as e:
            if self.fallback:
                print(f"[LLMScheduler] API-Fehler ({e}), Fallback auf Heuristik.")
                return HeuristicScheduler().plan(pin)
            raise

        self.last_usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        }

        tool_input = _extract_tool_call(response)
        if tool_input is None:
            if self.fallback:
                print("[LLMScheduler] Keine tool_use Antwort, Fallback auf Heuristik.")
                return HeuristicScheduler().plan(pin)
            return _build_plan_from_assignments(pin, {})

        self.last_reasoning = tool_input.get("reasoning")
        zuordnungen = tool_input.get("zuordnungen") or {}
        return _build_plan_from_assignments(pin, zuordnungen)
