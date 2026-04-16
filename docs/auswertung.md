# Wissenschaftliche Auswertung: Vergleich heuristischer, solver-basierter und LLM-gesteuerter Einsatzplanung

**Projekt:** Einsatzplaner — Experiment zur automatisierten Tourenplanung für Servicetechniker eines Heizungsbaubetriebs
**Datum:** 2026-04-15
**Status:** Prototyp / Proof of Concept

---

## Zusammenfassung

Wir vergleichen vier Verfahren zur täglichen Zuweisung von Wartungsaufträgen auf zehn Servicetechniker eines fiktiven Heizungsbaubetriebs in Oldenburg: eine **Insertion-Heuristik**, einen **OR-Tools VRPTW-Solver** mit statischen Priorisierungs-Gewichten, einen **LLM-Direct-Scheduler** (Claude Sonnet 4.6 entscheidet Zuordnung und Reihenfolge) und einen **Hybrid-Ansatz** (LLM setzt tagesaktuelle Priorisierungs-Gewichte, OR-Tools optimiert darauf).

**Kernbotschaft in einem Satz:** _Ein vernünftig konstruierter klassischer VRPTW-Solver (OR-Tools) ist in dieser Domäne dem LLM-gestützten Hybrid durchgehend ebenbürtig bis marginal überlegen — über vier unabhängige Testsetups (inklusive Skill-Heterogenität, 30 Seeds) lässt sich keine Bedingung identifizieren, in der LLM-Augmentation einen statistisch signifikanten Performance-Vorteil liefert._

Unter kontrollierten Einzelprofil-Bedingungen dominiert der **OR-Tools-Solver** mit statischen Gewichten — +11 Prozentpunkte Completion gegenüber der Heuristik bei gleichzeitig 15 % geringerer Gesamtfahrzeit. Der **LLM-Direct-Ansatz** schneidet in allen Messgrößen schlechter ab und zeigt die bekannte Schwäche von LLMs bei großen kombinatorischen Optimierungsproblemen.

Der methodisch belastbare Haupttest ist in vier Stufen aufgebaut, von denen jede eine spezifische Hybrid-Hypothese prüft — alle vier widerlegen sie:

1. **Multi-Profil-Woche mit 20 Seeds (§5.5).** Getestet gegen vier statische Kalibrierungen (`naiv`, `normal`, `chaos-safe`, `sla-boost`). Der Hybrid liegt in Completion, SLA-Verletzungen und Fahrzeit **innerhalb einer Standardabweichung** gegenüber allen vier — auch gegenüber der bewusst schwachen `naiv`-Variante. Die Hypothese „Hybrid schützt vor schlechter Kalibrierung" ist nicht belegt, weil OR-Tools im getesteten Bereich robust gegen Penalty-Variation ist.

2. **Replan-Test (§5.6, 10 Seeds).** Intraday-Störungen (Krankmeldung, Notfall, Stau, Auftragsverlängerung) triggern einen Replan. Alle Scheduler können neu planen. **Kein signifikanter Hybrid-Vorteil** — OR-Tools und Hybrid verlieren beide leicht durch den Replan (−1.3 pp Completion), während die Heuristik als einzige gewinnt (+2.9 pp).

3. **Replan mit Tagesverlauf-Kontext (§5.6).** Der Hybrid bekommt zusätzlich strukturierte Tagesverlauf-Daten. **Kein Mittelwert-Gewinn** gegenüber „Replan ohne Kontext". Einziger messbarer Effekt: die Run-zu-Run-Streuung des Hybrid sinkt (Completion-Stdev von ±2.43 auf ±1.73).

