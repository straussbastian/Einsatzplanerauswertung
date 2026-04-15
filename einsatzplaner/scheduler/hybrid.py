from __future__ import annotations

import json
import os
from collections import Counter

import anthropic

from ..models import Tourenplan
from .base import PlanInput
from .ortools_vrp import DEFAULT_WEIGHTS, ORToolsScheduler, PenaltyWeights


DEFAULT_MODEL = "claude-sonnet-4-6"


SYSTEM_PROMPT = """Du bist ein erfahrener Einsatzdisponent für einen Heizungsbaubetrieb.

Deine Aufgabe ist NICHT die Zuordnung der einzelnen Aufträge — das macht ein Optimierungs-Solver (OR-Tools VRPTW). Deine Aufgabe: die PRIORISIERUNGS-GEWICHTE für den Solver festlegen, basierend auf der heutigen Tageslage.

DER SOLVER SIEHT:
- Aufträge mit Feldern: dringlichkeit (1-3), notfall (bool), rollover_count (int), sla_frist (Datum)
- Fahrtzeiten zwischen allen Orten

FÜR JEDEN NICHT ZUGEWIESENEN AUFTRAG ZAHLT DER SOLVER EINE PENALTY:
  penalty = base_penalty
         + dringlichkeit_multiplier × dringlichkeit
         + notfall_bonus    (falls notfall=true)
         + rollover_multiplier × rollover_count
         + sla_today_bonus  (falls SLA-Frist ≤ heute)
         + sla_soon_bonus   (falls SLA-Frist in 1-2 Tagen)

Die Fahrtzeit-Kosten gehen als separate Kostenkomponente ein, skaliert mit travel_weight_pct.

DEINE STRATEGISCHE FRAGE: welche dieser Faktoren ist HEUTE wichtiger?
- Stapeln sich Rollover? → rollover_multiplier hoch, damit der Solver sie nicht weiter verschleppt
- Viele Notfälle? → notfall_bonus hoch
- Viele SLA-kritische? → sla_today_bonus hoch
- Wenige Aufträge, ruhige Lage? → travel_weight_pct hoch (mehr Fokus auf Effizienz)
- Chaos-Woche mit Krankmeldung? → base_penalty hoch (alles durchdrücken, Fahrten egal)

RICHTWERTE (damit der Solver eine Balance findet):
- base_penalty: 30000-100000 (Standard: 50000)
- dringlichkeit_multiplier: 5000-50000 (Standard: 10000)
- notfall_bonus: 50000-500000 (Standard: 100000)
- rollover_multiplier: 2000-30000 (Standard: 5000)
- sla_today_bonus: 20000-200000 (Standard: 50000)
- sla_soon_bonus: 5000-50000 (Standard: 15000)
- travel_weight_pct: 50-300 (100=Standard, höher=Fahrtzeit wichtiger, niedriger=Anzahl Aufträge wichtiger)

FAUSTREGEL: Wenn du einen Faktor stark machst, mache die anderen nicht gleich stark — sonst neutralisiert sich alles. Setze Kontraste.

INTRADAY-REPLAN: Bei einem Intraday-Replan (Feld `replan_kontext` im Input gesetzt) bekommst du zusätzlich Tagesverlaufs-Informationen: wie oft heute schon replant wurde, welches Ereignis den aktuellen Replan ausgelöst hat, wie viele Stops bis jetzt erledigt sind, und pro Techniker die verbleibende Schichtzeit sowie die noch geplante Arbeit. Diese Angaben sind rein deskriptiv — es ist deine Aufgabe zu entscheiden, ob und wie sie deine Gewichts-Wahl beeinflussen.

DIAGNOSTIK: Wenn `replan_kontext` gesetzt ist, vermerke in deinem `reasoning` EXPLIZIT, ob und warum die Replan-Kontext-Felder deine Wahl beeinflusst haben — auch wenn die Antwort „nicht relevant" ist. Das macht im Nachhinein nachvollziehbar, ob du die Information überlegt eingeordnet oder nur durchgewunken hast.

Rufe das Tool `submit_weights` GENAU EINMAL auf mit deinen Gewichten und einer Begründung."""


