# Technische Dokumentation: Einsatzplaner

**Zielgruppe:** Entwickler, die den Code lesen, erweitern oder operative Fragen beantworten müssen.
**Lesereihenfolge:** Diese Datei setzt die fachliche Auswertung in [auswertung.md](./auswertung.md) voraus. Hier geht es um Architektur, Datenflüsse und Betriebs-Details.

---

## 1. Projektstruktur

```
Einsatzplaner/
├── app.py                              # Streamlit-Einstieg (Tag-/Wochenmodus)
├── requirements.txt
├── .env                                # ANTHROPIC_API_KEY (nicht versioniert)
├── einsatzplaner/
│   ├── __init__.py
│   ├── models.py                       # Dataclasses: Auftrag, Techniker, Stop, Tour, …
│   ├── generator.py                    # Szenarioprofil + Auftrags-/Wochen-Generator
│   ├── geo.py                          # RouteProvider-Interface (Haversine-Impl)
│   ├── simulator.py                    # Wochenlauf mit Rollover + Event-Anwendung
│   ├── disruptions.py                  # YAML-Loader für Störungsszenarien
│   ├── evaluator.py                    # Metriken + Vergleichs-DataFrames
│   ├── visualization.py                # Gantt-Transformer: Pausen mitten im Auftrag
│   └── scheduler/
│       ├── __init__.py                 # Re-Export der 4 Scheduler
│       ├── base.py                     # Scheduler-Protocol + PlanInput
│       ├── heuristic.py                # Insertion-Greedy
│       ├── ortools_vrp.py              # VRPTW mit Disjunction-Penalties
│       ├── llm.py                      # LLM-Direct (Tool-Use)
│       └── hybrid.py                   # LLM setzt Penalty-Gewichte, OR-Tools löst
├── scenarios/                          # Störungsszenarien als YAML
│   ├── baseline.yaml
│   ├── sick_leave.yaml
│   ├── cancellations.yaml
│   ├── mixed.yaml
│   └── chaos.yaml                      # inkl. Auftragsverlängerungen
└── docs/
    ├── auswertung.md                   # Wissenschaftliche Auswertung
    └── technik.md                      # Diese Datei
```

## 2. Datenmodell

Alle Datenklassen sind in [einsatzplaner/models.py](../einsatzplaner/models.py) definiert und frozen/dataclass-basiert (ohne `frozen=True`, weil Simulator und Scheduler in-place Status ändern).

### 2.1 Auftrag

```python
@dataclass
class Auftrag:
    id: str                              # "A0001"
    kunde: str
    typ: Literal["privat", "gewerblich"]
    adresse: str
    lat: float
    lon: float
    dauer_min: int
    dringlichkeit: Dringlichkeit         # Enum 1..3
    terminart: Literal["flexibel", "fix"]
    fenster_von: time | None = None
    fenster_bis: time | None = None
    sla_frist: date | None = None
    notfall: bool = False
    rollover_count: int = 0
    created_at: datetime = field(default_factory=datetime.now)
```

### 2.2 Techniker

```python
@dataclass
class Techniker:
    id: str                              # "T01".."T10"
    name: str
    home_lat: float = BETRIEBSHOF_LAT    # 53.146661
    home_lon: float = BETRIEBSHOF_LON    # 8.180577
    schichtbeginn: time = time(8, 0)
    schichtende: time = time(16, 0)
    pause_fruehstueck_min: int = 15
    pause_mittag_min: int = 45
    max_arbeit_ohne_pause_min: int = 360
    mittag_fenster_von: time = time(11, 30)
    mittag_fenster_bis: time = time(13, 30)
```

### 2.3 Stop, Tour, Tourenplan

`Stop` trägt `typ: StopTyp` (`DEPOT_START`, `AUFTRAG`, `PAUSE_FRUEHSTUECK`, `PAUSE_MITTAG`, `DEPOT_ENDE`) und `status: Literal["geplant", "erledigt", "storniert", "nicht_ausgefuehrt"]`. Nach Simulation markiert der Simulator jeden Stop mit dem endgültigen Status.

`Tour = {techniker_id, datum, stops: list[Stop]}` mit Properties `auftrag_ids`, `gesamt_fahrzeit_min`, `gesamt_arbeitszeit_min`.

`Tourenplan = {datum, touren: dict[tech_id, Tour], nicht_zugewiesen: list[auftrag_id]}`.

### 2.4 Störungsereignisse