4. **Qualifikations-Constraint (§5.7, 30 Seeds).** Kälteschein für 40 % der Techniker, 20 % der Aufträge erfordern ihn. Ein 10-Seed-Pilotlauf zeigte einen nominellen Hybrid-Vorteil (+0.13 pp, als „richtungsweisend, nicht signifikant" eingeordnet). Der 30-Seed-Absicherungslauf reproduziert diese Richtung **nicht**: nominell liegt jetzt OR-Tools-normal +0.55 pp vorn, aber dieser Abstand liegt selbst bei n=30 noch im Rauschen (Stdev ±2.83–3.05 pp, SEM ≈ 0.53 pp, t ≈ 1.0, p ≈ 0.3). Die korrekte Aussage nach Absicherung ist deshalb nicht „OR-Tools schlägt Hybrid bei Quals", sondern **„kein detektierbarer Effekt in beide Richtungen"** — das 10-Seed-Pro-Hybrid-Signal ist bei 30 Seeds genauso wenig reproduzierbar wie ein Pro-OR-Tools-Signal.

**Konsequenz:** In allen vier Testsetups liefert der Hybrid **keinen statistisch signifikanten Performance-Vorteil** gegenüber einem vernünftig kalibrierten OR-Tools-Solver — auch nicht im ursprünglich als „letzte Bastion" vermuteten Fall der Skill-Heterogenität. Der Wertschlüssel des Hybrid reduziert sich auf das strukturelle Mittelstand-Argument in §6: Ersatz einer Kalibrierungs-Entscheidung, die der Endnutzer nicht explizit trifft. Performance-Zahlen rechtfertigen LLM-Augmentation in dieser Domäne nicht.

Zwei frühere Einzel-Seed-Befunde haben sich unter statistischer Absicherung **nicht bestätigt**: +1 Auftrag, −4 SLA im Chaos (§5.3) und +0.13 pp im Qualifikations-Pilotlauf (§5.7.1). Beide sind im Report als anekdotisch bzw. nicht reproduzierbar markiert. Beide zeigen, warum Seed-Absicherung in dieser Art Experiment nicht optional ist — richtungsweisende Signale können bei n≤10 reines Rauschen sein, selbst wenn sie vorsichtig als „nicht signifikant, aber konsistent gerichtet" interpretiert werden.

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

### 5.7 Qualifikations-Constraint: 10-Seed-Pilotsignal bei 30 Seeds nicht reproduzierbar

In §8 (Limitationen) war der Verdacht geäußert worden, dass unsere Tests das Problem geografisch dominieren — eine dominante Lösungsstrategie (Clustering + Prio) macht alle Scheduler ähnlich gut. Um zu prüfen, ob eine zweite harte Zuordnungs-Dimension den Hybrid-Vorteil aktiviert, wurde ein **Qualifikations-Constraint** eingeführt: 40 % der Techniker haben einen **Kälteschein**, 20 % der Aufträge **erfordern** ihn (Wärmepumpen-Wartung).

**Constraint-Umsetzung:** Alle vier Scheduler respektieren das Constraint **hart**. Heuristik: `_try_assign` gibt `None` zurück bei unqualifiziertem Techniker. OR-Tools: `routing.VehicleVar(node).SetValues([vid für qualifizierte Fahrzeuge])`. LLM-Direct: Prompt enthält Qualifikationen + Tour-Check. Hybrid: nutzt OR-Tools-Constraint unter der Haube.

**Test-Setup:** Dieselbe Multi-Profil-Woche wie §5.5, dieselbe 8 h-Nettoarbeitszeit. Einziger Unterschied: Qualifikations-Constraint an/aus.

#### 5.7.1 Erster Lauf (10 Seeds) — scheinbar positives Signal

| Scheduler | Baseline (ohne Quals) | Mit Qualifikations-Constraint |
|---|---|---|
| OR-Tools normal | **74.54 % ± 2.04** | 74.36 % ± 2.62 |
| Hybrid | 74.50 % ± 2.29 | **74.49 % ± 2.68** |

Der erste Lauf mit 10 Seeds zeigte ein scheinbar interessantes Bild: Hybrid rückt im Qualifikations-Fall nominell vor OR-Tools (+0.13 pp), und der Hybrid verliert mit −0.01 pp am wenigsten durch das Constraint, während OR-Tools-naiv −0.55 pp und OR-Tools-normal −0.18 pp einbüßen. Die Richtung wirkte konsistent mit der §8-These „LLM-Adaption gewinnt Wert bei höherer Problem-Dimensionalität".

Wir haben das im ersten Bericht als **schwaches positives Signal** gekennzeichnet — statistisch nicht signifikant (<1 Stdev), aber konsistent gerichtet. Ausdrücklich mit dem Hinweis, dass 30 Seeds Klärung bringen sollten.

#### 5.7.2 Reproduktion (30 Seeds) — Pilotsignal nicht reproduzierbar

Der 30-Seed-Lauf im Folgenachts-Zyklus reproduziert die 10-Seed-Richtung nicht:

| Scheduler | Baseline (ohne Quals, 30 Seeds) | Mit Qualifikations-Constraint (30 Seeds) |
|---|---|---|
| Heuristik | 63.71 % ± 2.83 · SLA 25.3 | 63.50 % ± 3.00 · SLA 23.4 |
| OR-Tools naiv | 73.55 % ± 2.91 · SLA 14.7 | 73.12 % ± 3.41 · SLA 16.9 |
| OR-Tools normal | 73.70 % ± 2.74 · SLA 16.6 | **74.18 % ± 2.83** · SLA 16.9 |
| **Hybrid** | **73.77 % ± 2.91** · SLA 14.7 | 73.63 % ± 3.05 · SLA 16.8 |

**Die nominellen Rangfolgen drehen sich gegenüber dem 10-Seed-Lauf um — beide Richtungen im Rauschen:**

| Vergleich | 10 Seeds | 30 Seeds |
|---|---|---|
| Hybrid vs. OR-Tools-normal, ohne Quals | OR-Tools +0.04 pp | Hybrid +0.07 pp |
| Hybrid vs. OR-Tools-normal, mit Quals | Hybrid +0.13 pp | OR-Tools +0.55 pp |
| Δ Hybrid durch Quals | −0.01 pp | −0.14 pp |
| Δ OR-Tools-normal durch Quals | −0.18 pp | +0.48 pp |

**Statistische Einordnung des 30-Seed-Laufs:** Stdev je Zelle ±2.83–3.41 pp → SEM ≈ 0.53–0.62 pp. Der nominelle OR-Tools-Vorsprung von 0.55 pp mit Quals liegt bei etwa 1 SEM, t ≈ 1.0, p ≈ 0.3. Das ist **nicht signifikant**. Keiner der gezeigten 30-Seed-Abstände erreicht Signifikanz (alle nominellen Effekte bleiben < 1 pp bei ±3 pp Streuung).

Die ehrliche Aussage ist deshalb nicht „OR-Tools schlägt jetzt den Hybrid bei Quals". Sie lautet:

- Das 10-Seed-Pro-Hybrid-Signal (+0.13 pp) ist bei 30 Seeds **nicht reproduzierbar**.
- Die 30-Seed-Nominal-Richtung (+0.55 pp Pro-OR-Tools) ist selbst bei n=30 **ebenfalls nicht signifikant**.
- Was die Absicherung tatsächlich belegt: **es gibt in diesem Setup keinen detektierbaren Effekt in irgendeine Richtung**. Das 10-Seed-Signal war eine zufällige Rauschausprägung, die bei Verdopplung der Seed-Zahl sogar das Vorzeichen wechselt.

**Konsequenz:**

1. **Die Hybrid-Hypothese bleibt auch bei Skill-Heterogenität unbestätigt.** Die §8-Spekulation, dass mehrdimensionale Constraints dem Hybrid einen echten Hebel geben, ist damit für die hier getesteten Bedingungen **nicht nachgewiesen**. Der Hybrid liegt auch mit Qualifikations-Constraint im statistischen Rauschen gegen einen vernünftig kalibrierten OR-Tools-Solver — und umgekehrt gilt dasselbe.

2. **Lessons Learned zur Methodik.** Der 10-Seed-Lauf produzierte einen zufallsbedingten Richtungseffekt, den wir vorsichtig als „konsistent gerichtet, nicht signifikant" eingeordnet hatten. Die 30-Seed-Absicherung zeigt: auch solche vorsichtigen Interpretationen können trügerisch sein, wenn die Seed-Zahl zu klein ist. In Zukunft: bei richtungsrelevanten Signalen, deren Absolutwert unter einer Stdev liegt, mindestens 30 Seeds — lieber 50. Wir haben den ursprünglichen §5.7-Befund bewusst als „schwaches Signal" eingeordnet — das reicht für den Bericht nicht aus, wenn der Befund die Kernthese berührt.

3. **Die §7.0-Quintessenz gilt damit durchgehend.** Alle vier Testsetups — inklusive des vorher hoffnungsvollen §5.7 — zeigen: in dieser Domäne liefert LLM-Augmentation keinen messbaren Performance-Vorteil gegenüber einem vernünftig kalibrierten OR-Tools-Solver. Der Effekt ist weder bei variierenden Lastprofilen, noch bei intraday Replan-Dynamik, noch bei Tagesverlauf-Kontext, noch bei Skill-Heterogenität sichtbar.

Rohdaten: `bench/results_baseline_8h.csv` / `bench/results_qualifications.csv` (10 Seeds, archiviert) und `bench/results_baseline_30seeds.csv` / `bench/results_qualifications_30seeds.csv` (30 Seeds, belastbar).

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

Das zentrale Resultat dieser Arbeit ist ein durchgehender Negativbefund mit klarer Aussage:

> **Ein vernünftig modellierter VRPTW-Solver (OR-Tools) ist in dieser Domäne ebenbürtig bis marginal überlegen gegenüber dem LLM-geführten Hybrid — über vier unabhängige Testsetups inklusive Skill-Heterogenität (30 Seeds) lässt sich keine Bedingung identifizieren, in der LLM-Augmentation einen statistisch signifikanten Performance-Vorteil liefert.**

Die vier Stufen der Hypothesen-Prüfung:

| Hypothese | Ergebnis |
|---|---|
| „Hybrid schlägt statische Kalibrierung bei variierenden Tagesprofilen" (§5.5) | Nicht belegt. OR-Tools robust gegen Penalty-Variation. |
| „Hybrid setzt sich bei intraday Störungen und Replan-Notwendigkeit ab" (§5.6) | Nicht belegt. Replan hilft nur der Heuristik. |
| „Mit Tagesverlauf-Kontext kann das LLM tagesabhängig adaptiv reagieren" (§5.6) | Nicht belegt. Mit oder ohne Context identische Mittelwerte. |
| „Bei Skill-Heterogenität zeigt sich der Hybrid-Vorteil" (§5.7) | **Nicht belegt.** 10-Seed-Pilotsignal (+0.13 pp Hybrid) bei 30 Seeds nicht reproduzierbar; die nominelle 30-Seed-Richtung (+0.55 pp OR-Tools) ist selbst bei n=30 nicht signifikant (p ≈ 0.3). Netto: kein detektierbarer Effekt in beide Richtungen. |

**Warum der Solver so schwer zu schlagen ist: die flache Gewichts-Landschaft.**

Es wäre falsch, die OR-Tools-Überlegenheit auf besonders geschickt gewählte Penalty-Gewichte zurückzuführen. Der Befund in §5.5 sagt genau das Gegenteil: selbst die bewusst schwach gewählte `ortools-naiv`-Variante liegt statistisch gleichauf mit `normal`, `chaos-safe` und `sla-boost`. Die `normal`-Kalibrierung ist nicht in einem schmalen Optimum, sondern irgendwo in einer großzügigen **robusten Plateau-Zone**.

Das ist — nicht eine besondere handwerkliche Leistung der `normal`-Gewichte — der eigentliche Grund, warum der Hybrid in dieser Domäne keinen Hebel findet: wenn die Gewichts-Landschaft in einer breiten Zone flach ist, kann tägliches Umparametrisieren per LLM nichts herausholen, egal wie intelligent das LLM seine Auswahl trifft. Das LLM wählt jeden Tag einen Punkt **innerhalb** der Plateau-Fläche — die klassische Strategie wählt einen festen Punkt **im selben** Plateau. Beide liefern denselben Output.

Diese Robustheit ist modell-spezifisch. In einer früheren Variante mit `SetBreakIntervalsOfVehicle` war die Landschaft deutlich empfindlicher (ein 58 %-Completion-Einbruch bei `base_penalty=2000`, §7.3). Der Wechsel auf die post-hoc Pause-Einfügung hat nebenbei die Kalibrierungs-Sensitivität fast eliminiert — ein Seiteneffekt, nicht ein bewusstes Design-Ziel, aber eine direkte Folge davon, wie die Pausen jetzt außerhalb des Routings gehandhabt werden.

**Praktische Konsequenz:** Für ein Produktivsystem in genau dieser Größenordnung (10 Techniker, 40–50 Aufträge/Tag, 30-km-Radius, Standard-SLA- und Zeitfenster-Profile, optional Kälteschein-Constraint) sind die Gewichte der `normal`-Kalibrierung bereits in der guten Zone — ein Weiterkalibrieren würde keinen messbaren Gewinn bringen. Bei strukturellen Sprüngen (50+ Techniker, Multi-Depot, 4+ gleichzeitige Qualifikationen, deutlich andere Auftragsdichten) ist die flache Zone nicht verifiziert und sollte neu vermessen werden.

**Was das für die KI-Debatte heißt, über diese Domäne hinaus:**

1. **LLMs sind nicht automatisch das bessere Werkzeug.** Für kombinatorische Optimierungsprobleme mit klaren Constraints und sauberem Cost-Modell gibt es seit Jahrzehnten spezialisierte Algorithmen (OR-Tools, Gurobi, CPLEX). In ihrem Domänenbereich sind sie überlegen — nicht weil sie mehr lösen, sondern weil sie das Richtige lösen.

2. **Der vermutete LLM-Hebel — „Kontextverständnis und strategische Adaptivität" — greift nicht automatisch.** Das LLM kann in unseren Tests den Kontext **lesen** (messbar im Reasoning), aber die Information übersetzt sich nicht in bessere Entscheidungen. Auch wenn zusätzliche Zuordnungs-Dimensionen wie Qualifikationen hinzukommen (was theoretisch dem Hybrid helfen sollte), bleibt der Performance-Vorteil unter statistischer Absicherung aus.

3. **„LLM-Augmentation" ist keine freie Verbesserung.** API-Kosten, Latenz, Run-zu-Run-Variabilität und operative Abhängigkeiten sind real. Wenn der Mittelwert-Gewinn null ist, fallen diese Kosten ohne Gegenwert an. Ein guter Algorithmus schlägt einen LLM-Aufsatz nicht nur auf Qualität, sondern auch auf Kostenseite.

4. **Methodik-Lesson: kleine Seed-Zahlen täuschen.** Der §5.7-Pilotlauf (10 Seeds) zeigte einen scheinbar konsistent gerichteten Hybrid-Vorteil, den wir vorsichtig als „nicht signifikant, aber richtungsweisend" eingeordnet hatten. Der 30-Seed-Absicherungslauf liefert die Nicht-Reproduktion: die Richtung dreht sich nominell um, und auch die gedrehte Richtung bleibt im Rauschen (p ≈ 0.3). Was korrekt bleibt nach Absicherung ist nicht „OR-Tools ist besser", sondern **„kein detektierbarer Effekt in beide Richtungen"**. Schlussfolgerung: bei richtungsrelevanten Signalen, deren Absolutwert <1 Stdev liegt, ist auch vorsichtige Interpretation trügerisch. Seed-Absicherung ist in solchen Experimenten keine Option, sondern Voraussetzung.

5. **Der strukturelle Wert des Hybrid bleibt — ist aber ein Kompetenz-, kein Performance-Argument.** Im Mittelstand (§6) ersetzt der Hybrid eine Kalibrierungs-Entscheidung, die der Endnutzer nicht treffen will oder kann. Das ist ein valider, aber spezifischer Anwendungsfall — keine allgemeine Überlegenheit.

Diese Punkte sind das, was in der aktuellen KI-Hype-Welle oft übersehen wird: _In einer Domäne mit etabliertem algorithmischem Fundament ist LLM-Einsatz begründungspflichtig — nicht die Nicht-Nutzung. Diese Begründungspflicht haben wir in keinem der vier Testsetups einlösen können._

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

- **Schichtende**: Keine Tour endet nach dem konfigurierten Schichtende (default 17:00 bei 8h Netto + 60 min Pausen; bei Überstunden-Modus 19:00 bei 10h Netto). Verifiziert für Heuristik und OR-Tools via Single-Day-Check.
- **Pausen**: Jede aktive Tour enthält genau eine Frühstücks- und eine Mittagspause. Die Mittagspause liegt garantiert im gesetzlichen Fenster 11:30–13:30 (`visualization.py` erzwingt das; wenn kein freier Slot im Fenster existiert, wird ein Auftrag gesplittet).
- **Zeitfenster**: Aufträge mit `fenster_von/bis` werden vom Solver hart erzwungen; bei der Heuristik durch Prüfung im `_try_assign`.
- **Qualifikationen**: Kälteschein-pflichtige Aufträge werden ausschließlich qualifizierten Technikern zugewiesen (alle Scheduler, verifiziert auf 0 Verletzungen über 10 Seeds).
- **Arbeitszeit-Caps**: Tägliche Netto-Arbeitszeit ≤ profil-definiertes Limit (default 480 min = 8h, max 600 min = 10h). Wochen-Cap wird im Simulator pro Techniker kumuliert; bei Erreichen fällt der Techniker für den Rest der Woche ganztägig aus.

Zwei frühere Bugs wurden im Verlauf des Projekts entdeckt und behoben:
- Heuristik: Pausen wurden nachträglich in vollgeplante Touren eingefügt und verursachten bis zu 83 min Überstunden pro Techniker. Fix: effektives Schichtende um ausstehende Pausen und Rückfahrt reduziert.
- Gantt-Visualizer: Mittagspause-Slot-Suche nahm den letzten passenden Slot (auch außerhalb des Fensters), statt den ersten im Mittagsfenster. Fix: Slot-Suche auf `[mittag_von, mittag_bis]` eingeschränkt, bei keiner freien Lücke wird ein Auftrag gesplittet.

---

## 8. Limitationen

**Zur statistischen Absicherung** ist zwischen den Abschnitten zu unterscheiden: Die Detail-Tabellen in §5.1–5.3 basieren auf einzelnen Seeds und sind **illustrativ, nicht belastbar** — die Differenzen dort liegen innerhalb der Streuung. Die statistisch belastbaren Haupttests sind **§5.5 mit 20 Seeds** (statische Kalibrierungen bei 7h Netto-Basis — Zahlen sind tendenziell niedriger, Struktur der Befunde aber gleich), **§5.6 mit 10 Seeds** (Replan, Replan+Context bei 8h Netto-Basis) und **§5.7 mit 30 Seeds** (Qualifikations-Constraint bei 8h Netto-Basis; die 10-Seed-Pilotzahlen sind im Bericht dokumentiert und archiviert, aber nicht mehr interpretationsführend).

**Lessons Learned zur Seed-Größe.** Der §5.7-Pilotlauf (10 Seeds) produzierte einen scheinbar konsistent gerichteten Hybrid-Vorteil (+0.13 pp Completion bei Quals, −0.01 pp Constraint-Verlust). Wir hatten das vorsichtig als „richtungsweisend, aber nicht signifikant" eingeordnet — exakt die Formulierung, die sich bei enger Interpretation absichern soll. Der 30-Seed-Lauf hat die Richtung **umgedreht** (OR-Tools-normal +0.55 pp bei Quals, Hybrid −0.14 pp Constraint-Verlust). Daraus zwei methodische Regeln für zukünftige Benchmarks in dieser Umgebung:

1. **Bei Effekt-Größen < 1 Stdev sind 10 Seeds zu wenig — auch für Richtungs-Aussagen.** Die Seed-Stdev liegt in unseren Tests bei ±2.0 bis ±3.0 pp Completion. Ein nomineller Effekt von 0.1–0.5 pp kann damit reines Seed-Rauschen sein, und die Rangfolge kann sich bei Verdopplung des Seed-Samples umkehren. Für richtungsweisende Aussagen in diesem Rauschbereich: Mindestens 30 Seeds, lieber 50.
2. **Eine „nicht signifikant, aber richtungsweisend"-Einordnung ist keine Absicherung, sondern ein Risiko-Hinweis.** Wenn ein Befund die Kernthese berührt (wie §5.7 die Frage „gibt es irgendwo einen Hybrid-Vorteil?"), reicht diese Einordnung für einen belastbaren Bericht nicht aus. Entweder wird das Signal abgesichert, oder es wird nicht als Befund kommuniziert. In diesem Bericht haben wir beides getan: §5.7.1 ist als **Pilot mit widerlegter Hypothese** stehen geblieben (Nachvollziehbarkeit), die Kernaussagen in §7.0 und Zusammenfassung stehen aber auf §5.7.2 (30 Seeds).

