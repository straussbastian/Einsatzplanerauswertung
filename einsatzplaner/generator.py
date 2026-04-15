from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .models import (
    BETRIEBSHOF_LAT,
    BETRIEBSHOF_LON,
    Auftrag,
    Dringlichkeit,
    Techniker,
)


@dataclass
class Szenarioprofil:
    """Einstellbare Tages-/Wochenintensität für Stresstests."""
    notfall_rate: float = 0.02  # Anteil aller Aufträge mit notfall=True
    sla_druck_rate: float = 0.10  # Anteil mit SLA-Frist heute oder überfällig
    ueberlast_pct: int = 100  # 100 = Standard-Menge, 200 = doppelt
    rollover_vorbelastung: int = 0  # Zusätzliche Aufträge am Montag mit rollover_count=1..3
    dringlichkeit_gewichte: tuple[float, float, float] = (0.5, 0.35, 0.15)  # (niedrig, mittel, hoch)
    # Intraday-Störungen (werden pro Tag zufällig injiziert, während der Tag läuft)
    intraday_krank_rate: float = 0.0  # P(mind. ein Techniker fällt spontan aus), pro Tag
    intraday_verlaengerung_rate: float = 0.0  # P(Auftrag verlängert sich ungeplant), pro Auftrag
    intraday_absage_rate: float = 0.0  # P(Kunde sagt spontan ab), pro Auftrag
    intraday_stau_rate: float = 0.0  # P(Stau irgendwo heute), pro Tag

    @classmethod
    def presets(cls) -> dict[str, "Szenarioprofil"]:
        return {
            "Normal": cls(),
            "Hochlast": cls(ueberlast_pct=150, sla_druck_rate=0.15),
            "Notfallwoche": cls(notfall_rate=0.20, dringlichkeit_gewichte=(0.3, 0.35, 0.35)),
            "SLA-Katastrophe": cls(sla_druck_rate=0.40, rollover_vorbelastung=15),
            "Chaos": cls(
                notfall_rate=0.12,
                sla_druck_rate=0.25,
                ueberlast_pct=180,
                rollover_vorbelastung=20,
                intraday_krank_rate=0.15,
                intraday_verlaengerung_rate=0.04,
                intraday_absage_rate=0.02,
                intraday_stau_rate=0.30,
            ),
            "Realistisch": cls(
                ueberlast_pct=120,
                intraday_krank_rate=0.05,
                intraday_verlaengerung_rate=0.02,
                intraday_absage_rate=0.015,
                intraday_stau_rate=0.20,
            ),
        }


OLDENBURG_RADIUS_KM = 30.0

VORNAMEN = [
    "Anna", "Bernd", "Claudia", "Dieter", "Erika", "Friedrich", "Gisela", "Helmut",
    "Inge", "Jürgen", "Karin", "Lars", "Monika", "Norbert", "Olga", "Peter",
    "Petra", "Rainer", "Sabine", "Thomas", "Ulrike", "Volker", "Wiebke", "Xenia",
]
NACHNAMEN = [
    "Meyer", "Schmidt", "Müller", "Fischer", "Weber", "Schneider", "Wagner",
    "Becker", "Hoffmann", "Schulz", "Koch", "Bauer", "Richter", "Klein", "Wolf",
    "Neumann", "Schwarz", "Zimmermann", "Braun", "Krüger",
]
FIRMEN = [
    "Bäckerei Brinkmann GmbH", "Hotel Ammerland", "Autohaus Janßen",
    "Metzgerei Tönjes", "Restaurant Waldeslust", "Praxis Dr. Hoffmann",
    "Kindergarten Sonnenschein", "Getränke Cordes", "Tischlerei Lüken",
    "Steuerberatung Bünting", "Apotheke am Markt", "Fitnessstudio Power-Gym",
    "Friseursalon Kamp", "Werkstatt Ahlers", "Senioren­heim Parkblick",
]
STRASSEN = [
    "Hauptstraße", "Gartenweg", "Bahnhofstraße", "Dorfstraße", "Kirchweg",
    "Am Markt", "Bremer Straße", "Cloppenburger Straße", "Lindenallee",
    "Eichenweg", "Mühlenweg", "Schulstraße", "Moorweg", "Feldstraße",
]
ORTE = [
    "Oldenburg", "Rastede", "Wardenburg", "Bad Zwischenahn", "Wiefelstede",
    "Hude", "Ganderkesee", "Hatten", "Elsfleth", "Westerstede",
]


def _random_point_in_radius(center_lat: float, center_lon: float, max_km: float, rng: random.Random) -> tuple[float, float]:
    r_km = max_km * math.sqrt(rng.random())
    theta = rng.uniform(0, 2 * math.pi)
    dlat = (r_km / 111.0) * math.cos(theta)
    dlon = (r_km / (111.0 * math.cos(math.radians(center_lat)))) * math.sin(theta)
    return center_lat + dlat, center_lon + dlon


