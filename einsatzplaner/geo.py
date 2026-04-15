from __future__ import annotations

import math
from typing import Protocol


DEFAULT_SPEED_KMH = 50.0
DEFAULT_DETOUR_FACTOR = 1.3


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class RouteProvider(Protocol):
    def travel_time_min(self, lat1: float, lon1: float, lat2: float, lon2: float) -> int: ...


class HaversineRouteProvider:
    def __init__(self, speed_kmh: float = DEFAULT_SPEED_KMH, detour: float = DEFAULT_DETOUR_FACTOR):
        self.speed_kmh = speed_kmh
        self.detour = detour

    def travel_time_min(self, lat1: float, lon1: float, lat2: float, lon2: float) -> int:
        distance_km = haversine_km(lat1, lon1, lat2, lon2) * self.detour
        hours = distance_km / self.speed_kmh
        return max(1, int(round(hours * 60))) if distance_km > 0 else 0
