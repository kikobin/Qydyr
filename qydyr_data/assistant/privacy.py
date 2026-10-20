"""Location privacy: exact coordinates are used only in memory for this one answer.
Anything that is logged or shown back is rounded to a geohash cell (~150 m at precision 7)."""
from __future__ import annotations

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"


def geohash(lat: float, lon: float, precision: int = 7) -> str:
    lat_rng, lon_rng = [-90.0, 90.0], [-180.0, 180.0]
    bits, bit, ch, even, out = [16, 8, 4, 2, 1], 0, 0, True, []
    while len(out) < precision:
        rng, val = (lon_rng, lon) if even else (lat_rng, lat)
        mid = (rng[0] + rng[1]) / 2
        if val >= mid:
            ch |= bits[bit]
            rng[0] = mid
        else:
            rng[1] = mid
        even = not even
        bit += 1
        if bit == 5:
            out.append(_BASE32[ch])
            bit, ch = 0, 0
    return "".join(out)


def cell_center(gh: str) -> tuple[float, float]:
    lat_rng, lon_rng, even = [-90.0, 90.0], [-180.0, 180.0], True
    for c in gh:
        cd = _BASE32.index(c)
        for mask in (16, 8, 4, 2, 1):
            rng = lon_rng if even else lat_rng
            mid = (rng[0] + rng[1]) / 2
            if cd & mask:
                rng[0] = mid
            else:
                rng[1] = mid
            even = not even
    return (lat_rng[0] + lat_rng[1]) / 2, (lon_rng[0] + lon_rng[1]) / 2


def coarse(lat: float, lon: float, precision: int = 7) -> tuple[float, float, str]:
    gh = geohash(lat, lon, precision)
    clat, clon = cell_center(gh)
    return round(clat, 5), round(clon, 5), gh