def generate_techniker(n: int = 10, rng: random.Random | None = None) -> list[Techniker]:
    rng = rng or random.Random(42)
    techniker = []
    for i in range(n):
        vorname = rng.choice(VORNAMEN)
        nachname = rng.choice(NACHNAMEN)
        techniker.append(Techniker(id=f"T{i+1:02d}", name=f"{vorname} {nachname}"))
    return techniker


def _sla_frist_fuer(dringlichkeit: Dringlichkeit, ab: date, rng: random.Random) -> date:
    if dringlichkeit == Dringlichkeit.HOCH:
        plus = rng.randint(0, 1)
    elif dringlichkeit == Dringlichkeit.MITTEL:
        plus = rng.randint(2, 5)
    else:
        plus = rng.randint(5, 14)
    return ab + timedelta(days=plus)


def _gewerbl_fenster(rng: random.Random) -> tuple[time, time]:
    opt = [(time(8, 0), time(17, 0)), (time(9, 0), time(18, 0)), (time(10, 0), time(16, 0))]
    return rng.choice(opt)


def _fixes_fenster(rng: random.Random) -> tuple[time, time]:
    start_hour = rng.choice([9, 10, 11, 13, 14, 15])
    return time(start_hour, 0), time(start_hour + 2, 0)


def generate_auftraege(
    n: int,
    tag: date,
    seq_start: int = 1,
    rng: random.Random | None = None,
    profil: Szenarioprofil | None = None,
) -> list[Auftrag]:
    rng = rng or random.Random()
    profil = profil or Szenarioprofil()
    auftraege: list[Auftrag] = []
    for i in range(n):
        typ = rng.choices(["privat", "gewerblich"], weights=[0.65, 0.35])[0]
        dringlichkeit = rng.choices(
            [Dringlichkeit.NIEDRIG, Dringlichkeit.MITTEL, Dringlichkeit.HOCH],
            weights=list(profil.dringlichkeit_gewichte),
        )[0]
        notfall = rng.random() < profil.notfall_rate
        if notfall:
            dringlichkeit = Dringlichkeit.HOCH
        terminart = rng.choices(["flexibel", "fix"], weights=[0.7, 0.3])[0]
        dauer = rng.choice([30, 45, 60, 90, 120, 180])
        lat, lon = _random_point_in_radius(BETRIEBSHOF_LAT, BETRIEBSHOF_LON, OLDENBURG_RADIUS_KM, rng)

        fenster_von: time | None = None
        fenster_bis: time | None = None
        if terminart == "fix":
            fenster_von, fenster_bis = _fixes_fenster(rng)
        elif typ == "gewerblich":
            fenster_von, fenster_bis = _gewerbl_fenster(rng)

        if typ == "privat":
            kunde = f"{rng.choice(VORNAMEN)} {rng.choice(NACHNAMEN)}"
        else:
            kunde = rng.choice(FIRMEN)

        adresse = f"{rng.choice(STRASSEN)} {rng.randint(1, 120)}, {rng.choice(ORTE)}"

        if rng.random() < profil.sla_druck_rate:
            offset = rng.choice([-1, 0, 0, 0])
            sla_frist = tag + timedelta(days=offset)
        else:
            sla_frist = _sla_frist_fuer(dringlichkeit, tag, rng)

        auftraege.append(
            Auftrag(
                id=f"A{seq_start + i:04d}",
                kunde=kunde,
                typ=typ,
                adresse=adresse,
                lat=lat,
                lon=lon,
                dauer_min=dauer,
                dringlichkeit=dringlichkeit,
                terminart=terminart,
                fenster_von=fenster_von,
                fenster_bis=fenster_bis,
                sla_frist=sla_frist,
                notfall=notfall,
            )
        )
    return auftraege


def verteile_auftraege_auf_woche(n_gesamt: int, rng: random.Random) -> list[int]:
    """Verteilt n_gesamt Aufträge auf 5 Werktage mit leichter Variation (±10 %).

    Beispiel: 218 Gesamt → ca. [44, 46, 42, 45, 41] — kein Tag genau gleich,
    Summe = n_gesamt.
    """
    basis = n_gesamt / 5
    roh = [basis * (1 + rng.uniform(-0.1, 0.1)) for _ in range(5)]
    gerundet = [max(1, round(x)) for x in roh]
    diff = n_gesamt - sum(gerundet)
    idx = 0
    while diff != 0:
        step = 1 if diff > 0 else -1
        if gerundet[idx % 5] + step >= 1:
            gerundet[idx % 5] += step
            diff -= step
        idx += 1
    return gerundet


