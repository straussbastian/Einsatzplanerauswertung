# Wissenschaftliche Auswertung: Vergleich heuristischer, solver-basierter und LLM-gesteuerter Einsatzplanung

**Projekt:** Einsatzplaner — Experiment zur automatisierten Tourenplanung für Servicetechniker eines Heizungsbaubetriebs
**Datum:** 2026-04-15
**Status:** Prototyp / Proof of Concept

---

## Zusammenfassung

Wir vergleichen vier Verfahren zur täglichen Zuweisung von Wartungsaufträgen auf zehn Servicetechniker eines fiktiven Heizungsbaubetriebs in Oldenburg: eine **Insertion-Heuristik**, einen **OR-Tools VRPTW-Solver** mit statischen Priorisierungs-Gewichten, einen **LLM-Direct-Scheduler** (Claude Sonnet 4.6 entscheidet Zuordnung und Reihenfolge) und einen **Hybrid-Ansatz** (LLM setzt tagesaktuelle Priorisierungs-Gewichte, OR-Tools optimiert darauf).

**Kernbotschaft in einem Satz:** _Ein gut konstruierter klassischer Algorithmus ist in dieser Domäne nicht nur ebenbürtig, sondern in allen drei Testsetups marginal überlegen — LLM-basierte Zusatzkomponenten liefern keinen messbaren Performance-Vorteil, selbst wenn sie genau die Information bekommen, die ihnen theoretisch helfen müsste._

Unter kontrollierten Einzelprofil-Bedingungen dominiert der **OR-Tools-Solver** mit statischen Gewichten — +11 Prozentpunkte Completion gegenüber der Heuristik bei gleichzeitig 15 % geringerer Gesamtfahrzeit. Der **LLM-Direct-Ansatz** schneidet in allen Messgrößen schlechter ab und zeigt die bekannte Schwäche von LLMs bei großen kombinatorischen Optimierungsproblemen.

Der methodisch belastbare Haupttest ist in drei Stufen aufgebaut, von denen jede eine spezifische Hybrid-Hypothese prüft — alle drei widerlegen sie:

1. **Multi-Profil-Woche mit 20 Seeds (§5.5).** Getestet gegen vier statische Kalibrierungen (`naiv`, `normal`, `chaos-safe`, `sla-boost`). Der Hybrid liegt in Completion, SLA-Verletzungen und Fahrzeit **innerhalb einer Standardabweichung** gegenüber allen vier — auch gegenüber der bewusst schwachen `naiv`-Variante. Die Hypothese „Hybrid schützt vor schlechter Kalibrierung" ist widerlegt, weil OR-Tools im getesteten Bereich **robust gegen Penalty-Variation** ist.

2. **Replan-Test (§5.6, 10 Seeds).** Intraday-Störungen (Krankmeldung, Notfall, Stau, Auftragsverlängerung) triggern einen Replan. Alle Scheduler können neu planen. Auch hier **kein signifikanter Hybrid-Vorteil** — im Gegenteil, OR-Tools und Hybrid verlieren beide leicht durch den Replan (−1.3 pp Completion), während die Heuristik als einzige gewinnt (+2.9 pp), weil sie vorher überhaupt keine Umverteilung nach Ereignissen gemacht hat.

3. **Replan mit Tagesverlauf-Kontext (§5.6).** Der Hybrid bekommt zusätzlich strukturierte Tagesverlauf-Daten: wievielter Replan heute, welches Ereignis, bisheriger Tagesfortschritt, Rest-Schicht pro Techniker. **Kein Mittelwert-Gewinn** gegenüber „Replan ohne Kontext". Einziger messbarer Effekt: die Run-zu-Run-Streuung des Hybrid sinkt (Completion-Stdev von ±2.43 auf ±1.73). Das LLM verarbeitet den Kontext nachweislich (im Reasoning artikuliert), ohne dass sich daraus ein Performance-Hebel ergibt.

**Konsequenz:** In allen drei theoretisch günstigen Szenarien für den Hybrid — schlechte Kalibrierung, Störungs-Dynamik, tagesverlauf-sensitive Adaptivität — liefert er **keinen Performance-Vorteil** gegenüber einem statisch kalibrierten OR-Tools-Solver. Der Wertschlüssel reduziert sich auf ein strukturelles Argument: der Hybrid ersetzt eine Kalibrierungs-Entscheidung, die der Anwender im Mittelstand ohnehin nicht explizit trifft (§6) — nicht auf bessere Ergebnisse pro Lauf. Ob dieser strukturelle Vorteil LLM-API-Kosten, Laufzeit-Variabilität und Betriebskomplexität rechtfertigt, ist eine Geschäfts- und keine Performance-Frage.

Der frühere Einzel-Seed-Befund (+1 Auftrag, −4 SLA im Chaos) hat sich unter statistischer Absicherung **nicht bestätigt** und ist in §5.3 als anekdotisch markiert.

---

## 1. Ausgangslage

### 1.1 Domäne

Ein Heizungsbaubetrieb unterhält eine Flotte von zehn Servicetechnikern, die täglich Wartungs- und Reparaturaufträge bei Privat- und Gewerbekunden in einem Umkreis von 30 km um Oldenburg (Oldb.) erledigen. Jeder Techniker verfügt über eine 8-Stunden-Schicht (8:00–16:00), unterbrochen durch 15 Minuten Frühstücks- und 45 Minuten Mittagspause, was eine effektive Nettoarbeitszeit von 420 min/Tag ergibt. Die gesetzlich vorgeschriebene Mittagspause muss spätestens nach 6 Stunden Arbeit genommen werden.

### 1.2 Problem

Die tägliche Disposition — welcher Auftrag wird welchem Techniker in welcher Reihenfolge zugewiesen — ist ein klassisches **Vehicle Routing Problem with Time Windows (VRPTW)** mit mehreren zusätzlichen Komplikationen:

- **Überkapazität** im Backlog: Im Normalbetrieb liegt der Auftragsvolumen bei ~90 % der rechnerischen Nettokapazität; zusammen mit Fahrtzeiten, Rollover und Zeitfenster-Zwängen übersteigt die tatsächliche Nachfrage regelmäßig das Mögliche. Im konstruierten Stress-Preset `Chaos` wird die Auftragsmenge bis auf ~180 % gehoben, um Priorisierungs-Verhalten gezielt sichtbar zu machen. Priorisierung ist damit in beiden Fällen unumgänglich.
- **Heterogene Dringlichkeit**: Notfälle (Kunde ohne Heizung im Winter), SLA-Fristen mit harten Deadlines, flexible Wartungstermine.
- **Zeitfenster** bei fixen Terminen und gewerblichen Öffnungszeiten.
- **Dynamische Störungen**: Kundenabsagen, krankheitsbedingter Technikerausfall, Stau, spontane Notfälle.
- **Rollover-Effekt**: Nicht erledigte Aufträge wandern in den nächsten Tag und erhöhen dort ihre Priorität. Ohne Gegensteuern akkumulieren sie und werden nach einer Woche zum Bottleneck.

### 1.3 Forschungsfragen

1. **Wie groß ist der Abstand zwischen einer simplen greedy-Heuristik und einem modernen VRPTW-Solver** in diesem konkreten Setting?
2. **Kann ein LLM (Claude Sonnet 4.6) die Disposition direkt lösen**, also ohne Solver?
3. **Liefert ein Hybrid-Ansatz — LLM wählt Priorisierungs-Gewichte, Solver optimiert — einen Mehrwert gegenüber statisch kalibrierten Gewichten**, und wenn ja: unter welchen Bedingungen?

---

## 2. Methodik

### 2.1 Simulationsumgebung

Das Experiment ist als **deterministische, seed-gesteuerte Simulation** implementiert. Identische Eingaben (Seed, Szenarioprofil, Planungswoche) erzeugen identische Auftragsmengen und erlauben den paarweisen Vergleich der vier Scheduler auf exakt denselben Daten.

**Technischer Stack:**

- **Python 3.14**, Streamlit als UI
- **Google OR-Tools 9.15** (`pywrapcp.RoutingModel`) für den VRPTW-Solver
- **Anthropic Claude API** (`claude-opus-4-6`, `claude-sonnet-4-6`, `claude-haiku-4-5`) über das offizielle Python-SDK
- Haversine-basierte Distanzberechnung mit Umwegfaktor 1.3 und Durchschnittsgeschwindigkeit 50 km/h — austauschbares `RouteProvider`-Interface erlaubt späteren Upgrade auf OSRM.

### 2.2 Datengenerator

Aufträge werden synthetisch erzeugt mit den Attributen `id`, `kunde`, `typ ∈ {privat, gewerblich}`, `adresse`, `lat/lon` (gleichverteilt im 30-km-Radius), `dauer_min ∈ {30, 45, 60, 90, 120, 180}`, `dringlichkeit ∈ {1, 2, 3}`, `terminart ∈ {flexibel, fix}`, `fenster_von/bis`, `sla_frist`, `notfall`, `rollover_count`.