Weitere Einschränkungen:

- **Fahrzeiten rein haversine-basiert** — keine echten Straßendaten, keine Tageszeit- oder Wochentagseffekte (Berufsverkehr). Ein OSRM-Upgrade ist über das `RouteProvider`-Interface vorgesehen.
- **Problem-Dimensionalität begrenzt.** Der §5.7-Befund legt nahe, dass der Hybrid-Vorteil mit der Dimensionalität skaliert. Wir haben nur eine zusätzliche Dimension (Kälteschein) getestet. Mehrere gleichzeitige Qualifikationen (Gas, Öl, Wärmepumpe, …), Ersatzteil-Constraints, Techniker-Erfahrungsstufen oder Auftrags-Abhängigkeiten bleiben ungetestet.
- **LLM-Prompt-Caching greift nicht** — System-Prompt unter der Mindestlänge für Caching (siehe 4.1). Kosten pro Woche bleiben niedrig, Skaleneffekte noch ungenutzt.
- **Auslastung im Chaos-Preset unrealistisch hoch** — ~200 % Kapazitätsüberhang testet das Priorisierungsverhalten, entspricht aber selten einer realen Auftragslage. Der realistische Lastbereich liegt eher bei 100–130 %.
- **Kalibrier-Bandbreite begrenzt.** Die vier getesteten statischen Kalibrierungen decken den Bereich „naiv" bis „sla-boost" ab, aber keine wirklich pathologischen Fehl-Einstellungen (z. B. `travel_weight_pct=500`). Es ist denkbar, dass der Hybrid-Vorteil dort stärker sichtbar wird.
- **Intraday Re-Planung funktional, aber nicht exotisch.** Der Replan wird bei Krankmeldung, Notfall, Stau und Auftragsverlängerung ausgelöst. Dispatching-Spezialfälle wie „Tech ist bei falschem Kunden angekommen und braucht sofortige Umverteilung" oder „System schlägt aktiv alle 30 min eine Replanung vor, egal ob Event" wurden nicht getestet.