def generate_woche(
    start_montag: date,
    auftraege_pro_tag: list[int] | None = None,
    auftraege_pro_woche: int | None = None,
    rng: random.Random | None = None,
    profil: Szenarioprofil | None = None,
) -> dict[date, list[Auftrag]]:
    """Erzeugt eine Arbeitswoche.

    Priorität der Mengenangabe:
      1. auftraege_pro_tag (explizite Liste, Länge = Anzahl Tage)
      2. auftraege_pro_woche (Gesamtzahl → wird über die 5 Tage verteilt)
      3. Default [45, 42, 48, 40, 43] (218 Aufträge total)

    Die Profil-Überlast (`ueberlast_pct`) wird danach multiplikativ angewandt.
    """
    rng = rng or random.Random(7)
    profil = profil or Szenarioprofil()

    # Mengen-Bestimmung mit drei Pfaden:
    # 1. auftraege_pro_tag explizit → exakt diese Werte (kein Skalieren mit Überlast)
    # 2. auftraege_pro_woche explizit → Gesamtzahl inkl. Rollover-Altlast,
    #    Altlast wird abgezogen und neue Aufträge daraus verteilt;
    #    Profil-Überlast wirkt NICHT mehr (User hat ja eine konkrete Zahl genannt)
    # 3. Default → [45, 42, 48, 40, 43] mit Profil-Überlast multipliziert
    if auftraege_pro_tag is not None:
        skaliert = list(auftraege_pro_tag)
    elif auftraege_pro_woche is not None:
        netto_neu = max(5, auftraege_pro_woche - profil.rollover_vorbelastung)
        skaliert = verteile_auftraege_auf_woche(netto_neu, rng)
    else:
        default_pro_tag = [45, 42, 48, 40, 43]
        skaliert = [max(1, round(n * profil.ueberlast_pct / 100)) for n in default_pro_tag]

    woche: dict[date, list[Auftrag]] = {}
    seq = 1
    for idx, n in enumerate(skaliert):
        tag = start_montag + timedelta(days=idx)
        woche[tag] = generate_auftraege(n, tag, seq_start=seq, rng=rng, profil=profil)
        seq += n

    if profil.rollover_vorbelastung > 0 and woche:
        erster_tag = min(woche.keys())
        zusatz = generate_auftraege(
            profil.rollover_vorbelastung,
            erster_tag,
            seq_start=seq,
            rng=rng,
            profil=profil,
        )
        for a in zusatz:
            a.rollover_count = rng.choice([1, 1, 2, 3])
            a.sla_frist = erster_tag + timedelta(days=rng.choice([-1, 0, 0, 1]))
        woche[erster_tag] = zusatz + woche[erster_tag]

    return woche


def generate_multiprofil_woche(
    start_montag: date,
    profile_pro_tag: list[Szenarioprofil],
    basis_auftraege_pro_tag: int = 45,
    auftraege_pro_woche: int | None = None,
    rng: random.Random | None = None,
) -> tuple[dict[date, list[Auftrag]], dict[date, Szenarioprofil]]:
    """Erzeugt eine Woche, in der jeder Tag ein eigenes Szenarioprofil hat.

    Kernbenchmark für den Hybrid-Mehrwert: ein statisch kalibrierter Solver wird
    auf einem Tag (z.B. Normal) optimal sein, aber an den anderen vier Tagen
    suboptimal — genau das Profil eines Handwerksbetriebs, der einmal einstellt
    und nie neu kalibriert.
    """
    rng = rng or random.Random(7)
    if len(profile_pro_tag) == 0:
        raise ValueError("profile_pro_tag darf nicht leer sein")

    if auftraege_pro_woche is not None:
        basis_verteilt = verteile_auftraege_auf_woche(auftraege_pro_woche, rng)
    else:
        basis_verteilt = [basis_auftraege_pro_tag] * len(profile_pro_tag)

    woche: dict[date, list[Auftrag]] = {}
    profil_pro_tag: dict[date, Szenarioprofil] = {}
    seq = 1
    for idx, profil in enumerate(profile_pro_tag):
        tag = start_montag + timedelta(days=idx)
        basis = basis_verteilt[idx] if idx < len(basis_verteilt) else basis_auftraege_pro_tag
        n = max(1, round(basis * profil.ueberlast_pct / 100))
        woche[tag] = generate_auftraege(n, tag, seq_start=seq, rng=rng, profil=profil)
        profil_pro_tag[tag] = profil
        seq += n

    if profile_pro_tag[0].rollover_vorbelastung > 0 and woche:
        erster_tag = min(woche.keys())
        zusatz = generate_auftraege(
            profile_pro_tag[0].rollover_vorbelastung,
            erster_tag,
            seq_start=seq,
            rng=rng,
            profil=profile_pro_tag[0],
        )
        for a in zusatz:
            a.rollover_count = rng.choice([1, 1, 2, 3])
            a.sla_frist = erster_tag + timedelta(days=rng.choice([-1, 0, 0, 1]))
        woche[erster_tag] = zusatz + woche[erster_tag]

    return woche, profil_pro_tag


def naechster_montag(von: date | None = None) -> date:
    d = von or datetime.now().date()
    offset = (7 - d.weekday()) % 7
    if offset == 0:
        offset = 7
    return d + timedelta(days=offset)
