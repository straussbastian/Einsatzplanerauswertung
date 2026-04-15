from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta
from pathlib import Path

import yaml

from .generator import Szenarioprofil
from .models import Auftrag, EventTyp, Stoerung, Techniker


def _parse_event(raw: dict, woche_start: date) -> Stoerung:
    tag_offset = raw.get("tag", 0)
    zeit_str = raw.get("zeit", "09:00")
    h, m = [int(x) for x in zeit_str.split(":")]
    zeitpunkt = datetime.combine(woche_start + timedelta(days=tag_offset), time(h, m))

    typ = EventTyp(raw["typ"])
    return Stoerung(
        typ=typ,
        zeitpunkt=zeitpunkt,
        techniker_id=raw.get("techniker"),
        auftrag_id=raw.get("auftrag"),
        stau_dauer_min=int(raw.get("stau_min", 0)),
        extra_min=int(raw.get("extra_min", 0)),
        betroffene_techniker=list(raw.get("betroffene_techniker", [])),
        dauer_tage=int(raw.get("dauer_tage", 1)),
    )


def load_scenario(pfad: str | Path, woche_start: date) -> list[Stoerung]:
    pfad = Path(pfad)
    if not pfad.exists():
        return []
    daten = yaml.safe_load(pfad.read_text()) or {}
    events = daten.get("events", []) or []
    return [_parse_event(e, woche_start) for e in events]


def list_scenarios(scenarios_dir: str | Path = "scenarios") -> list[Path]:
    d = Path(scenarios_dir)
    if not d.exists():
        return []
    return sorted(d.glob("*.yaml"))


def _random_time(rng: random.Random, hour_min: int, hour_max: int) -> time:
    h = rng.randint(hour_min, hour_max)
    m = rng.choice([0, 15, 30, 45])
    return time(h, m)


def generate_intraday_events(
    tag: date,
    auftraege_heute: list[Auftrag],
    techniker: list[Techniker],
    profil: Szenarioprofil,
    rng: random.Random,
) -> list[Stoerung]:
    """Erzeugt zufällige Störungen für einen Tag basierend auf den Intraday-Raten im Profil.

    Seed-gesteuert: gleicher rng-Zustand ⇒ identische Events. Das ermöglicht
    Multi-Seed-Benchmarks mit echtem stochastischen Stör-Profil statt festem YAML.
    """
    events: list[Stoerung] = []

    for tech in techniker:
        if rng.random() < profil.intraday_krank_rate:
            events.append(
                Stoerung(
                    typ=EventTyp.TECHNIKER_KRANK,
                    zeitpunkt=datetime.combine(tag, _random_time(rng, 9, 14)),
                    techniker_id=tech.id,
                )
            )

    for a in auftraege_heute:
        if rng.random() < profil.intraday_verlaengerung_rate:
            events.append(
                Stoerung(
                    typ=EventTyp.AUFTRAG_VERLAENGERT,
                    zeitpunkt=datetime.combine(tag, _random_time(rng, 9, 15)),
                    auftrag_id=a.id,
                    extra_min=rng.choice([30, 45, 60, 90]),
                )
            )

    for a in auftraege_heute:
        if rng.random() < profil.intraday_absage_rate:
            events.append(
                Stoerung(
                    typ=EventTyp.KUNDE_ABSAGE,
                    zeitpunkt=datetime.combine(tag, _random_time(rng, 8, 14)),
                    auftrag_id=a.id,
                )
            )

    if rng.random() < profil.intraday_stau_rate:
        n = rng.randint(1, min(4, len(techniker)))
        events.append(
            Stoerung(
                typ=EventTyp.STAU,
                zeitpunkt=datetime.combine(tag, _random_time(rng, 8, 14)),
                stau_dauer_min=rng.choice([15, 20, 30, 45]),
                betroffene_techniker=rng.sample([t.id for t in techniker], k=n),
            )
        )

    return events