---

## 9. Ausblick

Vier ursprüngliche Follow-Ups wurden im Projektverlauf abgearbeitet: statistische Absicherung (§5.5, 20 Seeds), intraday Re-Planung (§5.6, 10 Seeds), Skill-Heterogenität als Pilot (§5.7.1, 10 Seeds) und die Nachabsicherung dieses Pilot-Befundes (§5.7.2, 30 Seeds). Alle vier liefern denselben Negativbefund. Die interessanten offenen Fragen verschieben sich damit **weg von „ist das 10-Seed-§5.7-Signal echt?"** (erledigt — es war es nicht) **hin zu strukturell anderen Hypothesen**.

1. **Höhere Dimensionalität.** Eine einzelne Qualifikation hat den Hybrid-Vorteil nicht aktiviert. Die strukturell nächste offene Frage ist, ob **mehrere gleichzeitige** Zuordnungs-Dimensionen das Bild ändern: Gas / Öl / Wärmepumpe / Solarthermie parallel, Ersatzteil-Constraints pro Fahrzeug, Erfahrungslevel-gerechte Zuweisung, Auftrags-Abhängigkeiten („erst A dann B innerhalb 24 h"). Theoretisch wächst der Matching-Raum kombinatorisch mit jeder Dimension — ob OR-Tools dort an Grenzen stößt oder ob das LLM einen Reasoning-Vorteil findet, ist bei einer einzelnen Dimension nicht beantwortet worden.

2. **Extrem-Fehlkalibrierung.** Die aktuellen Baselines decken „naiv bis sla-boost" ab, aber keine wirklich pathologischen Fehl-Einstellungen (`travel_weight_pct=500`, Penalties in Fahrzeit-Größenordnung). Bei solchen Konfigurationen könnte OR-Tools zusammenbrechen und der Hybrid den Ausgleich machen. Die empirisch offene Bastion — und durch §5.5 nicht ausgeschlossen, nur nicht betreten.

3. **Operative Validierung im Feld.** Alle Tests hier sind simulativ. Die interessanteste offene Frage ist, ob sich die hier stabil negativen Mittelwerte auch an realen Betriebsdaten halten — insbesondere ob das LLM bei echten, multi-dimensionalen Lastprofilen (Qualifikationen, Wartungsverträge, saisonale Drift, kundenbezogene Präferenzen) einen Hebel findet, den ein statisch kalibrierter Solver nicht sieht. Ohne Felddaten bleibt der Schluss simulativ.

4. **Prompt-Caching effektiv nutzen.** System-Prompt auf >4 096 Tokens erweitern (Fallbeispiele, Policy-Katalog) für ~90 % Kostenreduktion auf wiederkehrenden Input-Tokens. Betrifft die Ökonomie des Hybrid, nicht die Qualität — aber relevant, wenn trotz fehlendem Performance-Vorteil der Hybrid aus strukturellen Gründen (§6) eingesetzt wird.

5. **Methodische Absicherung für Folgestudien.** Bei allen künftigen Tests mit Effekt-Größen im einstelligen pp-Bereich: mindestens 30 Seeds ansetzen, lieber 50. Die §5.7-Erfahrung zeigt, dass auch vorsichtig interpretierte 10-Seed-Richtungs-Signale trügerisch sein können, wenn die Kernthese auf ihnen ruht.

6. **LLM als Kalibrier-Lifecycle-Owner statt als Tages-Kalibrator.** Die plausibelste Produktidee, die aus dem Negativbefund folgt, verschiebt den LLM-Einsatz komplett weg vom laufenden Betrieb und in die **Daten-Schicht** — in zwei Lebensphasen, die dasselbe Muster teilen: *LLM im Daten-Loop, nicht im Entscheidungs-Loop*.

   **6a — Onboarding-Kalibrierung (einmalig).** Bei der Inbetriebnahme analysiert das LLM das Betriebsprofil eines neuen Kunden — Radius, Techniker-Zahl, Schicht-Modell, typische Auftragsdichte, SLA-Regeln, Qualifikationsmix, Saisonalität — und wählt dafür einen initialen Gewichts-Vektor (`base_penalty`, `notfall_bonus`, `travel_weight_pct`, `sla_*_bonus` etc.). Danach läuft der Solver statisch, ohne LLM im Loop.

   **6b — Daten-getriggertes Drift-Monitoring (periodisch).** In Ergänzung zu 6a läuft das LLM **nicht zeitbasiert**, sondern **datenmengen-getriggert** gegen die aggregierte Betriebshistorie. Mögliche Trigger: „seit letzter Kalibrierung 2 000 Aufträge erledigt" / „SLA-Verletzungs-Rate über gleitenden 500er-Aufträgen um 30 % gestiegen" / „Technikerzahl +20 %" / „neue Qualifikation eingeführt" / „saisonaler Profilwechsel hält länger als 4 Wochen". Bei Trigger analysiert das LLM die Kennzahlen-Historie (Completion-Drift, SLA-Trend, Auslastungs-Profil, Fahrzeit-Anteil, Auftragsmix-Shift) und prüft, ob die aktuellen Gewichte noch zum jetzigen Profil passen. Output ist eine **Empfehlung** mit Begründung („Notfall-Quote in letzten 2 000 Aufträgen +40 %, empfehle `notfall_bonus` X → Y") — der Disponent bestätigt oder verwirft, der Solver läuft danach wieder statisch.

   **Warum das mit unseren Befunden verträglich ist.** §5.5 zeigt: *innerhalb* einer Konstellation ist die Plateau-Zone breit — statisch ist gut genug. Aber die Plateau-Zone ist **konstellations-spezifisch**; strukturelle Wechsel (Technikerzahl, Kundenstruktur, neue Qualifikationen, saisonale Drift) können sie verschieben. Der vier Tests dieser Arbeit widerlegen, dass tägliche Umparametrisierung im flachen Plateau einen Gewinn bringt — sie widerlegen **nicht**, dass Konstellations-Wechsel eine Re-Kalibrierung wert sein können. MLOps-Analogie: Modelle werden nicht pro Request neu trainiert, aber pro X Datenpunkte gegen Drift validiert. Dasselbe Prinzip, nur für Penalty-Kalibrierungen statt Modellgewichte.

   **Was an dieser Idee im Vergleich zum widerlegten Hybrid neu ist:**

   | | Getesteter Hybrid (widerlegt) | Kalibrier-Lifecycle (offen) |
   |---|---|---|
   | Trigger | jeder Tag, jeder Replan | einmal beim Onboarding + bei strukturellem Daten-Drift |
   | Entscheidungs-Granularität | pro Wochen-/Tages-Plan | pro Betriebsphase |
   | Position im System | im Entscheidungs-Loop | im Daten-Loop |
   | Determinismus im Alltag | gebrochen (jeden Tag andere Gewichte) | unverändert (Solver bleibt statisch) |
   | Validierung | über Plateau-Zone hinweg — flach, kein Gewinn | über strukturelle Profil-Wechsel hinweg — ungetestet |

   **Noch zu bauen:** Der Benchmark dafür existiert nicht. Er müsste über synthetische Profil-Varianten laufen (10 Techs 30 km vs. 40 Techs 80 km Multi-Depot vs. 5 Techs SLA-lastig etc.) und prüfen, ob LLM-kalibrierte Gewichte in genug Profilen einen messbaren Vorsprung gegen den einheitlichen `normal`-Default aufbauen, um die einmaligen Onboarding-Kosten zu rechtfertigen. Für 6b zusätzlich: synthetische Zeitreihen mit strukturellem Drift, gegen die das Monitoring den Wechsel erkennen muss, ohne im stationären Betrieb falsche Umstellungen zu empfehlen. Kostenstruktur passt: einmalige LLM-Kosten (<$0.20) und periodische Drift-Checks (<$0.05 pro Monat) gegen die Betriebs-Lebensdauer des Solvers.

Weitere technische Ideen: Ersetzen von Haversine durch OSRM; OR-Tools-Parametrisierung um Solver-Strategie (nicht nur Penalty-Gewichte) LLM-gesteuert auswählen zu lassen — das wäre eine andere Art von LLM-Nutzung als in dieser Arbeit.

Die zentrale Erkenntnis der Arbeit bleibt nach der 30-Seed-Absicherung **ungebrochen**: _In einer Domäne mit etabliertem algorithmischem Fundament ist LLM-Augmentation begründungspflichtig._ Der Hybrid gewinnt nicht durch Zauberei, und die vermutete „letzte Bastion" (Skill-Heterogenität) war sie nicht. Was bleibt, ist der **strukturelle** Mittelstand-Wert aus §6 — Ersatz einer Kalibrierungs-Entscheidung, die der Nutzer nicht treffen will —, nicht ein Performance-Vorsprung. Ob das genügt, den operativen Mehr-Aufwand zu rechtfertigen, ist eine Geschäfts-, keine Performance-Frage.

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