```python
class EventTyp(str, Enum):
    TECHNIKER_KRANK     = "techniker_krank"
    KUNDE_ABSAGE        = "kunde_absage"
    NOTFALL             = "notfall"
    STAU                = "stau"
    AUFTRAG_VERLAENGERT = "auftrag_verlaengert"  # Auftrag dauert unerwartet länger

@dataclass
class Stoerung:
    typ: EventTyp
    zeitpunkt: datetime
    techniker_id: str | None = None
    auftrag_id: str | None = None
    notfall_auftrag: Auftrag | None = None
    stau_dauer_min: int = 0
    extra_min: int = 0                   # für AUFTRAG_VERLAENGERT: zusätzliche Minuten
    betroffene_techniker: list[str] = field(default_factory=list)
```

## 3. Scheduler-Interface

```python
# einsatzplaner/scheduler/base.py

@dataclass
class PlanInput:
    datum: date
    techniker: list[Techniker]
    auftraege: list[Auftrag]
    route_provider: RouteProvider

class Scheduler(Protocol):
    name: str
    def plan(self, pin: PlanInput) -> Tourenplan: ...
```

Alle vier Scheduler implementieren dasselbe Protokoll. Das macht sie austauschbar im Simulator und erlaubt die Arena-Vergleiche in der App.

## 4. Scheduler-Details

### 4.1 Heuristik — [scheduler/heuristic.py](../einsatzplaner/scheduler/heuristic.py)

**Einstiegspunkt:** `HeuristicScheduler.plan(pin)`.

Interner Zustand pro Techniker: `_TechState(techniker, datum, stops, fruehstueck_gesetzt, mittag_gesetzt, arbeit_seit_letzter_pause_min)`. Die Funktionen `_try_assign`, `_commit`, `_place_pausen_if_due` und `_finalize_tour` bilden die Kernlogik.

**Prio-Score** (`prio_score`):

```python
score = 10 × dringlichkeit + 50 × notfall + 5 × rollover_count + max(0, 10 - tage_bis_sla)
```

**Schichtende-Reserve**: In `_try_assign` wird das effektive Schichtende reduziert um:
- ausstehende Pausen, die nach Auftrags-Ankunft noch fällig sind
- Fahrzeit vom neuen Stop zurück zum Depot

Das verhindert Überstunden, die in einer früheren Version bis zu 83 min betrugen.

### 4.2 OR-Tools VRPTW — [scheduler/ortools_vrp.py](../einsatzplaner/scheduler/ortools_vrp.py)

**Einstiegspunkt:** `ORToolsScheduler(time_limit_sec: int, weights: PenaltyWeights).plan(pin)`.

Die Penalty-Gewichte sind in `PenaltyWeights` gebündelt (Default in `DEFAULT_WEIGHTS`):

```python
@dataclass
class PenaltyWeights:
    base_penalty: int = 50_000
    dringlichkeit_multiplier: int = 10_000
    notfall_bonus: int = 100_000
    rollover_multiplier: int = 5_000
    sla_today_bonus: int = 50_000
    sla_soon_bonus: int = 15_000
    travel_weight_pct: int = 100  # 100 = 1:1, 50 = halbiert, 200 = verdoppelt
```

**OR-Tools-Modellierung:**

```
Knoten         = [Depot, Auftrag_1, Auftrag_2, ...]
Fahrzeuge      = 10 Techniker, alle Start+End = Depot (Index 0)
Distance-Matrix = Haversine × 1.3 / 50 km/h (int Minuten)

Cost-Callback      (Arc-Cost): matrix[i][j] × travel_weight_pct / 100
Time-Callback      (Dimension): matrix[i][j] + service_time[i]

Time-Dimension:
    Horizon = 600 min (Puffer über Schicht)
    Slack   = 600 min (erlaubt Wartezeiten auf Zeitfenster)
    Start-CumulVar pro Fahrzeug: [0, 0]
    End-CumulVar   pro Fahrzeug: [0, 420]   # 420 = NETTO_ARBEITS_MIN

Disjunction pro Auftrag:
    penalty = base_penalty
            + dringlichkeit_multiplier × dringlichkeit
            + notfall_bonus    (falls notfall)
            + rollover_multiplier × rollover_count
            + sla_today_bonus  (falls sla_frist ≤ heute)
            + sla_soon_bonus   (falls sla_frist in 1..2 Tagen)

Search-Parameters:
    FirstSolution = PATH_CHEAPEST_ARC
    LocalSearch   = GUIDED_LOCAL_SEARCH
    TimeLimit     = 4 s
```