Der Default-Generator erzeugt 40–50 Aufträge pro Werktag (218 pro Woche) mit Dringlichkeitsverteilung `(0.5, 0.35, 0.15)` für Stufen 1/2/3, ca. 2 % Notfälle und SLA-Fristen in Abhängigkeit der Dringlichkeit (hoch: 0–1 Tag, mittel: 2–5 Tage, niedrig: 5–14 Tage).

### 2.3 Szenarien und Intensitäts-Presets

Wir trennen zwei orthogonale Achsen:

**Störungs-Szenarien** (steuern das Event-Profil während des Wochenablaufs):
- `baseline`: keine Störungen
- `sick_leave`: Techniker T03 meldet sich Di 10:00 krank
- `cancellations`: drei Kundenabsagen verteilt über die Woche
- `mixed`: Kundenabsage, Krankmeldung, Stau und Kundenabsage kombiniert
- `chaos`: Krankmeldung + Absage + Stau + **drei ungeplante Auftragsverlängerungen** (z.B. weil der Techniker vor Ort ein zusätzliches Problem findet, das die Bearbeitung um 60–90 min verlängert). Auslegung für den Einsatz zusammen mit dem Chaos-Intensitätspreset.

Unterstützte Event-Typen:

| EventTyp | Wirkung |
|---|---|
| `techniker_krank` | Alle nach Event-Zeitpunkt geplanten Stops des betroffenen Technikers werden `nicht_ausgefuehrt`; deren Aufträge gehen in Rollover. |
| `kunde_absage` | Ein konkreter Auftrag wird aus der Tour entfernt und als `storniert` markiert. |
| `stau` | Für die `betroffene_techniker` werden alle Stops nach Event-Zeit um `stau_min` verschoben; Stops, die dadurch über Schichtende rutschen, rollen. |
| `auftrag_verlaengert` | Die Dauer eines konkreten Auftrags wird um `extra_min` erhöht; nachfolgende Stops derselben Tour verschieben sich mit; Überläufe gehen in Rollover. |
| `notfall` | Ein neuer Auftrag mit Notfall-Flag kommt ins Tagesset (in der aktuellen Implementierung als Rollover für den Folgetag). |

**Intensitäts-Presets** (steuern die Auftragszusammensetzung):

| Preset | Notfall-Rate | SLA-Druck | Überlast | Rollover-Altlast |
|---|---|---|---|---|
| Normal | 2 % | 10 % | 100 % | 0 |
| Hochlast | 2 % | 15 % | 150 % | 0 |
| Notfallwoche | 20 % | 10 % | 100 % | 0 |
| SLA-Katastrophe | 2 % | 40 % | 100 % | 15 |
| Chaos | 12 % | 25 % | 180 % | 20 |

Die resultierende Woche wird mit Seed 42 erzeugt. Das ergibt für `Normal` 218 Aufträge, 4 Notfälle und 35 SLA-kritische Aufträge; für `Chaos` entsprechend 412 Aufträge, 41 Notfälle, 148 SLA-kritische und 20 vorbelastete Rollover-Aufträge am Montag.

### 2.4 Ablauf einer Simulationswoche

Für jeden Werktag Mo–Fr:

1. Der Scheduler erhält die Auftragsmenge aus Neueingängen plus Rollover-Backlog aus dem Vortag.
2. Er liefert einen Tourenplan (Stops pro Techniker, Start-/Endezeiten, Pausen).
3. Störungsereignisse werden auf den Plan angewandt: Stops eines krankgemeldeten Technikers werden als „nicht ausgeführt" markiert; Kundenabsagen entfernen Stops; Stau verschiebt nachfolgende Stops; **Auftragsverlängerungen** verlängern den betroffenen Auftrag und schieben alle nachfolgenden Stops derselben Tour nach hinten.
4. Stops, die bis Schichtende nicht vor Störungen „erledigt" wurden, sind Rollover für den nächsten Tag mit `rollover_count += 1`.

### 2.5 Metriken

- **Completion Rate**: Anteil erledigter Aufträge an der Gesamtzahl der generierten Aufträge der Woche
- **Prio-gewichtete Completion**: Summe(`dringlichkeit`) der erledigten Aufträge / Summe(`dringlichkeit`) aller Aufträge
- **SLA-Verletzungen**: am Ende der Woche offene Aufträge, deren SLA-Frist bereits verstrichen ist
- **Gesamtfahrzeit**: Summe der Fahrzeiten über alle Techniker und Tage (Minuten)
- **Auslastung**: erledigte Arbeitszeit / verfügbare Nettokapazität (10 × 5 × 420 = 21 000 min)
- **Notfall-Abdeckung**: erledigte Notfälle / Gesamtnotfälle
- **Laufzeit**: Wall-Clock-Zeit des Wochenlaufs

---

## 3. Verfahren

### 3.1 Heuristik-Scheduler (`einsatzplaner/scheduler/heuristic.py`)

Deterministische Greedy-Insertion mit Priorisierung:

1. **Prio-Score** pro Auftrag: `10 × dringlichkeit + 50 × notfall + 5 × rollover_count + sla_urgency`
2. Sortiere Aufträge absteigend nach Score.
3. Für jeden Auftrag: wähle den Techniker mit den **geringsten zusätzlichen Kosten** (Fahrzeit + ggf. Wartezeit auf Zeitfenster-Öffnung), der die Arbeitszeit-, Pausen- und Fenster-Constraints nicht verletzt.
4. Pausen werden automatisch eingefügt: Frühstück ab 10:00, Mittag ab 12:00 (im Fenster 11:30–13:30).
5. Das effektive Schichtende wird um die ausstehenden Pausen-Minuten und die Rückfahrt zum Depot reduziert, um Überstunden zu vermeiden.

Die Heuristik ist kalibriert darauf, **keine Schichtende-Überschreitungen** zu produzieren (verifiziert auf Seed 42, Single-Day: 0 Minuten Überschreitung über alle 10 Techniker).

### 3.2 OR-Tools VRPTW (`einsatzplaner/scheduler/ortools_vrp.py`)

Formulierung als **VRPTW mit Disjunction-Penalties**:

- **Knoten**: Depot (Index 0) plus ein Knoten pro Auftrag
- **Fahrzeuge**: 10 Techniker, alle mit identischem Start- und Endknoten (Depot)
- **Distanz-Matrix**: Haversine × 1.3 / 50 km/h, in Minuten gerundet
- **Zwei Transit-Callbacks**:
  - `travel_cb` für die **Arc-Cost** (nur Fahrzeit, skaliert mit `travel_weight_pct`)
  - `time_cb` für die **Time-Dimension** (Fahrzeit + Service-Zeit am Origin-Knoten)
- **Time-Dimension** mit Horizon 600 min, CumulVar-Range pro Knoten aus den Zeitfenstern
- **Disjunction-Penalty** pro Auftrag, Höhe berechnet aus Dringlichkeit, Notfall-Flag, Rollover und SLA-Abstand
- **Schichtlänge**: End-CumulVar pro Fahrzeug auf 420 min (Nettoarbeitszeit, Pausen werden post-hoc eingefügt)
- **Solver**: `PATH_CHEAPEST_ARC` Erstlösung + `GUIDED_LOCAL_SEARCH` Metaheuristik, 4 s Zeitlimit

**Wichtige Designentscheidung — Pausen außerhalb des Modells:** Eine frühe Implementierung nutzte `FixedDurationIntervalVar` mit `SetBreakIntervalsOfVehicle`. Das blockierte den Solver massiv (32/45 Aufträge zugewiesen bei einem Tag). Die Umstellung auf implizite Kapazitätsreduktion (Schicht = 420 min statt 480 min) plus deterministische Pausen-Einfügung beim Parsen der Lösung erhöhte die Qualität auf 37/45 Aufträge und entfernte einen klaren Performance-Engpass im Solver.

**Standard-Penalty-Gewichte:**
```
base_penalty:             50 000
dringlichkeit_multiplier: 10 000
notfall_bonus:           100 000
rollover_multiplier:       5 000
sla_today_bonus:          50 000
sla_soon_bonus:           15 000
travel_weight_pct:           100
```

### 3.3 LLM-Direct (`einsatzplaner/scheduler/llm.py`)

Claude Sonnet 4.6 (konfigurierbar auf Opus 4.6 oder Haiku 4.5) erhält die Tagesaufträge und Technikerliste als JSON und liefert über Tool-Use eine Zuordnung `{tech_id → [auftrag_id, …]}` in Ausführungsreihenfolge. Die deterministische Tour-Konstruktion aus der Heuristik übernimmt danach die Zeitberechnung und Pausen-Einfügung — der LLM entscheidet also **nicht** über Uhrzeiten, sondern nur über **Zuweisung und Reihenfolge**.

Bei API-Fehlern oder ausbleibendem `submit_plan`-Aufruf fällt der Scheduler auf die Heuristik zurück.

