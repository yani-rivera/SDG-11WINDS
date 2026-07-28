"""
Honduras observed-wind map
==========================

Pulls every reporting station in the IEM `HN__ASOS` network and renders an
animated map of *observed* winds: one arrow per station per hour, pointing the
way the wind is blowing, length and colour scaled by speed.

No interpolation. Nothing between the stations is invented.

Usage
-----
    python hn_wind_map.py --start 2024-10-01 --end 2024-10-08
    python hn_wind_map.py --start 2024-10-01 --end 2024-10-08 --roses
    python hn_wind_map.py --demo          # synthetic data, no network needed

Outputs
-------
    hn_wind_map.html     animated arrow map (open in any browser)
    hn_wind_roses.html   per-station wind roses, if --roses

Requires: pandas, numpy, requests, folium, branca  (plotly only for --roses)
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

import wind_paths as wp

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
KT_TO_MS = 0.514444

# Longest arrow (for the fastest wind in the window), in degrees of latitude.
# 0.30 deg is about 33 km -- readable at country scale without stations
# overlapping each other. Turn it down if your arrows collide.
MAX_ARROW_DEG = 0.30


# --------------------------------------------------------------------------
# 1. Fetch
# --------------------------------------------------------------------------
def fetch_iem(start: datetime, end: datetime, network: str = "HN__ASOS") -> pd.DataFrame:
    """Download wind observations for a whole IEM network as one CSV."""
    import requests

    params = {
        "data": ["drct", "sknt", "gust"],
        "network": network,
        "sts": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ets": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tz": "UTC",
        "format": "comma",
        "latlon": "yes",       # without this you get no coordinates
        "missing": "M",
        "trace": "T",
        "report_type": [3, 4],  # routine + special METARs
    }
    print(f"  requesting {network} {start:%Y-%m-%d} -> {end:%Y-%m-%d} ...", flush=True)
    resp = requests.get(IEM_URL, params=params, timeout=300)
    resp.raise_for_status()
    if len(resp.text) < 200:
        raise RuntimeError(f"IEM returned almost nothing:\n{resp.text[:500]}")
    return pd.read_csv(io.StringIO(resp.text), comment="#", low_memory=False)


def demo_frame() -> pd.DataFrame:
    """Synthetic stand-in so the pipeline can be exercised without network."""
    sites = {
        "MHTG": (14.061, -87.217), "MHLM": (15.453, -87.924),
        "MHLC": (15.742, -86.853), "MHRO": (16.317, -86.523),
        "MHNJ": (16.445, -85.906), "MHPL": (15.262, -83.781),
        "MHTE": (15.927, -85.938), "MHSC": (14.382, -87.621),
    }
    rng = np.random.default_rng(7)
    times = pd.date_range("2024-10-01", periods=72, freq="h", tz="UTC")
    rows = []
    for stn, (lat, lon) in sites.items():
        # Coastal sites get NE trades; inland sites get valley channelling.
        base = 60 if lat > 15.2 else 110
        for t in times:
            if rng.random() < 0.12:      # simulate the reporting gaps
                continue
            rows.append({
                "station": stn, "valid": t, "lat": lat, "lon": lon,
                "drct": (base + rng.normal(0, 25)) % 360,
                "sknt": max(0.0, rng.normal(9, 4)),
                "gust": np.nan,
            })
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# 2. Clean
# --------------------------------------------------------------------------
def clean(df: pd.DataFrame, freq: str = "h") -> pd.DataFrame:
    """Coerce types, drop the traps, collapse to one observation per hour."""
    df = df.copy()
    df["valid"] = pd.to_datetime(df["valid"], utc=True, errors="coerce")
    for col in ("lat", "lon", "drct", "sknt", "gust"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")  # 'M' -> NaN

    n0 = len(df)
    df = df.dropna(subset=["valid", "lat", "lon", "drct", "sknt"])

    # Trap 1: calm. drct=0/sknt=0 means calm, NOT "wind from the north".
    calm = df["sknt"] <= 0
    # Trap 2: variable direction reports also land as 0 with a nonzero speed.
    vrb = (df["drct"] == 0) & (df["sknt"] > 0)
    df = df[~(calm | vrb)]
    print(f"  {n0} rows -> {len(df)} usable "
          f"({calm.sum()} calm, {vrb.sum()} variable-direction dropped)")

    # One observation per station per interval: nearest the interval boundary.
    df["hour"] = df["valid"].dt.round(freq)
    df["off"] = (df["valid"] - df["hour"]).abs()
    df = (df.sort_values("off")
            .groupby(["station", "hour"], as_index=False)
            .first()
            .drop(columns=["off"]))

    spd = df["sknt"] * KT_TO_MS
    df["speed_ms"] = spd
    # Meteorological convention: drct is the direction the wind comes FROM.
    df["u"] = -spd * np.sin(np.deg2rad(df["drct"]))
    df["v"] = -spd * np.cos(np.deg2rad(df["drct"]))
    return df.sort_values(["hour", "station"]).reset_index(drop=True)


# --------------------------------------------------------------------------
# 3. Arrow geometry
# --------------------------------------------------------------------------
def arrow(lat: float, lon: float, drct: float, length_deg: float):
    """A 5-point polyline shaped like an arrow, in GeoJSON [lon, lat] order."""
    toward = np.deg2rad((drct + 180.0) % 360.0)   # blowing-toward bearing
    coslat = max(np.cos(np.deg2rad(lat)), 0.1)    # lon degrees are shorter

    tip_lat = lat + length_deg * np.cos(toward)
    tip_lon = lon + length_deg * np.sin(toward) / coslat

    head = []
    for barb in (toward + np.deg2rad(150), toward - np.deg2rad(150)):
        head.append((tip_lat + 0.32 * length_deg * np.cos(barb),
                     tip_lon + 0.32 * length_deg * np.sin(barb) / coslat))

    pts = [(lat, lon), (tip_lat, tip_lon), head[0],
           (tip_lat, tip_lon), head[1]]
    return [[p[1], p[0]] for p in pts]


# --------------------------------------------------------------------------
# 4. Map
# --------------------------------------------------------------------------
def build_map(df: pd.DataFrame, out="hn_wind_map.html",
              arrow_deg: float | None = None, weight: float = 2.5) -> str:
    import branca.colormap as cm
    import folium
    from folium.plugins import TimestampedGeoJson

    if arrow_deg is None:
        # Scale to the network: an arrow should be a fraction of the spacing
        # between stations, or a country-scale constant swamps a city-scale net.
        span = max(df["lat"].max() - df["lat"].min(),
                   df["lon"].max() - df["lon"].min(), 0.02)
        arrow_deg = float(np.clip(span * 0.35, 0.01, MAX_ARROW_DEG))
        print(f"  arrow length scaled to {arrow_deg:.3f} deg")

    vmax = float(np.ceil(df["speed_ms"].quantile(0.98)))
    ramp = cm.LinearColormap(
        ["#3b6ea5", "#63a375", "#e0c368", "#d97b3f", "#b8434a"],
        vmin=0, vmax=vmax, caption="Wind speed (m/s)")

    features = []
    for row in df.itertuples():
        length = arrow_deg * min(row.speed_ms / vmax, 1.0)
        if length < arrow_deg * 0.15:
            length = arrow_deg * 0.15
        stamp = row.hour.isoformat()
        coords = arrow(row.lat, row.lon, row.drct, length)
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                # TimestampedGeoJson wants one timestamp per coordinate
                "times": [stamp] * len(coords),
                "style": {"color": ramp(min(row.speed_ms, vmax)),
                          "weight": weight, "opacity": 0.9},
                "popup": (f"<b>{row.station}</b><br>{row.hour:%Y-%m-%d %H:%M} UTC"
                          f"<br>{row.drct:.0f}&deg; at {row.sknt:.0f} kt"
                          f" ({row.speed_ms:.1f} m/s)"),
            },
        })

    m = folium.Map(tiles="CartoDB positron")
    m.fit_bounds([[df["lat"].min(), df["lon"].min()],
                  [df["lat"].max(), df["lon"].max()]], padding=(40, 40))

    # Station dots stay put so the map never looks empty during gaps.
    for stn, g in df.groupby("station"):
        folium.CircleMarker(
            [g["lat"].iloc[0], g["lon"].iloc[0]], radius=3,
            color="#444", fill=True, fill_opacity=1,
            tooltip=f"{stn} ({len(g)} hourly obs)",
        ).add_to(m)

    TimestampedGeoJson(
        {"type": "FeatureCollection", "features": features},
        period="PT1H", duration="PT1H",      # each arrow lives exactly its hour
        transition_time=180, auto_play=False, loop=False,
        date_options="YYYY-MM-DD HH:mm [UTC]",
    ).add_to(m)

    ramp.add_to(m)
    m.save(str(out))
    return out


# --------------------------------------------------------------------------
# 5. Wind roses (optional)
# --------------------------------------------------------------------------
def build_roses(df: pd.DataFrame, out="hn_wind_roses.html") -> str:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    stations = sorted(df["station"].unique())
    cols = 4
    rows = int(np.ceil(len(stations) / cols))
    fig = make_subplots(rows=rows, cols=cols, subplot_titles=stations,
                        specs=[[{"type": "polar"}] * cols for _ in range(rows)])

    sectors = np.arange(0, 360, 22.5)
    bands = [(0, 3), (3, 6), (6, 10), (10, 99)]
    shades = ["#cfe0ee", "#8fb8d8", "#4b7fae", "#1d4e79"]

    for i, stn in enumerate(stations):
        g = df[df["station"] == stn]
        r, c = i // cols + 1, i % cols + 1
        sec = ((g["drct"] + 11.25) // 22.5 % 16).astype(int)
        for (lo, hi), shade in zip(bands, shades):
            band = g["speed_ms"].between(lo, hi)
            counts = np.bincount(sec[band], minlength=16) / len(g) * 100
            fig.add_trace(
                go.Barpolar(r=counts, theta=sectors, width=20,
                            marker_color=shade, showlegend=(i == 0),
                            name=f"{lo}-{hi} m/s"),
                row=r, col=c)

    fig.update_polars(angularaxis_direction="clockwise",
                      angularaxis_rotation=90, radialaxis_showticklabels=False)
    fig.update_layout(height=280 * rows, title="Wind roses (% of observations, "
                                               "direction wind blows FROM)")
    fig.write_html(str(out))
    return out


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--start", help="UTC start date, YYYY-MM-DD")
    p.add_argument("--end", help="UTC end date, YYYY-MM-DD")
    p.add_argument("--network", default="HN__ASOS")
    p.add_argument("--csv", help="use an already-downloaded IEM CSV instead")
    p.add_argument("--demo", action="store_true", help="synthetic data")
    p.add_argument("--out", default="hn_wind_map.html")
    wp.add_data_arg(p)
    p.add_argument("--roses", action="store_true", help="also build wind roses")
    p.add_argument("--freq", default="h",
                   help="resampling interval: h, 30min, 15min (default h)")
    p.add_argument("--arrow-deg", type=float,
                   help="override arrow length in degrees")
    p.add_argument("--weight", type=float, default=2.5,
                   help="arrow stroke width in pixels (default 2.5)")
    wp.add_config_arg(p)
    wp.apply_config(p, "windmap")
    a = p.parse_args()

    root = wp.data_root(a.data_dir)
    if a.demo:
        print("Demo mode: synthetic stations.")
        raw = demo_frame()
    elif a.csv:
        src = wp.require(wp.in_path(a.csv, wp.RAW, root), "observations csv")
        wp.report("reading", src)
        raw = pd.read_csv(src, comment="#", low_memory=False)
    else:
        if not (a.start and a.end):
            p.error("need --start and --end (or --csv, or --demo)")
        fmt = "%Y-%m-%d"
        raw = fetch_iem(datetime.strptime(a.start, fmt).replace(tzinfo=timezone.utc),
                        datetime.strptime(a.end, fmt).replace(tzinfo=timezone.utc),
                        a.network)

    print("Cleaning ...")
    df = clean(raw, a.freq)
    if df.empty:
        print("No usable observations. Widen the date range?")
        return 1

    span = f"{df['hour'].min():%Y-%m-%d %H:%M} to {df['hour'].max():%Y-%m-%d %H:%M} UTC"
    print(f"  {df['station'].nunique()} stations, {len(df)} arrows at {a.freq}, {span}")
    counts = df.groupby("station").size().sort_values(ascending=False)
    print("  reporting density:",
          ", ".join(f"{s}={n}" for s, n in counts.items()))

    print("Rendering map ...")
    print("  wrote", build_map(df, wp.out_path(a.out, wp.MAPS, root), a.arrow_deg, a.weight))
    if a.roses:
        print("  wrote", build_roses(df, wp.out_path("hn_wind_roses.html", wp.MAPS, root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