**Pausen außerhalb des Solvers:** Der Solver selbst rechnet nur mit 420 min Nettoarbeitszeit. Die Pause-Stops werden in `_solution_to_plan` deterministisch eingefügt: Frühstück ab Minute 120 (10:00), Mittag im Fenster Minute 210–330 (11:30–13:30). Die Lösung wird entsprechend zeitlich nach hinten geshiftet (`shift_offset`).

**Warum nicht `SetBreakIntervalsOfVehicle`?** Frühe Experimente zeigten, dass IntervalVar-basierte Pausen den Solver blockieren: statt 37/45 Aufträgen wurden nur 32 zugewiesen, auch bei 20 s Zeitlimit. Die post-hoc Einfügung ist zwar nicht optimal (der Solver sieht die Pause nicht), aber für unsere Auftragsdauern und -dichten ist der Fehler vernachlässigbar.

### 4.3 LLM-Direct — [scheduler/llm.py](../einsatzplaner/scheduler/llm.py)

**Einstiegspunkt:** `LLMScheduler(model: str, max_tokens: int, api_key: str, fallback: bool).plan(pin)`.

**Ablauf:**

```
1. _build_user_prompt(pin):
   -> JSON mit Datum, Techniker-IDs, Aufträge (kompakt)

2. client.messages.create(
       model="claude-sonnet-4-6",
       system=[{"type": "text", "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"}}],
       tools=[SUBMIT_PLAN_TOOL],
       tool_choice={"type": "tool", "name": "submit_plan"},
       messages=[{"role": "user", "content": prompt}],
       max_tokens=16_000,
   )

3. _extract_tool_call(response):
   -> {"zuordnungen": {tech_id: [auftrag_id, ...]}, "reasoning": "..."}

4. _build_plan_from_assignments:
   -> für jeden Techniker, für jede Auftrags-ID in Reihenfolge:
      _try_assign(state, auftrag) aus heuristic.py
      _commit wenn feasibel
   -> nicht-feasible Aufträge landen in tp.nicht_zugewiesen

5. Fallback bei Fehler:
   -> HeuristicScheduler().plan(pin)
```