Siehe Abschnitt 4 für den vollständigen Prompt.

### 3.4 Hybrid (`einsatzplaner/scheduler/hybrid.py`)

**Kernidee:** Das LLM löst nicht das Routing-Problem selbst (das ist ein kombinatorisches Optimierungsproblem, bei dem LLMs schwächer sind als dedizierte Solver), sondern trifft die **strategische Meta-Entscheidung**: welche Priorisierungs-Dimension ist heute wichtiger? Die eigentliche Route berechnet OR-Tools.

Ablauf pro Tag:

1. Der Scheduler berechnet **Tagesstatistiken**: Anzahl Aufträge, Verteilung der Dringlichkeit, Anzahl Notfälle, SLA-kritische Aufträge, Rollover-Verteilung, Auslastungsdruck.
2. Das LLM erhält diese Statistiken und liefert über Tool-Use sieben numerische Gewichte plus eine Begründung.
3. Die Gewichte werden auf definierte Wertebereiche geclippt und in eine `PenaltyWeights`-Datenklasse übertragen.
4. OR-Tools wird mit diesen Gewichten instanziiert und löst den VRPTW mit unverändertem Modell.

Das Verfahren tauscht also nur die **Penalty-Kalibrierung** aus, nicht das Modell oder die Solver-Strategie.

Siehe Abschnitt 4 für den vollständigen Prompt.

---

## 4. LLM-Einbindung

### 4.1 Modellwahl und Parameter

Alle LLM-Aufrufe verwenden default `claude-sonnet-4-6` als Kompromiss aus Qualität, Latenz und Kosten. Die App erlaubt Umschalten auf `claude-opus-4-6` (beste Qualität, teuer) oder `claude-haiku-4-5` (schnell, günstig).

Konfiguration:
- `max_tokens`: 16 000 (LLM-Direct) bzw. 4 000 (Hybrid — Ausgabe ist nur ein kleiner Parameter-Satz)
- **Prompt-Caching** aktiviert für den System-Prompt (`cache_control: {"type": "ephemeral"}`). In der aktuellen Implementierung ist der System-Prompt mit ca. 900–1100 Tokens unter der Cache-Mindestlänge von Claude Opus/Sonnet 4.6 (4 096 Tokens) — Caching bleibt daher zunächst wirkungslos, was in der App am `cache_read_input_tokens: 0` sichtbar ist. Für Produktionseinsatz wäre der Prompt gezielt auf >4 096 Tokens zu erweitern (Beispiele, Regelwerk, Policy-Referenzen).
- **Tool-Choice**: `{"type": "tool", "name": "submit_plan"|"submit_weights"}` erzwingt den strukturierten Aufruf.
- **Fallback**: Bei `AnthropicError` oder fehlendem Tool-Aufruf übernimmt die Heuristik (LLM-Direct) bzw. werden Default-Gewichte verwendet (Hybrid).

### 4.2 LLM-Direct — System-Prompt (wörtlich)

```text
Du bist ein erfahrener Einsatzdisponent für einen Heizungsbaubetrieb in Oldenburg (Oldb).

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
- Nur gegebene Techniker-IDs verwenden
```

### 4.3 LLM-Direct — Tool-Schema (wörtlich)

```json
{
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
          "items": {"type": "string"}
        }
      },
      "reasoning": {
        "type": "string",
        "description": "Kurze Begründung der Strategie (max. 200 Wörter)."
      }
    },
    "required": ["zuordnungen", "reasoning"]
  }
}
```

### 4.4 LLM-Direct — User-Prompt-Schema

```
Datum: <isoformat>

TECHNIKER (10): T01, T02, ..., T10

AUFTRÄGE (<n>):
[
  {"id": "A0001", "lat": 53.12, "lon": 8.15, "dauer_min": 60,
   "prio": 2, "notfall": false, "terminart": "flexibel",
   "fenster": "09:00-17:00", "sla_in_tagen": 3, "rollover_count": 0},
  ...
]

Erstelle den optimalen Einsatzplan mit dem Tool submit_plan.
```

### 4.5 Hybrid — System-Prompt (wörtlich)

```text
Du bist ein erfahrener Einsatzdisponent für einen Heizungsbaubetrieb.

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

Rufe das Tool `submit_weights` GENAU EINMAL auf mit deinen Gewichten und einer Begründung.
```

### 4.6 Hybrid — Tool-Schema (wörtlich)

```json
{
  "name": "submit_weights",
  "description": "Reicht die Priorisierungs-Gewichte für den OR-Tools-Solver ein.",
  "input_schema": {
    "type": "object",
    "properties": {
      "base_penalty":             {"type": "integer", "description": "Basis-Kosten pro gedropptem Auftrag (30000-100000)."},
      "dringlichkeit_multiplier": {"type": "integer", "description": "Multiplikator × Dringlichkeit (5000-50000)."},
      "notfall_bonus":            {"type": "integer", "description": "Extra-Penalty für Notfälle (50000-500000)."},
      "rollover_multiplier":      {"type": "integer", "description": "Pro Tag Rollover (2000-30000)."},
      "sla_today_bonus":          {"type": "integer", "description": "Extra-Penalty wenn SLA ≤ heute (20000-200000)."},
      "sla_soon_bonus":           {"type": "integer", "description": "Extra-Penalty wenn SLA in 1-2 Tagen (5000-50000)."},
      "travel_weight_pct":        {"type": "integer", "description": "Fahrtzeit-Gewicht in % (50=halbiert, 100=normal, 300=verdreifacht)."},
      "reasoning":                {"type": "string",  "description": "Kurze Begründung der gewählten Strategie (max. 150 Wörter)."}
    },
    "required": ["base_penalty", "dringlichkeit_multiplier", "notfall_bonus",
                 "rollover_multiplier", "sla_today_bonus", "sla_soon_bonus",
                 "travel_weight_pct", "reasoning"]
  }
}
```

### 4.7 Hybrid — User-Prompt-Schema

```
TAGESLAGE:
{
  "datum": "2026-04-20",
  "wochentag": "Monday",
  "auftraege_gesamt": 45,
  "techniker": 10,
  "kapazitaet_min": 4200,
  "gesamt_auftragsdauer_min": 3692,
  "auslastungs_druck_pct": 87.9,
  "dringlichkeit_1": 22,
  "dringlichkeit_2": 14,
  "dringlichkeit_3": 9,
  "notfaelle": 2,
  "sla_heute_oder_ueber": 5,
  "sla_in_1_2_tagen": 6,
  "rollover_max_tage": 0,
  "rollover_verteilung": {"0": 45},
  "fixe_termine": 14,
  "gewerbliche_auftraege": 18
}

Setze die Gewichte so, dass der Solver die heute wichtigsten Aufträge bevorzugt.
```

### 4.8 Beispiel-Reasoning des Hybrid-LLM

Tatsächliche Ausgabe bei Seed 42, Montag des Normal-Presets:

> **Gewichte:** `base_penalty=50000, dringlichkeit_multiplier=10000, notfall_bonus=250000, rollover_multiplier=2000, sla_today_bonus=150000, sla_soon_bonus=30000, travel_weight_pct=80`
>
> **Reasoning:** „Heute Montag, 20.04.: Kapazität ist ausreichend (Auslastungsdruck 87,9 % – alle 45 Aufträge passen rechnerisch in die 4200 min). Keine Rollover vorhanden (rollover_max=0), daher rollover_multiplier minimal. Zwei Notfälle bekommen massiven Bonus (250k), damit der Solver sie immer einplant. Fünf SLA-überfällige Aufträge sind kritisch → sla_today_bonus sehr hoch (150k). Sechs weitere mit SLA in 1-2 Tagen erhalten deutlich erhöhten sla_soon_bonus (30k). travel_weight_pct leicht reduziert (80), da Auftragsvolumen wichtiger ist als Fahrteffizienz – der Solver soll lieber einen kleinen Umweg nehmen als einen kritischen Auftrag droppen. Dringlichkeitsmultiplikator auf Standard, um die 9 hoch-dringlichen Aufträge (Stufe 3) sauber zu priorisieren."

Dieses Verhalten passt qualitativ zur Richtwerte-Tabelle im Prompt und zeigt, dass das LLM die Statistiken tatsächlich interpretiert, statt pauschal Defaults zurückzugeben.

---

## 5. Ergebnisse

Alle Ergebnisse basieren auf Seed 42, 10 Techniker, Montag 2026-04-20 als Wochenstart. Solver-Zeitlimit 4 s/Tag. LLM-Modell: `claude-sonnet-4-6`. Alle Werte beziehen sich auf eine vollständige Arbeitswoche (Mo–Fr).

### 5.1 Normal-Preset mit Störungsszenarien

218 generierte Aufträge, 4 Notfälle, 35 SLA-kritisch, 0 Rollover-Vorbelastung.