SUBMIT_WEIGHTS_TOOL = {
    "name": "submit_weights",
    "description": "Reicht die Priorisierungs-Gewichte für den OR-Tools-Solver ein.",
    "input_schema": {
        "type": "object",
        "properties": {
            "base_penalty": {"type": "integer", "description": "Basis-Kosten pro gedropptem Auftrag (30000-100000)."},
            "dringlichkeit_multiplier": {"type": "integer", "description": "Multiplikator × Dringlichkeit (5000-50000)."},
            "notfall_bonus": {"type": "integer", "description": "Extra-Penalty für Notfälle (50000-500000)."},
            "rollover_multiplier": {"type": "integer", "description": "Pro Tag Rollover (2000-30000)."},
            "sla_today_bonus": {"type": "integer", "description": "Extra-Penalty wenn SLA ≤ heute (20000-200000)."},
            "sla_soon_bonus": {"type": "integer", "description": "Extra-Penalty wenn SLA in 1-2 Tagen (5000-50000)."},
            "travel_weight_pct": {"type": "integer", "description": "Fahrtzeit-Gewicht in % (50=halbiert, 100=normal, 300=verdreifacht)."},
            "reasoning": {"type": "string", "description": "Kurze Begründung der gewählten Strategie (max. 150 Wörter)."},
        },
        "required": [
            "base_penalty",
            "dringlichkeit_multiplier",
            "notfall_bonus",
            "rollover_multiplier",
            "sla_today_bonus",
            "sla_soon_bonus",
            "travel_weight_pct",
            "reasoning",
        ],
    },
}


def _tages_statistik(pin: PlanInput) -> dict:
    auftraege = pin.auftraege
    dring_counter: Counter[int] = Counter()
    for a in auftraege:
        dring_counter[int(a.dringlichkeit)] += 1

    notfaelle = sum(1 for a in auftraege if a.notfall)
    rollover_max = max((a.rollover_count for a in auftraege), default=0)
    rollover_dist = Counter(a.rollover_count for a in auftraege)
    sla_today = sum(1 for a in auftraege if a.sla_frist and (a.sla_frist - pin.datum).days <= 0)
    sla_soon = sum(
        1
        for a in auftraege
        if a.sla_frist and 0 < (a.sla_frist - pin.datum).days <= 2
    )
    fixe_termine = sum(1 for a in auftraege if a.terminart == "fix")
    gewerblich = sum(1 for a in auftraege if a.typ == "gewerblich")
    gesamt_dauer = sum(a.dauer_min for a in auftraege)
    aktive_techs = len(pin.techniker) - len(pin.ausgeschlossene_techs)

    if pin.ist_replan and pin.replan_ab is not None:
        schicht_ende_min = 16 * 60
        replan_ab_min = pin.replan_ab.hour * 60 + pin.replan_ab.minute
        rest_brutto = max(0, schicht_ende_min - replan_ab_min)
        # Grobe Schätzung: 60min Pausen werden durchschnittlich schon teilweise
        # erledigt sein. Daher Netto ~= Brutto * 0.85 (konservativer Durchschnitt).
        kapazitaet_min = aktive_techs * max(1, int(rest_brutto * 0.85))
    else:
        kapazitaet_min = aktive_techs * 420

    stats = {
        "datum": pin.datum.isoformat(),
        "wochentag": pin.datum.strftime("%A"),
        "auftraege_gesamt": len(auftraege),
        "techniker": aktive_techs,
        "kapazitaet_min": kapazitaet_min,
        "gesamt_auftragsdauer_min": gesamt_dauer,
        "auslastungs_druck_pct": round(100 * gesamt_dauer / kapazitaet_min, 1) if kapazitaet_min else 0.0,
        "dringlichkeit_1": dring_counter.get(1, 0),
        "dringlichkeit_2": dring_counter.get(2, 0),
        "dringlichkeit_3": dring_counter.get(3, 0),
        "notfaelle": notfaelle,
        "sla_heute_oder_ueber": sla_today,
        "sla_in_1_2_tagen": sla_soon,
        "rollover_max_tage": rollover_max,
        "rollover_verteilung": dict(sorted(rollover_dist.items())),
        "fixe_termine": fixe_termine,
        "gewerbliche_auftraege": gewerblich,
    }

    if pin.ist_replan and pin.replan_ab is not None:
        kontext_block: dict = {
            "replan_zeitpunkt": pin.replan_ab.strftime("%H:%M"),
            "ausgeschlossene_techniker": sorted(pin.ausgeschlossene_techs),
        }
        rk = pin.replan_kontext
        if rk is not None:
            kontext_block["trigger_event"] = rk.trigger_event_typ
            kontext_block["replanungen_heute_bisher"] = rk.replanungen_heute_bisher
            kontext_block["bisher_erledigt_heute"] = rk.bisher_erledigt_heute
            kontext_block["rest_schicht_pro_tech_min"] = rk.rest_schicht_pro_tech_min
            kontext_block["pending_pro_tech_min"] = rk.pending_pro_tech_min

        kontext_block["hinweis"] = (
            "Das ist ein INTRADAY-REPLAN — ein Ereignis hat den morgendlichen Plan "
            "gestört. Die angegebenen Aufträge sind nur die noch nicht begonnenen; "
            "die aktive Techniker-Zahl ist reduziert um die ausgefallenen. Nutze "
            "`replanungen_heute_bisher` und `trigger_event` um zu entscheiden, wie "
            "aggressiv du umverteilen willst (Details im System-Prompt)."
        )
        stats["replan_kontext"] = kontext_block

    return stats


