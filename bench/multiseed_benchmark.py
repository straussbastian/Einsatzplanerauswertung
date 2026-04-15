"""Multi-Seed-Benchmark: Multi-Profil-Woche + stochastische Intraday-Events.

Vergleicht:
  - Heuristik (Insertion-Greedy)
  - OR-Tools mit drei verschiedenen statischen Kalibrierungen:
      * normal       (Standard-Kalibrierung für Normal-Lastprofil)
      * chaos-safe   (vorsichtige Worst-Case-Einstellung)
      * sla-boost    (SLA-fokussiert)
  - Hybrid (LLM wählt Gewichte pro Tag)

Die Woche rotiert durch alle fünf Intensitäts-Presets:
  Mo = Normal, Di = Hochlast, Mi = Notfallwoche, Do = SLA-Katastrophe, Fr = Chaos

So testet der Benchmark genau das, was ein Mittelstands-Disponent nie explizit
konfigurieren würde: dass sich das Lastprofil über die Woche stark ändert. Der
statische Solver kann nicht alle fünf Profile gleichzeitig abdecken; das zeigt
den strukturellen Mehrwert adaptiver Priorisierung.

Jede Scheduler-Variante läuft auf N Seeds auf demselben seed-gesteuerten
Datenstand, inklusive stochastisch gewürfelter Intraday-Events (Krankmeldung,
Verlängerung, Absage, Stau) aus den Profil-Raten.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")

import random

from einsatzplaner.evaluator import compute_metriken
from einsatzplaner.generator import (
    Szenarioprofil,
    generate_multiprofil_woche,
    generate_techniker,
)
from einsatzplaner.geo import HaversineRouteProvider
from einsatzplaner.scheduler.heuristic import HeuristicScheduler
from einsatzplaner.scheduler.hybrid import LLMGuidedVRPScheduler
from einsatzplaner.scheduler.ortools_vrp import (
    CHAOS_SAFE_WEIGHTS,
    DEFAULT_WEIGHTS,
    NAIVE_WEIGHTS,
    ORToolsScheduler,
    SLA_BOOST_WEIGHTS,
)
from einsatzplaner.simulator import run_woche


def profile_der_woche(with_quals: bool = False) -> list[Szenarioprofil]:
    presets = Szenarioprofil.presets()
    tage = [
        presets["Normal"],
        presets["Hochlast"],
        presets["Notfallwoche"],
        presets["SLA-Katastrophe"],
        presets["Chaos"],
    ]
    if with_quals:
        from dataclasses import replace
        tage = [
            replace(p, kaelteschein_rate_techniker=0.4, kaelteschein_rate_auftraege=0.2)
            for p in tage
        ]
    return tage


def run_one(
    seed: int,
    scheduler_label: str,
    scheduler_factory,
    include_intraday: bool = True,
    with_quals: bool = False,
) -> dict:
    montag = date(2026, 4, 20)
    rp = HaversineRouteProvider()
    profile = profile_der_woche(with_quals=with_quals)

    rng = random.Random(seed)
    techs = generate_techniker(10, rng, profil=profile[0])
    woche, profil_pro_tag = generate_multiprofil_woche(montag, profile, rng=rng)
    bekannte = {a.id: a for tag in woche.values() for a in tag}

    sched = scheduler_factory()
    t0 = time.time()
    we = run_woche(
        woche,
        techs,
        sched,
        rp,
        stoerungen=[],
        szenario="multi-profil",
        profil_pro_tag=profil_pro_tag if include_intraday else None,
        intraday_seed=seed + 10_000 if include_intraday else None,
    )
    dauer = time.time() - t0
    m = compute_metriken(we, bekannte)

    return {
        "seed": seed,
        "scheduler": scheduler_label,
        "erledigt": m.erledigt,
        "generiert": m.generiert,
        "completion_pct": m.completion_rate,
        "prio_completion_pct": m.completion_prio_gewichtet,
        "sla_vlz": m.sla_verletzungen,
        "fahrzeit_min": m.gesamtfahrzeit_min,
        "auslastung_pct": m.auslastung_pct,
        "notfall_erledigt": m.notfaelle_erledigt,
        "notfall_gesamt": m.notfaelle_gesamt,
        "rollover_max": m.rollover_max,
        "laufzeit_sec": round(dauer, 1),
    }


def aggregate(rows: list[dict]) -> list[dict]:
    by_sched: dict[str, list[dict]] = {}
    for r in rows:
        by_sched.setdefault(r["scheduler"], []).append(r)

    out = []
    for sched, runs in by_sched.items():
        def stat(key: str) -> tuple[float, float]:
            vals = [r[key] for r in runs]
            m = statistics.mean(vals)
            s = statistics.stdev(vals) if len(vals) > 1 else 0.0
            return round(m, 2), round(s, 2)

        n = len(runs)
        out.append(
            {
                "scheduler": sched,
                "n_seeds": n,
                "completion_mean": stat("completion_pct")[0],
                "completion_std": stat("completion_pct")[1],
                "prio_completion_mean": stat("prio_completion_pct")[0],
                "prio_completion_std": stat("prio_completion_pct")[1],
                "sla_vlz_mean": stat("sla_vlz")[0],
                "sla_vlz_std": stat("sla_vlz")[1],
                "fahrzeit_mean": stat("fahrzeit_min")[0],
                "fahrzeit_std": stat("fahrzeit_min")[1],
                "laufzeit_mean": stat("laufzeit_sec")[0],
            }
        )
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=20)
    parser.add_argument("--skip-hybrid", action="store_true", help="LLM-Hybrid weglassen (spart API-Kosten)")
    parser.add_argument("--skip-intraday", action="store_true", help="stochastische Intraday-Events deaktivieren")
    parser.add_argument("--core-only", action="store_true", help="nur Heuristik + normal + naiv + Hybrid (kein chaos-safe/sla-boost)")
    parser.add_argument("--qualifications", action="store_true", help="Kälteschein-Constraint aktivieren (40 %% Techniker, 20 %% Aufträge)")
    parser.add_argument("--llm-model", default="claude-sonnet-4-6")
    parser.add_argument("--solver-time-limit", type=int, default=4)
    parser.add_argument("--out", default="bench/results_multiseed.csv")
    args = parser.parse_args()

    schedulers: list[tuple[str, callable]] = [
        ("heuristik", lambda: HeuristicScheduler()),
        ("ortools-naiv", lambda: ORToolsScheduler(time_limit_sec=args.solver_time_limit, weights=NAIVE_WEIGHTS)),
        ("ortools-normal", lambda: ORToolsScheduler(time_limit_sec=args.solver_time_limit, weights=DEFAULT_WEIGHTS)),
    ]
    if not args.core_only:
        schedulers.extend([
            ("ortools-chaos-safe", lambda: ORToolsScheduler(time_limit_sec=args.solver_time_limit, weights=CHAOS_SAFE_WEIGHTS)),
            ("ortools-sla-boost", lambda: ORToolsScheduler(time_limit_sec=args.solver_time_limit, weights=SLA_BOOST_WEIGHTS)),
        ])
    if not args.skip_hybrid:
        schedulers.append(
            ("hybrid", lambda: LLMGuidedVRPScheduler(model=args.llm_model, time_limit_sec=args.solver_time_limit))
        )

    print(f"Running {args.seeds} seeds × {len(schedulers)} schedulers = {args.seeds * len(schedulers)} total runs")
    print(f"Intraday-Events: {'aus' if args.skip_intraday else 'an'}\n")

    all_rows: list[dict] = []
    for seed in range(args.seeds):
        for label, factory in schedulers:
            row = run_one(
                seed, label, factory,
                include_intraday=not args.skip_intraday,
                with_quals=args.qualifications,
            )
            all_rows.append(row)
            print(
                f"seed={seed:02d} {label:<20} "
                f"comp={row['completion_pct']:>5}% "
                f"sla={row['sla_vlz']:>3} "
                f"fahrt={row['fahrzeit_min']:>5} "
                f"t={row['laufzeit_sec']:>5}s",
                flush=True,
            )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\nRaw results → {out_path}")

    print("\n=== AGGREGATED (mean ± stdev) ===")
    agg = aggregate(all_rows)
    for row in agg:
        print(
            f"{row['scheduler']:<20} "
            f"comp {row['completion_mean']}% ±{row['completion_std']} | "
            f"prio {row['prio_completion_mean']}% ±{row['prio_completion_std']} | "
            f"sla {row['sla_vlz_mean']} ±{row['sla_vlz_std']} | "
            f"fahrt {row['fahrzeit_mean']} ±{row['fahrzeit_std']}"
        )

    agg_path = out_path.with_name(out_path.stem + "_aggregated.csv")
    with agg_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(agg[0].keys()))
        writer.writeheader()
        writer.writerows(agg)
    print(f"\nAggregated → {agg_path}")


if __name__ == "__main__":
    main()