| Szenario | Scheduler | Erledigt | Completion | Prio-gew. | Fahrzeit | SLA-Vlz | Laufzeit |
|---|---|---|---|---|---|---|---|
| baseline    | Heuristik      | 156 | 71.6 % | 77.7 % | 5650 | 7 | 0.0 s |
| baseline    | OR-Tools (fix) | **180** | **82.6 %** | **83.5 %** | 4806 | 7 | 20 s |
| baseline    | LLM            | 135 | 61.9 % | 63.4 % | **4253** | 11 | 58 s |
| baseline    | Hybrid         | 179 | 82.1 % | 83.0 % | 4860 | 7 | 62 s |
| sick_leave  | Heuristik      | 150 | 68.8 % | 76.0 % | 5683 | 7 | 0.0 s |
| sick_leave  | OR-Tools (fix) | **179** | **82.1 %** | **83.0 %** | 4750 | 7 | 20 s |
| sick_leave  | LLM            | 138 | 63.3 % | 62.8 % | **4187** | 13 | 66 s |
| sick_leave  | Hybrid         | 178 | 81.7 % | 82.7 % | 4756 | 7 | 60 s |
| mixed       | Heuristik      | 148 | 67.9 % | 75.1 % | 5649 | 7 | 0.0 s |
| mixed       | OR-Tools (fix) | **175** | **80.3 %** | **82.1 %** | 4762 | 7 | 20 s |
| mixed       | LLM            | 136 | 62.4 % | 64.5 % | 4513 | 9 | 69 s |
| mixed       | Hybrid         | 175 | 80.3 % | 81.6 % | **4682** | 7 | 60 s |

**Beobachtungen:**

- OR-Tools schlägt die Heuristik konsistent um 10–12 Prozentpunkte Completion bei gleichzeitig 15–17 % weniger Fahrtzeit. Der Solver-Vorteil ist robust gegen Störungen.
- Der **LLM-Direct-Scheduler** liegt 9–12 Prozentpunkte unter der Heuristik und 20 Prozentpunkte unter OR-Tools. Er fährt allerdings die kürzesten Touren pro *zugewiesenem* Auftrag — der LLM neigt zur Unterbesetzung der Tour (siehe Abschnitt 6).
- Der **Hybrid** liegt ± 1 Auftrag neben OR-Tools mit statischen Gewichten. Bei `mixed` fährt er minimal weniger Fahrzeit (4682 vs. 4762).

### 5.2 Extrem-Szenarien (nur OR-Tools-fix vs. Hybrid)

Um den Mehrwert des LLM-gesteuerten Gewichtswahl zu isolieren, vergleichen wir den Hybrid ausschließlich mit dem statisch kalibrierten OR-Tools. Störungs-Szenario immer `baseline`; Variation nur über die Intensitäts-Presets.

| Preset | Scheduler | Erledigt | Completion | Prio-gew. | Notfall | Fahrzeit | SLA-Vlz |
|---|---|---|---|---|---|---|---|
| Hochlast           | ortools-fix | **204** | **62.4 %** | **67.2 %** | 5/5 | **4779** | 17 |
| Hochlast           | hybrid      | 203 | 62.1 % | 65.9 % | 5/5 | 4792 | 17 |
| Notfallwoche       | ortools-fix | **180** | **82.6 %** | **84.5 %** | 35/41 | 4878 | 18 |
| Notfallwoche       | hybrid      | 178 | 81.7 % | 83.3 % | 35/41 | **4730** | 18 |
| SLA-Katastrophe    | ortools-fix | **184** | **79.0 %** | **81.7 %** | 5/5 | **4642** | 17 |
| SLA-Katastrophe    | hybrid      | 183 | 78.5 % | 80.2 % | 5/5 | 4732 | 17 |
| **Chaos**          | ortools-fix | 214 | 51.9 % | 59.4 % | 36/42 | 4998 | 45 |
| **Chaos**          | **hybrid**  | **218** | **52.9 %** | 59.4 % | 36/42 | **4829** | **44** |

**Zentrale Beobachtung:**

- Bei **single-factor Stress** (nur Überlast / nur Notfälle / nur SLA) schneidet der statische Solver marginal besser ab. Das ist erklärbar: unsere Default-Gewichte wurden für das Normal-Preset kalibriert; solange der Stress auf einer Dimension liegt, hebt sich der dafür zuständige Penalty-Term sichtbar gegen die Fahrtkosten ab.
- Im **Chaos-Preset** (hoher Notfall- *und* SLA- *und* Überlast-Druck gleichzeitig, plus Rollover-Altlast) **schlägt der Hybrid den statischen Solver** um 4 zusätzliche Aufträge (+1.0 Prozentpunkt Completion), eine SLA-Verletzung weniger (−2.2 %), und 169 Minuten weniger Fahrzeit (−3.4 %).

### 5.3 Extrem-Chaos mit ungeplanten Auftragsverlängerungen

> ⚠️ **Anekdotisch, nicht statistisch belegt.** Dieser Abschnitt basiert auf einem einzigen Seed (42). Die unten stehenden Differenzen (+1 Auftrag, −4 SLA, −234 min Fahrzeit) sind unter Multi-Seed-Bedingungen **nicht reproduzierbar** (siehe §5.5). Sie werden hier als **qualitative Illustration** beibehalten, weil sie das Ausgangs-Entdeckungs-Narrativ des Projekts dokumentieren — als Performance-Beweis sind sie **nicht belastbar**.

Der härteste bisher gemessene Test kombiniert das **Chaos-Intensitätspreset** (412 Aufträge, 41 Notfälle, 148 SLA-kritisch, 20 vorbelastete Rollover) mit dem **`chaos`-Störungsszenario** (Krankmeldung T04, Kundenabsage, Stau, drei ungeplante Auftragsverlängerungen mit +60 bis +90 min während der Ausführung). Letzteres entspricht dem realistischen Fall, dass ein Techniker vor Ort ein zusätzliches Problem entdeckt, das die Bearbeitungszeit deutlich über den Plan hinaus verlängert und alle nachfolgenden Stops der Tour nach hinten schiebt.

| Scheduler | Erledigt | Completion | Prio-gew. | Fahrzeit | SLA-Vlz | Laufzeit |
|---|---|---|---|---|---|---|
| Heuristik       | 144 | 35.0 % | 48.3 % | 5632 | 71 | 0.0 s |
| OR-Tools (fix)  | 214 | 51.9 % | **59.2 %** | 5112 | 50 | 20 s |
| LLM-Direct      | 136 | 33.0 % | 39.1 % | **4194** | 101 | 96 s |
| **Hybrid**      | **215** | **52.2 %** | 58.7 % | **4878** | **46** | 63 s |

**Beobachtungen:**

- Der Hybrid **schlägt den statischen OR-Tools-Solver auf allen drei operativen Hauptmetriken gleichzeitig**: er erledigt einen Auftrag mehr (215 vs. 214), verursacht **4 SLA-Verletzungen weniger** (46 vs. 50, −8 %) und spart **234 min Fahrzeit** (−4.6 %).
- Die Prio-gewichtete Completion ist marginal niedriger (58.7 % vs. 59.2 %) — das LLM opfert also einen hoch-dringlichen Stop zugunsten mehrerer SLA-kritischer mit etwas niedrigerer Dringlichkeit. In der Abwägung *verletzte SLA-Fristen vs. nicht erledigte Notfälle* ist das je nach Betriebspolitik sinnvoll.
- Der **LLM-Direct-Scheduler bricht unter dieser Last ein**: mit nur 136 erledigten Aufträgen und 101 SLA-Verletzungen liegt er deutlich unter der Heuristik. Der kombinatorische Druck der Chaos-Woche überfordert das direkte End-to-End-Routing per LLM.
- Die Heuristik verliert gegenüber OR-Tools/Hybrid **17 Prozentpunkte Completion** und **21 SLA-Verletzungen mehr**. Hier zahlt sich die globale Optimierung besonders aus.

### 5.4 Interpretation

Die Ergebnisse bestätigen eine klare Hypothese: **LLM-gesteuerte Parameterwahl wird wertvoll, wenn der Strategie-Raum mehrdimensional wird**. Bei einer einzigen dominanten Stressachse ist statische Kalibrierung effizient und robust. Bei gleichzeitigem Druck auf mehreren Achsen (Chaos) muss das Gewichtsprofil dynamisch abwägen, welche Dimension heute das engere Bottleneck ist — genau dort glänzt die kontextabhängige Entscheidung des LLM.

Der **LLM-Direct-Scheduler** überzeugt nicht: er unterschätzt systematisch die Kapazität pro Techniker (bei 20 Aufträgen nur 16/20 zugewiesen; bei 45 Aufträgen 132/218 über die Woche), obwohl die Touren pro eingeplantem Auftrag kürzer sind als bei den anderen Verfahren. Das entspricht der Literaturlage: LLMs sind **strategisch gut** aber **kombinatorisch schwach** — sie können Prioritäten erkennen, aber nicht viele Constraints gleichzeitig global optimieren.

---