Siehe [auswertung.md §4](./auswertung.md#4-llm-einbindung) für den wörtlichen Prompt und das Tool-Schema.

**Wichtig:** Die deterministische Tour-Konstruktion aus Schritt 4 (`_try_assign`/`_commit`) stellt sicher, dass der LLM keine invaliden Pläne produzieren kann — er bestimmt nur Zuweisung und Reihenfolge, die Zeit-/Pausen-Constraints werden beim Bauen der Tour geprüft. Aufträge, die nicht passen (z.B. wegen Zeitfenster-Konflikt), werden still verworfen.

### 4.4 Hybrid — [scheduler/hybrid.py](../einsatzplaner/scheduler/hybrid.py)

**Einstiegspunkt:** `LLMGuidedVRPScheduler(model: str, time_limit_sec: int, api_key: str, fallback: bool).plan(pin)`.

**Ablauf:**

```
1. _tages_statistik(pin):
   -> dict mit datum, wochentag, auftraege_gesamt, kapazitaet_min,
      auslastungs_druck_pct, dringlichkeit_{1,2,3}, notfaelle,
      sla_heute_oder_ueber, sla_in_1_2_tagen, rollover_verteilung,
      fixe_termine, gewerbliche_auftraege

2. client.messages.create(
       model="claude-sonnet-4-6",
       system=[{...SYSTEM_PROMPT..., "cache_control": {...}}],
       tools=[SUBMIT_WEIGHTS_TOOL],
       tool_choice={"type": "tool", "name": "submit_weights"},
       messages=[{"role": "user", "content": json.dumps(stats)}],
       max_tokens=4_000,
   )

3. _weights_from_llm(raw):
   -> PenaltyWeights mit Clamping auf {10k..200k, 1k..100k, ...}

4. ORToolsScheduler(time_limit_sec, weights).plan(pin)

5. Fallback bei Fehler oder fehlendem Tool-Aufruf:
   -> ORToolsScheduler(time_limit_sec, DEFAULT_WEIGHTS).plan(pin)
```

**Clamping** ist wichtig: der LLM könnte theoretisch extreme Werte liefern (z.B. `base_penalty=1_000_000_000`), die den Solver destabilisieren würden. `_clamp(value, lo, hi)` begrenzt jede Dimension auf plausible Bereiche.

## 5. Generator

[einsatzplaner/generator.py](../einsatzplaner/generator.py)

### 5.1 Szenarioprofil

```python
@dataclass
class Szenarioprofil:
    notfall_rate: float = 0.02
    sla_druck_rate: float = 0.10
    ueberlast_pct: int = 100
    rollover_vorbelastung: int = 0
    dringlichkeit_gewichte: tuple[float, float, float] = (0.5, 0.35, 0.15)
    # Intraday-Störungen werden pro Tag seed-gesteuert gewürfelt
    # (zusätzlich zu YAML-Events, additiv):
    intraday_krank_rate: float = 0.0         # pro Techniker/Tag
    intraday_verlaengerung_rate: float = 0.0 # pro Auftrag
    intraday_absage_rate: float = 0.0        # pro Auftrag
    intraday_stau_rate: float = 0.0          # pro Tag

    @classmethod
    def presets(cls) -> dict[str, "Szenarioprofil"]:
        # 5 Standard-Presets + "Realistisch" mit moderaten Intraday-Raten
        ...
```

Der `Chaos`-Preset enthält auch Intraday-Raten (krank 15 %, Verlängerung 4 %, Absage 2 %, Stau 30 %). `Realistisch` ist moderater (120 % Überlast, 5 % Krank, 2 % Verlängerung, 15 % Absage, 20 % Stau) und eignet sich für Alltags-Tests.

### 5.2 Generator-Funktionen

```python
generate_techniker(n: int, rng: random.Random | None) -> list[Techniker]
generate_auftraege(n: int, tag: date, seq_start: int,
                   rng: random.Random | None,
                   profil: Szenarioprofil | None) -> list[Auftrag]
generate_woche(start_montag: date,
               auftraege_pro_tag: list[int] | None,
               auftraege_pro_woche: int | None,
               rng: random.Random | None,
               profil: Szenarioprofil | None) -> dict[date, list[Auftrag]]
verteile_auftraege_auf_woche(n_gesamt: int,
                             rng: random.Random) -> list[int]
generate_multiprofil_woche(start_montag: date,
                           profile_pro_tag: list[Szenarioprofil],
                           basis_auftraege_pro_tag: int = 45,
                           rng: random.Random | None,
                           ) -> tuple[dict[date, list[Auftrag]],
                                      dict[date, Szenarioprofil]]
```

Der Generator skaliert `auftraege_pro_tag` mit `ueberlast_pct / 100` und fügt bei `rollover_vorbelastung > 0` zusätzliche Aufträge mit `rollover_count ∈ {1,2,3}` und SLA-Frist ≤ Wochenstart in den Montag ein.

`verteile_auftraege_auf_woche(n_gesamt, rng)` verteilt eine Gesamt-Wochenmenge auf 5 Tage mit ±10 % Streuung. Wird als Alternative zu expliziten Tagesmengen genutzt, wenn der Anwender im UI nur den Wochen-Gesamtwert einstellt.

**`generate_multiprofil_woche`** nimmt eine Liste von Profilen (eines pro Tag) und erzeugt eine Woche mit wechselndem Lastprofil. Gibt zusätzlich das Tag→Profil-Mapping zurück, das der Simulator für die stochastische Intraday-Event-Generierung benötigt.

**Determinismus:** Gleicher Seed + gleiches Profil ⇒ identische Auftragsmenge. Der Scheduler erhält dieselbe Woche in jeder Arena-Kombination.

## 6. Simulator

[einsatzplaner/simulator.py](../einsatzplaner/simulator.py)

### 6.1 Pro-Tag-Ablauf

```python
def run_tag(datum, techniker, auftraege, scheduler, route_provider, events_heute):
    pin = PlanInput(datum, techniker, auftraege, route_provider)
    tp = scheduler.plan(pin)                             # 1. Scheduler plant
    rollover, storniert, events = _apply_events(tp, e)   # 2. Events anwenden
    erledigt = _classify_stops(tp)                       # 3. Rest = erledigt
    return TagesErgebnis(datum, tp, erledigt, storniert, rollover, events, ...)
```

**Event-Handler** (`_apply_techniker_krank`, `_apply_kunde_absage`, `_apply_stau`, `_apply_auftrag_verlaengert`) mutieren `Stop.status` von `"geplant"` auf `"storniert"` bzw. `"nicht_ausgefuehrt"`. Nach der Event-Phase markiert `_classify_stops` alle noch geplanten Stops als `"erledigt"`.

**Verlängerungs-Handler** (`_apply_auftrag_verlaengert`): findet den Auftrag in seiner Tour, addiert `extra_min` auf `stop.ende`, shiftet alle nachfolgenden Stops derselben Tour um `extra_min` nach hinten. Stops, die dadurch über 16:00 rutschen, werden `nicht_ausgefuehrt` und rollen. Diese intraday-Shift-Logik ist der primäre Stresstest für die Priorisierung: ein einziger verlängerter Auftrag am Vormittag kann ganze Nachmittagstouren in den Rollover schieben.

### 6.2 Stochastische Intraday-Event-Injektion

`run_woche` akzeptiert optional `profil_pro_tag: dict[date, Szenarioprofil]` und `intraday_seed: int`. Wenn gesetzt, ruft der Simulator vor Anwendung der YAML-Events `generate_intraday_events()` auf, das pro Tag zusätzliche Störungen aus den Profil-Raten würfelt:

```python
# aus disruptions.py
def generate_intraday_events(tag, auftraege_heute, techniker, profil, rng) -> list[Stoerung]:
    # für jeden Techniker: P(krank) = profil.intraday_krank_rate
    # für jeden Auftrag:   P(verlängerung) = profil.intraday_verlaengerung_rate
    # für jeden Auftrag:   P(absage)       = profil.intraday_absage_rate
    # für den Tag:         P(stau)         = profil.intraday_stau_rate
    ...
```

Der `intraday_seed` ist unabhängig vom Auftragsgenerator-Seed; beide sind deterministisch. Für den Multi-Seed-Benchmark wird typischerweise `intraday_seed = seed + 10_000` verwendet.

### 6.3 Wochenlauf mit Rollover

```python
def run_woche(woche_auftraege, techniker, scheduler, route_provider,
              stoerungen, szenario):
    backlog = []                                    # rollover vom Vortag
    bekannte_auftraege = {}                         # id -> Auftrag
    ergebnisse = []
    for tag in sorted(woche_auftraege):
        auftraege_heute = backlog + woche_auftraege[tag]
        bekannte_auftraege.update({a.id: a for a in auftraege_heute})
        events_heute = [e for e in stoerungen if e.zeitpunkt.date() == tag]
        ergebnis = run_tag(tag, techniker, auftraege_heute,
                           scheduler, route_provider, events_heute)
        ergebnisse.append(ergebnis)
        backlog = [bekannte_auftraege[aid] for aid in ergebnis.rollover]
        for a in backlog: a.rollover_count += 1
    return WochenErgebnis(ergebnisse, offen_am_ende=backlog, szenario=...)
```

## 6b. Multi-Seed-Benchmark (`bench/multiseed_benchmark.py`)

CLI-Skript für den Hauptbenchmark der Auswertung (§5.5). Konfiguration via argparse:

```bash
.venv/bin/python bench/multiseed_benchmark.py \
    --seeds 20 \
    --llm-model claude-sonnet-4-6 \
    --solver-time-limit 3 \
    --out bench/results_multiseed.csv
```

Flags:
- `--skip-hybrid` — LLM-Call auslassen (API-Kosten sparen)
- `--skip-intraday` — stochastische Events aus

Das Skript iteriert über Seeds und Scheduler, führt pro Kombination eine komplette Multi-Profil-Woche aus und schreibt sowohl Rohdaten (`results_multiseed.csv`) als auch aggregierte Kennzahlen (`results_multiseed_aggregated.csv`) weg.

Scheduler-Set:
- `heuristik`
- `ortools-normal` (DEFAULT_WEIGHTS)
- `ortools-chaos-safe` (CHAOS_SAFE_WEIGHTS: hohe Penalties)
- `ortools-sla-boost` (SLA_BOOST_WEIGHTS: SLA-Bonus hoch)
- `hybrid` (LLM wählt Gewichte pro Tag)

## 7. Evaluator

[einsatzplaner/evaluator.py](../einsatzplaner/evaluator.py)

Kern-Funktion:

```python
compute_metriken(we: WochenErgebnis,
                 auftraege_bekannt: dict[str, Auftrag]) -> Metriken
```

`Metriken` ist eine Dataclass mit 14 Feldern (siehe [auswertung.md §2.5](./auswertung.md#25-metriken)). `vergleichs_df(list[Metriken]) -> pd.DataFrame` erzeugt die tabellarische Übersicht für die App.

## 8. UI

[app.py](../app.py), Streamlit mit Sidebar + Tabs.

### 8.1 Zwei Modi

- **Tag**: plant einen einzelnen Tag, zeigt Gantt, Karte, Techniker-Übersicht. Kein Rollover, keine Events.
- **Woche**: voller Wochenlauf mit Szenario, Profil, Störungen, Arena-Vergleich.

### 8.2 Sidebar-Steuerung (Woche-Modus)

```
Modus [Tag | Woche]
Random Seed
Techniker (5..15)
Scheduler [Heuristik | OR-Tools | LLM | Hybrid]
LLM Modell [claude-opus-4-6 | claude-sonnet-4-6 | claude-haiku-4-5]
Planen (Button)
---
Störungs-Szenario (baseline / sick_leave / ...)
Wochenstart
Intensitäts-Preset (Normal / Hochlast / Notfallwoche / SLA-Katastrophe / Chaos / Manuell)
  +- Notfall-Rate (0-50%)
  +- SLA-Druck (0-60%)
  +- Überlast (50-300%)
  +- Rollover-Altlast (0-40)
Arena: multiselect [Heuristik, OR-Tools, LLM, Hybrid]
```

Die Slider haben dynamische Keys (`key=f"nf_{preset_name}"`), damit ein Preset-Wechsel die Werte zurücksetzt, aber manuelle Änderungen erhalten bleiben, solange das Preset gleich bleibt.

### 8.3 Ergebnisanzeige

- Metrik-Header (Erledigt, Completion, SLA, Auslastung, Fahrt)
- Tabs: Tagesüberblick, Events, Offen am Ende, Gantt pro Tag, Karte pro Tag
- Bei Arena-Vergleich: zusätzliche Vergleichs-Tabelle und Balkendiagramme
- Bei LLM/Hybrid-Scheduler: Expander mit Reasoning, Gewichten, Token-Usage

## 9. Tech-Stack & Abhängigkeiten

```
Python 3.14
streamlit       >= 1.30
pandas          >= 2.0
numpy           >= 1.26
plotly          >= 5.20
folium          >= 0.15
streamlit-folium>= 0.18
pyyaml          >= 6.0
python-dateutil >= 2.8
pytest          >= 8.0
ortools         >= 9.10       # VRPTW-Solver
anthropic       >= 0.40       # Claude API SDK
python-dotenv   >= 1.0        # .env-Loader
```

## 10. Entwicklungs-Workflows

### 10.1 App starten

```bash
.venv/bin/streamlit run app.py --server.port 8765 --server.headless true
```

### 10.2 Arena-Benchmark (CLI-Skript)

Aus der Projektwurzel:

```bash
.venv/bin/python -c "$(cat <<'PY'
from dotenv import load_dotenv; load_dotenv()
from datetime import date
import random, time as pt
from einsatzplaner.generator import (
    generate_techniker, generate_woche, Szenarioprofil)
from einsatzplaner.geo import HaversineRouteProvider
from einsatzplaner.scheduler.heuristic import HeuristicScheduler
from einsatzplaner.scheduler.ortools_vrp import ORToolsScheduler
from einsatzplaner.scheduler.llm import LLMScheduler
from einsatzplaner.scheduler.hybrid import LLMGuidedVRPScheduler
from einsatzplaner.simulator import run_woche
from einsatzplaner.disruptions import load_scenario
from einsatzplaner.evaluator import compute_metriken

montag = date(2026, 4, 20)
rp = HaversineRouteProvider()
ev = load_scenario("scenarios/baseline.yaml", montag)

for preset_name, profil in Szenarioprofil.presets().items():
    for name, sched in [
        ("heuristik", HeuristicScheduler()),
        ("ortools",   ORToolsScheduler(time_limit_sec=4)),
        ("llm",       LLMScheduler(model="claude-sonnet-4-6")),
        ("hybrid",    LLMGuidedVRPScheduler(model="claude-sonnet-4-6", time_limit_sec=4)),
    ]:
        rng = random.Random(42)
        techs = generate_techniker(10, rng)
        woche = generate_woche(montag, rng=rng, profil=profil)
        bekannte = {a.id: a for tag in woche.values() for a in tag}
        t0 = pt.time()
        we = run_woche(woche, techs, sched, rp, ev, preset_name)
        dur = pt.time() - t0
        m = compute_metriken(we, bekannte)
        print(f"{preset_name:<18} {name:<10} {m.erledigt:>3}/{m.generiert} "
              f"comp={m.completion_rate}% sla={m.sla_verletzungen} "
              f"fahrt={m.gesamtfahrzeit_min} t={dur:.1f}s")
PY
)"
```

### 10.3 Neue Störungsszenarien

YAML-Datei in `scenarios/` anlegen:

```yaml
name: Mein Szenario
beschreibung: Kurze Erklärung.
events:
  - typ: techniker_krank
    tag: 1                # 0 = Mo, 1 = Di, ...
    zeit: "10:00"
    techniker: T03
  - typ: kunde_absage
    tag: 2
    zeit: "08:15"
    auftrag: A0120
  - typ: stau
    tag: 3
    zeit: "08:30"
    stau_min: 30
    betroffene_techniker: [T01, T02]
  - typ: auftrag_verlaengert
    tag: 2
    zeit: "14:30"
    auftrag: A0150
    extra_min: 60
```

`list_scenarios("scenarios")` pickt die Datei automatisch in der UI auf.

### 10.4 Neuer Scheduler

1. Modul unter `einsatzplaner/scheduler/` anlegen mit Klasse `MyScheduler` die `Scheduler`-Protocol erfüllt (`name: str`, `plan(pin: PlanInput) -> Tourenplan`).
2. In `scheduler/__init__.py` re-exportieren.
3. In `app.py::_build_scheduler` neue Branch für `scheduler_name == "MyScheduler"` ergänzen und zu den Select-Optionen hinzufügen.
4. Optional in Multiselect der Arena aufnehmen.

### 10.5 Testen

Es gibt aktuell keine Unit-Tests. `pytest` ist in den Requirements, aber kein `tests/`-Verzeichnis angelegt. Empfohlene Priorisierung bei Testaufbau:

1. `test_generator.py` — Determinismus bei gleichem Seed, Grenzwerte des Profils
2. `test_heuristic_constraints.py` — Schichtende-Einhaltung, Pausen-Reihenfolge, Zeitfenster
3. `test_ortools_penalties.py` — Höhere Penalty ⇒ höhere Wahrscheinlichkeit der Zuweisung
4. `test_simulator_rollover.py` — Nicht erledigter Auftrag wandert mit `rollover_count += 1` weiter

## 11. Bekannte Issues / TODOs

- **Keine Tests** (siehe 10.5)
- **Prompt-Caching wirkt nicht** — System-Prompts unter 4 096 Tokens, Claude Opus/Sonnet 4.6 cached erst ab 4 096. Zu sehen an `cache_read_input_tokens: 0` in `last_usage`.
- **OR-Tools Breaks post-hoc** — Pausen sind nicht Teil des Optimierungsmodells, nur des Result-Parsings. Bei sehr dichten Zeitfenstern kann das zu unrealistischen Pause-Platzierungen führen.
- **Heuristik nicht balanciert** — Greedy-Insertion führt zu ungleicher Auslastung (beobachtet: T01 50 %, T09 85 %). Ein Load-Balancing-Term in der Cost-Funktion wäre simpel nachzurüsten, wurde aber bewusst nicht eingebaut, damit der Kontrast zu OR-Tools sichtbar bleibt.
- **Kein intraday Re-Planning** — Störungen führen zu Rollover, nicht zu neuem Scheduler-Aufruf am selben Tag.
- **Schema-Clamping im Hybrid ohne Sichtbarkeit** — wenn der LLM Werte außerhalb der Bereiche liefert, wird geclippt. Es gibt aktuell keinen Log-Output; `last_weights` enthält nur den geclippten Wert.

## 12. Operative Kennzahlen (Seed 42, Normal-Preset, baseline-Störungen)

| Kenngröße | Wert |
|---|---|
| Generierte Aufträge/Woche | 218 |
| Notfälle | 4–5 |
| SLA-kritisch (heute fällig) | 35 |
| Kapazität total | 21 000 min (10 × 5 × 420) |
| Gesamte Auftragsdauer | ~19 000 min (≈ 90 % Auslastungsdruck) |
| Heuristik Completion | 71.6 % |
| OR-Tools Completion | 82.6 % |
| Hybrid Completion | 82.1 % |
| Heuristik Laufzeit | < 0.1 s |
| OR-Tools Laufzeit (4 s × 5) | ~ 20 s |
| LLM/Hybrid Laufzeit | 55–70 s |
| API-Kosten Hybrid (Sonnet 4.6) | ~ $0.05 / Wochenlauf |
| API-Kosten LLM-Direct (Sonnet 4.6) | ~ $0.15 / Wochenlauf |

### 12.1 Extrem-Chaos (Single-Seed, anekdotisch)

| Scheduler | Erledigt | Completion | SLA-Vlz | Fahrzeit | Laufzeit |
|---|---|---|---|---|---|
| Heuristik | 144 / 412 | 35.0 % | 71 | 5632 | <0.1 s |
| OR-Tools (fix) | 214 / 412 | 51.9 % | 50 | 5112 | 20 s |
| LLM-Direct | 136 / 412 | 33.0 % | 101 | 4194 | 96 s |
| Hybrid | 215 / 412 | 52.2 % | 46 | 4878 | 63 s |

⚠️ Diese Zahlen stammen aus einem einzigen Seed und sind unter Multi-Seed-Bedingungen nicht reproduzierbar. Sie werden als historische Beobachtung geführt, nicht als belastbare Messung.

### 12.2 Multi-Profil-Woche, 20 Seeds (belastbarer Hauptbenchmark)

Mo = Normal, Di = Hochlast, Mi = Notfallwoche, Do = SLA-Katastrophe, Fr = Chaos. Stochastische Intraday-Events aus den Profil-Raten. Werte sind Mittelwert ± Standardabweichung.

| Scheduler | Completion | SLA-Vlz | Fahrzeit |
|---|---|---|---|
| Heuristik | 55.05 % ± 2.64 | 32.60 ± 5.25 | 5648 ± 208 |
| OR-Tools naiv | 67.64 % ± 1.68 | **17.25 ± 3.58** | 4690 ± 154 |
| OR-Tools normal | 68.75 % ± 1.63 | 19.05 ± 3.52 | 4678 ± 185 |
| OR-Tools chaos-safe | 68.11 % ± 1.90 | 18.55 ± 3.33 | 4678 ± 179 |
| OR-Tools sla-boost | 68.36 % ± 1.55 | 17.95 ± 3.10 | **4663 ± 156** |
| Hybrid | 67.86 % ± 1.88 | 18.10 ± 2.94 | 4667 ± 161 |

**Kern-Befund:** Alle vier OR-Tools-Varianten und der Hybrid liegen in allen Metriken im Rauschen (Differenzen < 1 Standardabweichung). Auch die bewusst schwach gewählte `naiv`-Kalibrierung performt vergleichbar — OR-Tools ist im getesteten Bereich robust gegen Penalty-Variation. Der Hybrid liefert keinen messbaren Performance-Vorteil; sein Wert ist die **Tatsache, dass der Anwender keine Kalibrierungs-Entscheidung treffen muss** (Details im Hauptreport §6 und §5.5).

Heuristik ist 13.3 Prozentpunkte unter allen Solver-Varianten — der einzige statistisch signifikante Unterschied im Set.

### 12.3 Replan-Test, 10 Seeds (intraday-Störungen triggern Replan)

Dieselbe Multi-Profil-Woche wie oben, aber mit aktiviertem Replan-Trigger. Krankmeldung, Notfall, Stau, Auftragsverlängerung lösen einen Scheduler-Aufruf aus. Absage bleibt lokal.

| Scheduler | Variante | Completion | SLA-Vlz | Fahrzeit |
|---|---|---|---|---|
| Heuristik | Replan | 57.92 % ± 2.03 | 28.90 ± 6.28 | 5713 ± 182 |
| OR-Tools naiv | Replan | 66.02 % ± 2.20 | 19.20 ± 5.98 | 4417 ± 254 |
| OR-Tools normal | Replan | 67.33 % ± 2.28 | 20.10 ± 4.48 | 4400 ± 236 |
| Hybrid | Replan (dumb) | 66.58 % ± 2.43 | 19.70 ± 3.62 | 4411 ± 214 |
| Hybrid | Replan + Context | 66.78 % ± **1.73** | 19.30 ± 3.68 | 4396 ± 179 |

**Kern-Befund:** Replan hilft nur der Heuristik signifikant (+2.87 pp Completion gegen §12.2). OR-Tools und Hybrid verlieren beide leicht durch Replan (−1.3 bis −1.6 pp). Der Tagesverlauf-Kontext für den Hybrid bringt keinen Mittelwert-Gewinn; einziger Effekt: Streuung sinkt von ±2.43 auf ±1.73. Die Hybrid-Hypothese „bei intraday Dynamik zeigt sich ein Vorteil" ist mit diesem Test widerlegt.

Rohdaten: `bench/results_replan_preview.csv` (dumb), `bench/results_replan_context.csv` (Context).