def _extract_weights_call(response):
    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_weights":
            return block.input
    return None


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


def _weights_from_llm(raw: dict) -> PenaltyWeights:
    return PenaltyWeights(
        base_penalty=_clamp(raw.get("base_penalty", 50_000), 10_000, 200_000),
        dringlichkeit_multiplier=_clamp(raw.get("dringlichkeit_multiplier", 10_000), 1_000, 100_000),
        notfall_bonus=_clamp(raw.get("notfall_bonus", 100_000), 0, 1_000_000),
        rollover_multiplier=_clamp(raw.get("rollover_multiplier", 5_000), 0, 100_000),
        sla_today_bonus=_clamp(raw.get("sla_today_bonus", 50_000), 0, 500_000),
        sla_soon_bonus=_clamp(raw.get("sla_soon_bonus", 15_000), 0, 200_000),
        travel_weight_pct=_clamp(raw.get("travel_weight_pct", 100), 10, 500),
    )


class LLMGuidedVRPScheduler:
    name = "hybrid"

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        time_limit_sec: int = 5,
        api_key: str | None = None,
        fallback: bool = True,
    ):
        self.model = model
        self.time_limit_sec = time_limit_sec
        self.fallback = fallback
        self.client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))
        self.last_reasoning: str | None = None
        self.last_weights: PenaltyWeights | None = None
        self.last_usage: dict | None = None

    def plan(self, pin: PlanInput) -> Tourenplan:
        stats = _tages_statistik(pin)
        user_prompt = (
            "TAGESLAGE:\n"
            f"{json.dumps(stats, ensure_ascii=False, indent=2)}\n\n"
            "Setze die Gewichte so, dass der Solver die heute wichtigsten Aufträge bevorzugt."
        )

        weights: PenaltyWeights
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4_000,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[SUBMIT_WEIGHTS_TOOL],
                tool_choice={"type": "tool", "name": "submit_weights"},
                messages=[{"role": "user", "content": user_prompt}],
            )
            self.last_usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            }
            raw = _extract_weights_call(response)
            if raw is None:
                if not self.fallback:
                    raise RuntimeError("LLM lieferte keinen submit_weights Tool-Aufruf.")
                print("[HybridScheduler] LLM lieferte keine Gewichte, nehme Default.")
                weights = DEFAULT_WEIGHTS
                self.last_reasoning = "Fallback auf Default-Gewichte"
            else:
                self.last_reasoning = raw.get("reasoning")
                weights = _weights_from_llm(raw)
        except anthropic.AnthropicError as e:
            if not self.fallback:
                raise
            print(f"[HybridScheduler] API-Fehler ({e}), nehme Default-Gewichte.")
            weights = DEFAULT_WEIGHTS
            self.last_reasoning = f"API-Fehler, Default-Gewichte: {e}"

        self.last_weights = weights
        return ORToolsScheduler(time_limit_sec=self.time_limit_sec, weights=weights).plan(pin)