### 5.5 Multi-Profil-Woche mit stochastischen Intraday-Störungen

Der bisher dargestellte Vergleich hat eine methodische Schwachstelle: jedes Preset wurde isoliert gegen die beiden Solver getestet, und die YAML-Szenarien sind zum Planungszeitpunkt bereits vollständig bekannt. Das misst zwei Eigenschaften, die so in der Realität nicht auftreten:

1. **Konstantes Lastprofil über eine Woche.** Ein Heizungsbetrieb hat nie fünf Tage Chaos oder fünf Tage SLA-Katastrophe am Stück; Lastprofile wechseln saisonal und tagesaktuell.
2. **Deterministische Störungen.** Echte Krankmeldungen, Staus, Auftragsverlängerungen und Absagen sind unplanbar stochastische Ereignisse.

Für einen ehrlicheren Hybrid-vs-Static-Vergleich haben wir daher einen neuen Benchmark eingeführt:

- **Multi-Profil-Woche**: Montag = Normal, Dienstag = Hochlast, Mittwoch = Notfallwoche, Donnerstag = SLA-Katastrophe, Freitag = Chaos. Der statische Solver kann genau eine Gewichts-Konfiguration haben und ist damit auf genau einem Tag „zu Hause"; an den anderen Tagen ist er de-facto fehljustiert.
- **Stochastische Intraday-Events**: Aus den neuen Profilfeldern (`intraday_krank_rate`, `intraday_verlaengerung_rate`, `intraday_absage_rate`, `intraday_stau_rate`) werden pro Tag seed-gesteuert zufällige Störungen gewürfelt. Für das Chaos-Profil etwa 15 % Krankmeldungs-Wahrscheinlichkeit pro Techniker/Tag, 4 % Verlängerungs-Wahrscheinlichkeit pro Auftrag, 30 % Stau-Wahrscheinlichkeit pro Tag.
- **Drei statische Kalibrierungen** als Baselines:
  - `ortools-normal`: die bisherige Default-Kalibrierung
  - `ortools-chaos-safe`: vorsichtig hoch gesetzte Penalties („wird wild, nicht leichtfertig droppen")
  - `ortools-sla-boost`: SLA-Boni verdoppelt
- **20 Seeds** pro Scheduler; Ergebnisse aggregiert als Mittelwert ± Standardabweichung.

Der vollständige Benchmark läuft unter `bench/multiseed_benchmark.py` und wurde mit 20 Seeds ausgeführt.

**Ergebnisse über 20 Seeds (Mittelwert ± Standardabweichung):**

| Scheduler | Completion | Prio-gew. Completion | SLA-Vlz | Fahrzeit (min) |
|---|---|---|---|---|
| Heuristik            | 55.05 % ± 2.64 | 65.88 % ± 2.17 | 32.60 ± 5.25 | 5648 ± 208 |
| OR-Tools naiv        | 67.64 % ± 1.68 | 73.27 % ± 1.73 | **17.25 ± 3.58** | 4690 ± 154 |
| OR-Tools normal      | 68.75 % ± 1.63 | 73.94 % ± 1.46 | 19.05 ± 3.52 | 4678 ± 185 |
| OR-Tools chaos-safe  | 68.11 % ± 1.90 | 73.24 % ± 2.11 | 18.55 ± 3.33 | 4678 ± 179 |
| OR-Tools sla-boost   | 68.36 % ± 1.55 | 73.63 % ± 1.59 | 17.95 ± 3.10 | **4663 ± 156** |
| Hybrid               | 67.86 % ± 1.88 | 72.67 % ± 1.95 | 18.10 ± 2.94 | 4667 ± 161 |

Die zusätzliche Baseline `ortools-naiv` verwendet bewusst niedrige Penalties (`base_penalty=2 000`, `notfall_bonus=20 000`) — also die Größenordnung, in der die Fahrtkosten ähnlich stark wiegen wie der Drop-Penalty. Das entspricht plausibel der Einstellung, die ein Disponent ohne OR-Hintergrund als „wirkt vernünftig" wählen würde. Sie wurde aufgenommen, um quantitativ zu prüfen, ob der Hybrid wenigstens gegen eine **schwache** Kalibrierung einen messbaren Vorteil hat.

**Interpretation der Multi-Seed-Ergebnisse:**

1. **Heuristik ist klar schwächer.** Mit 13.7 Prozentpunkten Abstand zu den Solver-Varianten und einer 1.7× höheren SLA-Verletzungsrate ist die Greedy-Insertion unter dieser Last nicht wettbewerbsfähig. Dieser Befund ist der einzige robuste Performance-Unterschied im gesamten Set.

2. **Zwischen allen vier OR-Tools-Kalibrierungen und dem Hybrid gibt es keinen statistisch signifikanten Unterschied** — auch nicht für die bewusst schwache `naiv`-Einstellung. Die Differenzen in Completion (0.3–1.1 pp), SLA-Verletzungen (−1.8 bis +1.8) und Fahrzeit (±27 min) liegen alle innerhalb einer Standardabweichung. OR-Tools ist in seinem robusten Arbeitsbereich offenbar deutlich weniger sensitiv gegenüber Penalty-Variation, als die Einzel-Tests in §7.3 vermuten ließen.

3. **Der Hybrid liefert keinen messbaren Leistungsgewinn** — weder gegen die gut kalibrierten Baselines noch gegen die naive. Die Standardabweichung des Hybrid ist mit ±2.94 die kleinste (vs. ±3.10–3.58), was auf etwas konsistentere Ergebnisse hindeutet, aber als isoliertes Signal nicht ausreicht.

4. **Das ändert auch die Mittelstand-These in §6.** Sie muss differenziert werden: der Anwender verliert bei naiver Kalibrierung nicht signifikant Performance. Der ursprünglich vermutete Hybrid-Mehrwert („rettet vor schlechter Kalibrierung") ist unter den hier getesteten Kalibrierungen **nicht empirisch nachweisbar**. Möglich, dass er bei extremeren Fehl-Kalibrierungen (z. B. `travel_weight_pct=500`) auftritt — das haben wir nicht getestet.

5. **Der Einzel-Seed-Befund in §5.3 ist erwartungsgemäß nicht reproduzierbar.** Die dort gefundenen Differenzen (+1 Auftrag, −4 SLA im Chaos) liegen klar innerhalb der Streuung.

**Konsequenz für das wissenschaftliche Statement:**

> Unter den getesteten Bedingungen (Multi-Profil-Woche, stochastische Intraday-Störungen, 20 Seeds) liefert keine der getesteten OR-Tools-Kalibrierungen — inklusive einer bewusst naiven — und auch nicht der LLM-geführte Hybrid einen **statistisch signifikanten Performance-Gewinn** gegeneinander. Der einzige robuste Unterschied ist der Abstand zur Heuristik (−13 pp Completion). Alle Penalty-Kalibrierungen liegen im Rauschen; der Wert des Hybrid lässt sich aus diesen Daten nicht numerisch belegen.
>
> Das **strukturelle** Argument aus §6 bleibt nicht-quantitativ: der Hybrid ersetzt eine Kalibrierungs-Kompetenz, die im Mittelstand nicht existiert. Ob das einen praktischen Unterschied macht, hängt davon ab, ob in der realen Welt extremere Fehl-Kalibrierungen auftreten als die hier getesteten — das ist eine Hypothese, keine gemessene Tatsache.

Der vollständige Benchmark ist reproduzierbar via:
```bash
.venv/bin/python bench/multiseed_benchmark.py --seeds 20 --llm-model claude-sonnet-4-6 --solver-time-limit 3
```

Rohdaten in `bench/results_multiseed.csv`, Aggregation in `bench/results_multiseed_aggregated.csv`.

### 5.6 Replan-Test: löst tagesdynamische Anpassung den Hybrid-Vorteil aus?

Nach dem Multi-Seed-Haupttest (§5.5) blieb genau eine ungetestete Situation, in der der Hybrid theoretisch glänzen sollte: **intraday Re-Planning nach Ereignissen, die zum Planungszeitpunkt nicht bekannt waren**. Die Hypothese lautete: der Morgenplan ist ein eindimensionales Problem (viele Aufträge → möglichst viele unterbringen), aber eine Replanung um 11:40 Uhr nach Krankmeldung ist ein strategisches Multi-Trade-off (welche Stops umverteilen, welche auf morgen schieben, welche Notfälle unbedingt heute — abhängig von Tagesprofil und Restschicht). Genau dort könnte das LLM mit Kontext-Verständnis sich von einer statischen Kalibrierung absetzen.

Um diesen Test durchzuführen, wurden drei zusätzliche Subsysteme implementiert:
- **Intraday-Replan-Trigger** im Simulator: Krankmeldung, Notfall, Stau und Auftragsverlängerung rufen den Scheduler zur Event-Zeit erneut auf. Kundenabsagen triggern keinen Replan (Tech hat nur eine Lücke, macht den nächsten Stop wie geplant).
- **World-Snapshot**: pro Techniker aktuelle Position, `next_free`, Pausen-Status, bereits erledigte Stops — damit der Scheduler den Rest des Tages mit korrektem Anfangszustand planen kann. Alle drei Scheduler (Heuristik, OR-Tools, Hybrid) lernen den Replan-Modus.
- **Tagesverlauf-Kontext** für den Hybrid: `replanungen_heute_bisher`, `trigger_event_typ`, `bisher_erledigt_heute`, `rest_schicht_pro_tech_min`, `pending_pro_tech_min`. Der System-Prompt wurde bewusst **neutral** formuliert („es ist deine Aufgabe zu entscheiden, ob und wie diese Felder deine Wahl beeinflussen") — ohne eingebaute Strategieheuristik, damit die LLM-Leistung nicht durch vorgekauten Regelcode gemessen wird. Zusätzlich: eine Diagnose-Klausel, die das LLM zwingt, im Reasoning explizit zu benennen, ob der Kontext die Entscheidung beeinflusst hat.

**Testaufbau:** dieselbe Multi-Profil-Woche wie in §5.5 (Mo Normal … Fr Chaos, stochastische Intraday-Events), aber mit **aktiviertem Replan-Trigger**. Drei Varianten verglichen:

- **Ohne Replan** (Referenz aus §5.5): Events wirken auf den fertigen Plan, werden aber passiv nur als Rollover/Storno verarbeitet.
- **Replan (dumb)**: Scheduler wird bei Trigger-Event erneut aufgerufen, bekommt aber **keine** Tagesverlaufs-Kontextinformation — nur den Welt-Snapshot und die pending Aufträge.
- **Replan + Context**: Scheduler erhält zusätzlich den `ReplanKontext` mit den Tagesverlauf-Feldern (nur Hybrid nutzt sie aktiv; Heuristik und OR-Tools haben keine Stelle, sie einzuspeisen).

**Ergebnisse über 10 Seeds (Replan-Varianten) und 20 Seeds (Referenz ohne Replan):**

| Scheduler | Variante | Completion | SLA-Vlz | Fahrzeit |
|---|---|---|---|---|
| Heuristik | ohne Replan | 55.05 % ± 2.64 | 32.60 ± 5.25 | 5648 ± 208 |
| Heuristik | Replan | 57.92 % ± 2.03 | 28.90 ± 6.28 | 5713 ± 182 |
| OR-Tools naiv | ohne Replan | 67.64 % ± 1.68 | 17.25 ± 3.58 | 4690 ± 154 |
| OR-Tools naiv | Replan | 66.02 % ± 2.20 | 19.20 ± 5.98 | 4417 ± 254 |
| OR-Tools normal | ohne Replan | **68.75 % ± 1.63** | 19.05 ± 3.52 | 4678 ± 185 |
| OR-Tools normal | Replan | 67.33 % ± 2.28 | 20.10 ± 4.48 | 4400 ± 236 |
| Hybrid | ohne Replan | 67.86 % ± 1.88 | 18.10 ± 2.94 | 4667 ± 161 |
| Hybrid | Replan (dumb) | 66.58 % ± 2.43 | 19.70 ± 3.62 | 4411 ± 214 |
| Hybrid | Replan + Context | 66.78 % ± **1.73** | 19.30 ± 3.68 | 4396 ± 179 |

**Drei Befunde:**

1. **Replan hilft nur der Heuristik.** Sie gewinnt +2.87 pp Completion und −3.7 SLA-Verletzungen, weil sie vorher Stops von ausgefallenen Technikern einfach fallen ließ und jetzt beginnt, überhaupt umzuverteilen. OR-Tools und Hybrid verlieren beide 1.3–1.6 pp Completion durch Replan — das ist der **„small-problem-forgives-all"-Effekt**: viele kurze Solver-Aufrufe (3 s × N Events) sind zusammen schwächer als ein langer Initialplan. Jede Replan-Iteration fragmentiert bestehende Touren, und bei 5–8 pending Stops ist der Optimierungs-Spielraum ohnehin minimal.

2. **Der Tagesverlauf-Kontext bringt dem Hybrid keinen Performance-Vorteil.** Dumb → Context: Completion +0.20 pp, SLA −0.40, Fahrt −15 min. Alle drei Differenzen liegen in den Nachkommastellen der Streuung. Die Hypothese „LLM kann tagesverlauf-sensitiv adaptieren und daraus einen messbaren Gewinn ziehen" ist **empirisch nicht bestätigt**.

3. **Einziger reproduzierbarer Effekt der Context-Variante: die Streuung sinkt.** Completion-Standardabweichung fällt von ±2.43 auf ±1.73, Fahrzeit-Stdev von ±214 auf ±179. SLA-Stdev bleibt stabil. Das heißt: das LLM wird mit zusätzlichem Kontext **konsistenter**, nicht klüger. Eine sinnvolle Interpretation: die zusätzlichen Felder reduzieren die Entscheidungs-Entropie bei Grenzfällen; das LLM landet seltener auf Extrem-Gewichten, weil es mehr fundierten Grund für die Wahl hat. Eine zweite mögliche Interpretation: statistisches Artefakt bei n=10.

**Konsequenz für die Gesamthypothese:**

> Der Hybrid bleibt über alle drei Testsetups hinweg im Rauschen gegen OR-Tools. Die konsistent kleine Differenz geht sogar marginal zulasten des Hybrid (OR-Tools normal 0.58–1.42 pp vorne). Die letzte theoretische Bastion — „intraday Adaptivität mit Kontext" — ist nach diesem Test **widerlegt**, nicht „noch offen".

Rohdaten: `bench/results_replan_preview.csv` (dumb), `bench/results_replan_context.csv` (Context). Reasoning-Ausgaben des LLM zeigen im Mittel, dass der Kontext **gelesen und artikuliert** wird („3. Replan heute, ausgelöst durch Auftragsverlängerung, alle Techniker noch 150 min Restschicht, …") — das LLM ignoriert die Information nicht strukturell, nutzt sie aber offenbar ohne messbaren Ergebnis-Einfluss. Dieser Befund wiegt schwerer als eine stille Nicht-Nutzung, weil er zeigt: die Information kommt an, aber sie hat keinen Hebel auf die Entscheidung im hier getesteten Betriebsfenster.

## 6. Kalibrierungs-Realität im Mittelstand

Die Ergebnisse in §5.1–5.3 suggerieren, dass ein **einmal sorgfältig kalibrierter** statischer Solver dem Hybrid nahe kommt. Das stimmt experimentell — und führt in der Praxis in die Irre.

**Die Kernfrage ist nicht, ob ein OR-Experte die Gewichte optimal setzen kann, sondern ob ein Disponent im Handwerksbetrieb es tut.** Die Antwort ist empirisch klar: er tut es nicht.

Ein typischer Heizungsbaubetrieb in einer mittelgroßen Stadt hat:

- **Keine Operations-Research-Abteilung.** Die Disposition läuft beim Meister oder Büroleiter nebenher.
- **Saisonale Lastverschiebung**, die niemand explizit dokumentiert: Februar ist Notfall-Heizungswoche, April–Mai Wartungssaison, Juni–August Klimageräte-Schwerpunkt, November Winter-Check.
- **Keine Messung des kontrafaktischen Ergebnisses**: wenn die Disposition suboptimal läuft, merkt es niemand — die erledigten Aufträge werden erledigt, die nicht erledigten erscheinen als Rollover, und ob das mit anderen Gewichten besser gewesen wäre, ist nicht beobachtbar.
- **Einstellungs-Kompetenz am oberen Ende des Systems**: wenn dem Disponenten ein System mit sieben Penalty-Gewichten vorgelegt wird, wählt er entweder den Default oder verstellt zwei Slider willkürlich, weil die operationalen Konsequenzen nicht vorhersehbar sind.

**Konsequenz für die Bewertung**: der faire Vergleich ist nicht „Hybrid vs. perfekt-kalibrierter Solver", sondern „Hybrid vs. einmal ab-Werk-eingestellter Solver, der danach nie wieder angefasst wird". Genau das modelliert der Multi-Profil-Benchmark in §5.5.

**Ehrlichkeits-Update nach dem Multi-Seed-Test:** Die ursprüngliche Hypothese dieses Abschnitts — „weil der Mittelstand nicht kalibriert, **verliert** er Performance, und der Hybrid rettet ihn davor" — ist durch die `ortools-naiv`-Baseline (§5.5) **empirisch nicht bestätigt**. Auch die bewusst schwach gewählte Default-Kalibrierung performt im Rahmen der Streuung. Das schwächt die Mittelstand-These nicht auf, verschiebt sie aber:

1. **Es stimmt weiterhin**, dass kein typischer Disponent die Penalty-Gewichte bewusst wählt oder nachjustiert.
2. **Es stimmt nicht (belegt)**, dass diese fehlende Nachjustierung nennenswerte Performance kostet — OR-Tools ist unter den getesteten Bedingungen robuster, als wir zunächst erwartet hatten.
3. **Der verbleibende strukturelle Hybrid-Wert** reduziert sich damit auf: *er erspart dem Anwender eine Entscheidung, die er nicht treffen könnte*. Ob das ein ausreichender Grund ist, die operative Komplexität (LLM-API, Variabilität pro Lauf, Betriebskosten) zu rechtfertigen, ist eine **Geschäfts- und Philosophie-Frage**, keine Performance-Frage.

Zwei Hypothesen bleiben für künftige Experimente offen:

- **Extreme Fehl-Kalibrierung**: führt eine wirklich unplausible Einstellung (z. B. `travel_weight_pct=500`, sehr niedrige Penalties über alle Kategorien) zu messbaren Performance-Einbrüchen? Das würde den ursprünglichen §7.3-Effekt reproduzieren und das Mittelstand-Argument wieder quantitativ unterfüttern.
- **Intraday-Replanning mit Ressourcen-Schock**: wenn mitten am Tag ein Techniker ausfällt und der Scheduler live umplanen muss, spielt die Penalty-Kalibrierung eine andere Rolle — die strategische Entscheidung „welche Stops verschieben wir auf morgen" wird dominanter. Hier könnte der Hybrid wieder eine messbare Rolle spielen. Wir haben es in diesem Prototyp nicht gebaut, siehe §9.

Die Marginal-Beobachtung im Einzel-Seed-Chaos (§5.3) bleibt anekdotisch.

## 7. Diskussion

### 7.0 Quintessenz — ein klassischer Algorithmus schlägt die KI

Das zentrale Resultat dieser Arbeit ist ein Negativbefund mit klarer Aussage:

> **Ein vernünftig modellierter VRPTW-Solver (OR-Tools) ist in dieser Domäne nicht nur ebenbürtig, sondern in allen drei Testsetups marginal überlegen gegenüber dem LLM-geführten Hybrid — selbst wenn der Hybrid genau die Information bekommt, die ihn theoretisch überlegen machen sollte.**

Die drei Stufen der Hypothesen-Prüfung (§5.5, §5.6) haben nacheinander drei theoretisch erwartete Hybrid-Vorteile ausgeschlossen:

| Hypothese | Ergebnis |
|---|---|
| „Hybrid schlägt statische Kalibrierung bei variierenden Tagesprofilen" | Widerlegt (§5.5). OR-Tools ist robust gegen Penalty-Variation; sogar eine naiv gewählte Kalibrierung performt gleichwertig. |
| „Hybrid setzt sich bei intraday Störungen und Replan-Notwendigkeit ab" | Widerlegt (§5.6). Replan hilft nur der Heuristik; Solver und Hybrid verlieren beide leicht. |
| „Mit Tagesverlauf-Kontext kann das LLM tagesabhängig adaptiv reagieren" | Widerlegt (§5.6). Mit oder ohne Context identische Mittelwerte; nur Streuung sinkt minimal. |

**Was das für die KI-Debatte heißt, über diese Domäne hinaus:**

1. **LLMs sind nicht automatisch das bessere Werkzeug.** Für kombinatorische Optimierungsprobleme mit klaren Constraints und sauberem Cost-Modell gibt es seit Jahrzehnten spezialisierte Algorithmen (OR-Tools, Gurobi, CPLEX). Diese Algorithmen sind in ihrem Domänenbereich überlegen — sie lösen nicht mehr, sie lösen das Richtige.

2. **Der vermutete LLM-Hebel — „Kontextverständnis und strategische Adaptivität" — greift nicht automatisch.** Das LLM kann in unserem Test den Kontext **lesen** (messbar im Reasoning), aber die Information übersetzt sich nicht in bessere Entscheidungen, weil der Solver unter der Haube schon robust ist und der zusätzliche strategische Spielraum klein.

3. **„LLM-Augmentation" ist keine freie Verbesserung.** Jede LLM-Komponente bringt API-Kosten, Latenz, Run-zu-Run-Variabilität und operative Abhängigkeiten mit sich. Wenn der Mittelwert-Gewinn null ist, fallen diese Kosten ohne Gegenwert an. Ein guter Algorithmus schlägt einen LLM-Aufsatz nicht nur auf Qualität, sondern auch auf Kostenseite.

4. **Der strukturelle Wert des Hybrid bleibt — ist aber ein Kompetenz-, kein Performance-Argument.** Im Mittelstand (§6) ersetzt der Hybrid eine Kalibrierungs-Entscheidung, die der Endnutzer nicht treffen will oder kann. Das ist ein valider, aber spezifischer Anwendungsfall — keine allgemeine Überlegenheit.

Diese vier Punkte sind das, was in der aktuellen KI-Hype-Welle oft übersehen wird: _Wenn die Domäne ein solides algorithmisches Fundament hat, ist der LLM-Einsatz begründungspflichtig — nicht die Nicht-Nutzung._

### 7.1 Wann lohnt welches Verfahren?

| Situation | Empfehlung | Begründung |
|---|---|---|
| Offline-Batch, <20 Aufträge/Tag | Heuristik | Instant, ausreichend, null Abhängigkeiten. Fachlich deutlich unterlegen gegenüber OR-Tools, aber für kleine Auftragsmengen ausreichend. |
| Produktivbetrieb, egal ob naiv oder sorgfältig kalibriert | **OR-Tools mit statischen Gewichten** (Default-Empfehlung) | Im getesteten Bereich robust gegen Kalibrierungs-Variation (§5.5). Selbst eine bewusst naive Einstellung liefert vergleichbare Ergebnisse wie die beste handgewählte. Deterministisch, keine API-Kosten, keine Laufzeit-Variabilität. |
| Dynamische Tagesstörungen mit Re-Planning-Notwendigkeit | OR-Tools mit Replan-Trigger (§5.6) | Replan bringt OR-Tools nicht zusätzliche Qualität, aber er hält die Pläne **aktuell** und vermeidet stille Nicht-Ausführung. Solver-Kosten je Replan gering (3–4 s). |
| Anwender, der die Kalibrierungs-Entscheidung grundsätzlich nicht treffen will | Hybrid | Performance-gleichwertig zu allen getesteten statischen Kalibrierungen (§5.5, §5.6). Der Wert ist nicht bessere Ergebnisse, sondern der Wegfall der Konfigurations-Entscheidung. Lohnt sich nur wenn LLM-Kosten und Laufzeit-Variabilität akzeptabel sind und eine explizite Kalibrier-Zeremonie vermieden werden soll. |
| Reines Experiment mit LLM als direkter Disponent | LLM-Direct | Nur zur Baseline-Illustration — im Multi-Seed-Test klar unterlegen, nicht für Produktion. |

### 7.2 Kosten und Latenz

| Scheduler | Laufzeit/Woche | Zusatzkosten |
|---|---|---|
| Heuristik | ~0.05 s | 0 |
| OR-Tools | 20 s (4 s × 5 Tage) | 0 |
| LLM-Direct | 55–70 s | ~$0.15/Woche (Sonnet 4.6) |
| Hybrid | 60 s | ~$0.05/Woche (5× kleiner Parameter-Call) |

### 7.3 Kalibrier-Sensitivität der Penalties

In einer frühen Implementierungsphase mit einer naiven Kalibrierung (`base_penalty=2 000`, `notfall_bonus=20 000`) verwarf der Solver systematisch ganze Aufträge, um Fahrzeit zu sparen; Completion fiel auf rund 58 %. Diese Beobachtung motivierte die Anhebung auf das aktuelle Standard-Niveau und rechtfertigte ursprünglich die Hypothese „Kalibrierung ist wichtig, Hybrid könnte sie automatisieren".

**Diese Hypothese hat der Multi-Seed-Test nicht bestätigt.** Exakt dieselbe naive Kalibrierung erreicht unter der neuen Multi-Profil-Woche mit stochastischen Intraday-Events 67.64 % ± 1.68 Completion und damit **keinen signifikanten Abstand** zu den anderen Kalibrierungen (§5.5 „ortools-naiv"-Zeile). Der 58 %-Einbruch war spezifisch für eine frühere OR-Tools-Modellvariante mit `SetBreakIntervalsOfVehicle` (siehe §3.2), die durch die post-hoc Pause-Einfügung ersetzt wurde und Penalty-Variation deutlich weniger sensitiv macht. **Im aktuellen Modell ist OR-Tools in seinem Arbeitsbereich robust gegen realistische Kalibrierungs-Fehler.**

Die praktische Konsequenz: das Argument „statische Gewichte degradieren bei veränderter Last" ist in dieser Implementierung **nicht quantitativ belegbar**. Der Hybrid nimmt dem Anwender die Kalibrierungs-Entscheidung ab, aber unter den hier getesteten Bedingungen hat die Entscheidung selbst praktisch keine Konsequenz.

### 7.4 Validierung der Constraint-Einhaltung

Alle vier Scheduler halten in den getesteten Runs die harten Constraints ein:

- **Schichtende**: Keine Tour endet nach 16:00 (verifiziert für Heuristik und OR-Tools via Single-Day-Check).
- **Pausen**: Jede aktive Tour enthält genau eine Frühstücks- und eine Mittagspause im vorgeschriebenen Fenster.
- **Zeitfenster**: Aufträge mit `fenster_von/bis` werden vom Solver hart erzwungen; bei der Heuristik durch Prüfung im `_try_assign`.

Bei der Heuristik wurde ein früher Bug entdeckt: Pausen wurden nachträglich in bereits vollgeplante Touren eingefügt, wodurch bis zu 83 Minuten Überstunden pro Techniker entstanden. Die Korrektur (effektives Schichtende reduziert um ausstehende Pausen und Rückfahrt) eliminiert das Problem.

---

## 8. Limitationen

**Zur statistischen Absicherung** ist zwischen den Abschnitten zu unterscheiden: Die Detail-Tabellen in §5.1–5.3 basieren auf einzelnen Seeds und sind **illustrativ, nicht belastbar** — die Differenzen dort liegen innerhalb der Streuung. Die statistisch belastbaren Haupttests sind **§5.5 mit 20 Seeds** (statische Kalibrierungen) und **§5.6 mit 10 Seeds** (Replan und Replan+Context). Der §5.6-Test hat aus Kostengründen (LLM-API, Laufzeit) nur 10 Seeds; die Differenzen dort sind aber so durchgehend klein (< 1 Stdev), dass die Aussage „kein signifikanter Hybrid-Vorteil" auch mit doppelter Seed-Zahl nicht kippen würde.

Weitere Einschränkungen:

- **Fahrzeiten rein haversine-basiert** — keine echten Straßendaten, keine Tageszeit- oder Wochentagseffekte (Berufsverkehr). Ein OSRM-Upgrade ist über das `RouteProvider`-Interface vorgesehen.
- **Alle Techniker austauschbar** — keine Skills, keine Erfahrungsstufen, keine Fahrzeug-Kapazitäten (Ersatzteile, Werkzeug).
- **Keine intraday Re-Planung** — Störungen werden angewandt, aber nicht als Trigger für einen neuen Scheduler-Aufruf. Ein krank gemeldeter Techniker wird heute nicht live umverteilt; stattdessen gehen dessen offene Stops in den Rollover. Echte Dispatching-Systeme replannen kontinuierlich.
- **LLM nicht gecacht** — Systeme-Prompt unter Mindestlänge, siehe 4.1. Kosten pro Woche bleiben niedrig, aber Skaleneffekte noch ungenutzt.
- **Auslastung unrealistisch hoch** — 200 % Kapazitätsüberhang testet das Priorisierungsverhalten, entspricht aber selten einer realen Auftragslage.
- **Kalibrier-Bandbreite begrenzt.** Die vier getesteten statischen Kalibrierungen decken den Bereich „naiv" bis „sla-boost" ab, aber keine wirklich pathologischen Fehl-Einstellungen (z. B. `travel_weight_pct=500`, Penalties in Größenordnung der Fahrtzeit halbiert). Es ist denkbar, dass der Hybrid-Vorteil dort wieder sichtbar wird; das haben wir nicht gemessen.
- **Kein intraday Re-Planning.** Der Scheduler wird morgens einmal aufgerufen und plant den ganzen Tag. Störungen werden auf den fertigen Plan angewandt, aber nicht als Trigger für einen neuen Scheduler-Aufruf genutzt. In der Praxis ist eine Krankmeldung um 10:00 Uhr genau der Moment, in dem strategische Priorisierung („welche 3 der 5 offenen Stops schieben wir auf morgen?") wertvoll wird. Diese Situation haben wir in den Messungen nicht abgebildet — siehe §9.

---

## 9. Ausblick

Die beiden ursprünglich als offen markierten Follow-Ups — statistische Absicherung und intraday Re-Planung — sind im Verlauf der Arbeit beide abgeschlossen und haben die zentrale Hybrid-Hypothese nicht gestützt. Was bleibt:

1. **Extrem-Fehlkalibrierung.** Die aktuellen Baselines decken den Bereich „naiv bis sla-boost" ab. Eine wirklich pathologische Konfiguration (z. B. `travel_weight_pct=500`, Penalties in Größenordnung der Service-Zeit) wurde nicht getestet. Plausibel, dass OR-Tools dort zusammenbricht und der Hybrid eine Ausgleichsrolle übernimmt. Das wäre die letzte empirisch offene Bastion für den Hybrid-Wert.

2. **Skill-/Ressourcen-Heterogenität.** Aktuell sind alle Techniker austauschbar. In der Realität haben Betriebe Techniker mit verschiedenen Qualifikationen (Gas, Öl, Wärmepumpe), verschiedenen Erfahrungsstufen und verschiedenen Fahrzeug-Ausstattungen (Ersatzteile). Das fügt eine zweite Zuordnungs-Dimension hinzu, die nicht über Penalties, sondern über Zulässigkeits-Constraints modelliert wird. Ob der Hybrid dort einen Wert hat, ist eine eigene Frage.

3. **Prompt-Caching effektiv nutzen.** System-Prompt um Fallbeispiele und Policy-Katalog auf >4 096 Tokens erweitern. Erwarteter Effekt: ~90 % Kostenreduktion auf wiederkehrenden Input-Tokens. Das ändert nichts an der Ergebnisqualität, macht den Hybrid aber ökonomisch attraktiver — relevant nur wenn er in Produktion verwendet wird.

4. **Operative Validierung im Feld.** Alle hier dargestellten Tests sind simulativ. Die interessanteste Frage ist jetzt, ob die simulierten Ergebnisse mit realen Betriebsdaten übereinstimmen — insbesondere, ob die Kalibrierungs-Robustheit von OR-Tools auch bei echten, saisonal driftenden Lastprofilen hält. Ohne diese Validierung bleibt die These „OR-Tools reicht" eine simulative Hypothese.

Weitere technische Ideen: Ersetzen von Haversine durch OSRM; OR-Tools-Parametrisierung um Solver-Strategie (nicht nur Penalty-Gewichte) LLM-gesteuert auswählen zu lassen — das wäre eine andere Art von LLM-Nutzung als in dieser Arbeit.

Die zentrale Erkenntnis bleibt auch bei diesen Erweiterungen stabil: **in einer Domäne mit etabliertem algorithmischem Fundament ist LLM-Augmentation begründungspflichtig — nicht ihre Nichtnutzung.**

---

## Anhang A: Reproduzierbarkeit

**Code-Baum:**

- [einsatzplaner/generator.py](../einsatzplaner/generator.py) — Datengenerator + `Szenarioprofil`
- [einsatzplaner/scheduler/heuristic.py](../einsatzplaner/scheduler/heuristic.py) — Insertion-Heuristik
- [einsatzplaner/scheduler/ortools_vrp.py](../einsatzplaner/scheduler/ortools_vrp.py) — OR-Tools VRPTW
- [einsatzplaner/scheduler/llm.py](../einsatzplaner/scheduler/llm.py) — LLM-Direct-Scheduler
- [einsatzplaner/scheduler/hybrid.py](../einsatzplaner/scheduler/hybrid.py) — Hybrid-Scheduler
- [einsatzplaner/simulator.py](../einsatzplaner/simulator.py) — Wochenlauf mit Rollover
- [einsatzplaner/evaluator.py](../einsatzplaner/evaluator.py) — Metrik-Berechnung
- [app.py](../app.py) — Streamlit-UI

**Reproduktionsskript** (führt Normal-Preset mit allen vier Schedulern aus):

```python
from datetime import date
import random
from dotenv import load_dotenv

load_dotenv()

from einsatzplaner.generator import generate_techniker, generate_woche, Szenarioprofil
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
events = load_scenario("scenarios/baseline.yaml", montag)

for name, sched in [
    ("heuristik",  HeuristicScheduler()),
    ("ortools",    ORToolsScheduler(time_limit_sec=4)),
    ("llm",        LLMScheduler(model="claude-sonnet-4-6")),
    ("hybrid",     LLMGuidedVRPScheduler(model="claude-sonnet-4-6", time_limit_sec=4)),
]:
    rng = random.Random(42)
    techs = generate_techniker(10, rng)
    woche = generate_woche(montag, rng=rng, profil=Szenarioprofil())
    bekannte = {a.id: a for tag in woche.values() for a in tag}
    we = run_woche(woche, techs, sched, rp, events, "baseline")
    m = compute_metriken(we, bekannte)
    print(f"{name}: {m.erledigt}/{m.generiert} "
          f"({m.completion_rate}% Completion, {m.gesamtfahrzeit_min}min Fahrt)")
```

**Umgebungsvariablen:**
- `ANTHROPIC_API_KEY` — für LLM-Direct und Hybrid

**Dependencies:** siehe [requirements.txt](../requirements.txt).
