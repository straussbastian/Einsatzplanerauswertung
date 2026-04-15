from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .models import StopTyp
from .simulator import WochenErgebnis


@dataclass
class Metriken:
    scheduler_name: str
    szenario: str
    generiert: int
    erledigt: int
    storniert: int
    offen: int
    completion_rate: float
    completion_prio_gewichtet: float
    gesamtfahrzeit_min: int
    arbeitszeit_min: int
    auslastung_pct: float
    notfaelle_erledigt: int
    notfaelle_gesamt: int
    rollover_max: int
    sla_verletzungen: int

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def compute_metriken(we: WochenErgebnis, auftraege_bekannt: dict[str, "Auftrag"]) -> Metriken:  # type: ignore[name-defined]
    erledigt_ids: set[str] = set()
    storniert_ids: set[str] = set()
    gesamtfahrt = 0
    gesamtarbeit = 0
    for tag in we.ergebnisse:
        erledigt_ids.update(tag.erledigt)
        storniert_ids.update(tag.storniert)
        for tour in tag.tourenplan.touren.values():
            gesamtfahrt += tour.gesamt_fahrzeit_min
            for stop in tour.stops:
                if stop.typ == StopTyp.AUFTRAG and stop.status == "erledigt":
                    gesamtarbeit += stop.dauer_min

    generiert = len(auftraege_bekannt)
    offen = len(we.offen_am_ende)
    n_techs = max(
        (len(tag.tourenplan.touren) for tag in we.ergebnisse), default=1
    )
    n_tage = len(we.ergebnisse) or 1

    # Prio-gewichtete Completion: Summe(prio) erledigt / Summe(prio) gesamt
    prio_total = sum(int(a.dringlichkeit) for a in auftraege_bekannt.values())
    prio_erledigt = sum(
        int(auftraege_bekannt[aid].dringlichkeit)
        for aid in erledigt_ids
        if aid in auftraege_bekannt
    )
    prio_completion = prio_erledigt / prio_total if prio_total else 0.0

    notfaelle_gesamt = sum(1 for a in auftraege_bekannt.values() if a.notfall)
    notfaelle_erledigt = sum(
        1 for aid in erledigt_ids if aid in auftraege_bekannt and auftraege_bekannt[aid].notfall
    )
    rollover_max = max((a.rollover_count for a in auftraege_bekannt.values()), default=0)

    sla_verletzungen = 0
    for a in we.offen_am_ende:
        if a.sla_frist and we.ergebnisse and a.sla_frist <= we.ergebnisse[-1].datum:
            sla_verletzungen += 1

    kapazitaet_min = n_techs * n_tage * 480  # 8h Nettoarbeitszeit/Tag

    return Metriken(
        scheduler_name=we.scheduler_name,
        szenario=we.szenario,
        generiert=generiert,
        erledigt=len(erledigt_ids),
        storniert=len(storniert_ids),
        offen=offen,
        completion_rate=round(100 * len(erledigt_ids) / generiert, 1) if generiert else 0.0,
        completion_prio_gewichtet=round(100 * prio_completion, 1),
        gesamtfahrzeit_min=gesamtfahrt,
        arbeitszeit_min=gesamtarbeit,
        auslastung_pct=round(100 * gesamtarbeit / kapazitaet_min, 1) if kapazitaet_min else 0.0,
        notfaelle_erledigt=notfaelle_erledigt,
        notfaelle_gesamt=notfaelle_gesamt,
        rollover_max=rollover_max,
        sla_verletzungen=sla_verletzungen,
    )


def vergleichs_df(ergebnisse: list[Metriken]) -> pd.DataFrame:
    return pd.DataFrame([m.as_dict() for m in ergebnisse])


def tages_df(we: WochenErgebnis) -> pd.DataFrame:
    rows = []
    for tag in we.ergebnisse:
        rows.append(
            dict(
                datum=tag.datum,
                erledigt=len(tag.erledigt),
                storniert=len(tag.storniert),
                rollover=len(tag.rollover),
                events=len(tag.angewandte_events),
            )
        )
    return pd.DataFrame(rows)
