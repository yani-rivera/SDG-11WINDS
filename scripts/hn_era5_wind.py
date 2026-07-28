"""
Honduras ERA5 wind -- gridded flow field + validation against station obs
=========================================================================

Three subcommands:

    fetch     download ERA5 10 m winds over Honduras from the CDS
    map       render an animated particle-flow map (standalone HTML)
    validate  compare ERA5 against the ASOS station observations

Setup
-----
    pip install "cdsapi>=0.7.7" xarray netcdf4 numpy pandas matplotlib

    ~/.cdsapirc:
        url: https://cds.climate.copernicus.eu/api
        key: <YOUR-PERSONAL-ACCESS-TOKEN>

    Copy the url line from your own CDS "How to API" page rather than
    trusting the one above -- the endpoint has moved before.

    You must also click "accept terms" on the dataset's download form
    once in the browser, or every retrieve returns 403.

Usage
-----
    python hn_era5_wind.py fetch --start 2024-10-01 --end 2024-10-08
    python hn_era5_wind.py map --nc era5_honduras.nc
    python hn_era5_wind.py validate --nc era5_honduras.nc --obs stations.csv
    python hn_era5_wind.py map --demo        # synthetic, no account needed
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import xarray as xr

import wind_paths as wp

# Bounding box: North, West, South, East (the order the CDS wants).
# This default covers Honduras; override with --area on the command line.
AREA = [17.0, -89.5, 12.5, -83.0]
KT_TO_MS = 0.514444


def snap_area(area, step: float):
    """Round a box outward to the reanalysis grid so nothing is clipped."""
    n, w, s, e = (float(x) for x in area)
    snapped = [np.ceil(n / step) * step, np.floor(w / step) * step,
               np.floor(s / step) * step, np.ceil(e / step) * step]
    snapped = [round(float(x), 4) for x in snapped]
    if snapped != [round(x, 4) for x in (n, w, s, e)]:
        print(f"  box snapped to the {step} deg grid: {snapped}")
    ny = int(round((snapped[0] - snapped[2]) / step)) + 1
    nx = int(round((snapped[3] - snapped[1]) / step)) + 1
    print(f"  grid: {nx} x {ny} = {nx * ny} points per hour")
    return snapped


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------
def cmd_fetch(a) -> int:
    import cdsapi

    env_file = apply_credentials(a)
    if env_file:
        print(f"  loaded {env_file}")
    print(f"  credentials: {wp.credential_source()}")

    if not (a.start and a.end):
        raise SystemExit("\n--start and --end are required (or set them under "
                         "[era5_fetch] in config.toml).")
    start = datetime.strptime(a.start, "%Y-%m-%d")
    end = datetime.strptime(a.end, "%Y-%m-%d")
    days = pd.date_range(start, end, freq="D")

    dataset = "reanalysis-era5-land" if a.land else "reanalysis-era5-single-levels"
    step = 0.1 if a.land else 0.25
    grid = [step, step]
    area = snap_area(a.area or AREA, step)

    request = {
        "product_type": ["reanalysis"],
        "variable": ["10m_u_component_of_wind", "10m_v_component_of_wind"],
        "year": sorted({f"{d.year}" for d in days}),
        "month": sorted({f"{d.month:02d}" for d in days}),
        "day": sorted({f"{d.day:02d}" for d in days}),
        "time": [f"{h:02d}:00" for h in range(24)],
        "area": area,
        "grid": grid,
        "data_format": "netcdf",
        "download_format": "unarchived",   # else you get a .zip
    }
    if a.land:
        request.pop("product_type")        # ERA5-Land has no product_type

    print(f"Requesting {dataset} ...")
    print("  (queue times of several minutes are normal)")
    target = wp.out_path(a.out, wp.RAW, wp.data_root(a.data_dir))
    cdsapi.Client().retrieve(dataset, request, str(target))
    print("  wrote", target)
    return 0


def apply_credentials(a) -> str | None:
    """Resolve credentials: --key/--url > shell env > .env > ~/.cdsapirc."""
    env_file = wp.load_dotenv()
    if getattr(a, "url", None):
        os.environ["CDSAPI_URL"] = a.url
    if getattr(a, "key", None):
        os.environ["CDSAPI_KEY"] = a.key.strip()
        print("  NOTE: --key is recorded in your shell history and is visible")
        print("        to other processes. Prefer .env or a conda env var.")
    elif getattr(a, "ask_key", False):
        import getpass
        os.environ["CDSAPI_KEY"] = getpass.getpass("CDS token: ").strip()
        os.environ.setdefault("CDSAPI_URL",
                              "https://cds.climate.copernicus.eu/api")
    return env_file


def add_credential_args(parser) -> None:
    parser.add_argument("--key", help="CDS token (leaks into shell history; "
                                      "prefer .env or --ask-key)")
    parser.add_argument("--url", help="override CDSAPI_URL")
    parser.add_argument("--ask-key", action="store_true",
                        help="prompt for the token, no echo, no history")


def cmd_creds(a) -> int:
    """Report credential status without ever printing the token."""
    env_file = apply_credentials(a)
    print(f"  .env file      : {env_file or 'none found'}")

    url = os.environ.get("CDSAPI_URL")
    key = os.environ.get("CDSAPI_KEY")
    rc = Path(os.environ.get("CDSAPI_RC", "~/.cdsapirc")).expanduser()
    print(f"  CDSAPI_URL     : {url or '(not set)'}")
    print(f"  CDSAPI_KEY     : {'set, ' + str(len(key)) + ' chars' if key else '(not set)'}")
    print(f"  ~/.cdsapirc    : {'exists' if rc.is_file() else 'not present'}")

    problems = []
    if key:
        if key[0] in "\"'" or key[-1] in "\"'":
            problems.append("CDSAPI_KEY is wrapped in quotes -- remove them")
        if key.count(":") == 1:
            problems.append("CDSAPI_KEY looks like the old UID:KEY form -- "
                            "the current CDS wants the token alone")
        if key != key.strip():
            problems.append("CDSAPI_KEY has leading or trailing whitespace")
    if url and not url.rstrip("/").endswith("/api"):
        problems.append(f"CDSAPI_URL should end in /api -- got {url}")

    if not (url and key) and not rc.is_file():
        problems.append("No credentials anywhere. Create .env in the project "
                        "root with CDSAPI_URL and CDSAPI_KEY.")

    print()
    if problems:
        for p_ in problems:
            print(f"  PROBLEM: {p_}")
        return 1

    try:
        import cdsapi
        cdsapi.Client()
    except Exception as exc:
        print(f"  client refused the config: {exc}")
        return 1
    print("  client initialised OK.")
    print("  (This does not check dataset licences -- if fetch returns 403,")
    print("   accept the terms on the dataset's download form in the browser.)")
    return 0


# --------------------------------------------------------------------------
# shared loading
# --------------------------------------------------------------------------
def load_grid(path) -> xr.Dataset:
    ds = xr.open_dataset(path)
    # The new CDS names the time axis valid_time; the old one used time.
    for old, new in (("valid_time", "time"), ("longitude", "lon"), ("latitude", "lat")):
        if old in ds.dims or old in ds.coords:
            ds = ds.rename({old: new})
    if "expver" in ds.dims:                 # mixed ERA5/ERA5T requests
        ds = ds.reduce(np.nanmax, dim="expver", keep_attrs=True)
    ds = ds.sortby("lat", ascending=False).sortby("lon")
    return ds[["u10", "v10"]]


def demo_grid() -> xr.Dataset:
    """Synthetic trade-wind field with a bit of curvature, for testing."""
    lat = np.arange(17.0, 12.4, -0.25)
    lon = np.arange(-89.5, -82.9, 0.25)
    time = pd.date_range("2024-10-01", periods=48, freq="h")
    LON, LAT = np.meshgrid(lon, lat)
    u = np.empty((len(time), len(lat), len(lon)))
    v = np.empty_like(u)
    for i, _ in enumerate(time):
        phase = i / len(time) * 2 * np.pi
        # Easterlies, stronger over water, deflected around the interior.
        base = -7.5 - 2.0 * np.cos(phase)
        u[i] = base * (1 + 0.25 * np.sin(np.deg2rad(LAT * 12)))
        v[i] = 2.5 * np.sin(np.deg2rad((LON + 86) * 20)) + 0.8 * np.cos(phase)
    return xr.Dataset(
        {"u10": (("time", "lat", "lon"), u), "v10": (("time", "lat", "lon"), v)},
        coords={"time": time, "lat": lat, "lon": lon},
    )


# --------------------------------------------------------------------------
# map
# --------------------------------------------------------------------------
VELOCITY_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>Honduras ERA5 winds</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet-velocity@2.1.4/dist/leaflet-velocity.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet-velocity@2.1.4/dist/leaflet-velocity.js"></script>
<style>
  html,body{margin:0;height:100%%;background:#0d1420;font:13px/1.4 system-ui,sans-serif}
  #map{height:100%%}
  #bar{position:absolute;z-index:1000;bottom:0;left:0;right:0;padding:10px 16px;
       background:rgba(13,20,32,.88);color:#dfe6ef;display:flex;gap:14px;align-items:center}
  #bar input{flex:1}
  #stamp{font-variant-numeric:tabular-nums;min-width:180px}
  button{background:#28405e;color:#dfe6ef;border:0;padding:5px 12px;border-radius:4px;cursor:pointer}
  #note{position:absolute;z-index:1000;top:8px;right:8px;max-width:250px;padding:8px 10px;
        background:rgba(13,20,32,.85);color:#9fb0c6;border-radius:4px;font-size:11px}
  #legend{margin-top:8px;border-top:1px solid #2a3a52;padding-top:6px;color:#c8d4e2}
  #legend b{color:#ffd782}
  .calm{stroke:none}
</style></head><body>
<div id="map"></div>
<div id="note">ERA5 reanalysis at %(res)s&deg; (about %(km)d km). The model smooths
terrain at that scale &mdash; this is the large-scale flow, not what a station
in a valley records.
<div id="legend">%(legend)s</div></div>
<div id="bar">
  <button id="play">Play</button>
  <input type="range" id="slider" min="0" max="%(last)d" value="0" step="1">
  <span id="stamp"></span>
</div>
<script>
const FRAMES = %(frames)s;
const STAMPS = %(stamps)s;
const map = L.map('map', {zoomControl:true}).setView([%(clat)s,%(clon)s], %(zoom)d);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  {attribution:'&copy; OpenStreetMap, &copy; CARTO | Wind: ERA5 (Copernicus)',
   subdomains:'abcd', maxZoom:12}).addTo(map);

const layer = L.velocityLayer({
  displayValues:true,
  displayOptions:{velocityType:'Wind', position:'bottomleft',
                  displayPosition:'bottomleft', displayEmptyString:'No data',
                  speedUnit:'m/s'},
  data: FRAMES[0],
  minVelocity: 0,
  maxVelocity: %(vmax)s,
  velocityScale: %(vscale)s,
  particleAge: %(page)d,
  particleMultiplier: %(pmult)s,
  lineWidth: %(lwidth)s,
  frameRate: 18,
  colorScale: ['#2f5d8a','#3f8f7d','#8fae54','#e0c368','#d97b3f','#b8434a']
}).addTo(map);

const slider=document.getElementById('slider'), stamp=document.getElementById('stamp');
let show = function(i){ layer.setData(FRAMES[i]); stamp.textContent = STAMPS[i]; slider.value = i; };
slider.addEventListener('input', e => show(+e.target.value));

let timer=null;
document.getElementById('play').addEventListener('click', function(){
  if(timer){ clearInterval(timer); timer=null; this.textContent='Play'; return; }
  this.textContent='Pause';
  timer = setInterval(() => {
    let i = (+slider.value + 1) %% FRAMES.length;
    show(i);
  }, 500);
});
const SITES = %(sites)s;
if (SITES) {
  SITES.forEach(st => {
    L.circleMarker([st.lat, st.lon], {radius:3, color:'#ffd782', weight:1.5,
      fillColor:'#ffd782', fillOpacity:.5}).bindTooltip(st.name,
      {permanent:false}).addTo(map);
  });
  const b = L.latLngBounds(SITES.map(st => [st.lat, st.lon])).pad(3.0);
  L.rectangle(b, {color:'#ffd782', weight:1, fill:false, dashArray:'4,4',
                  opacity:.5}).addTo(map);
}
const STATIONS = %(stations)s;
let obsLayer = null;
function drawObs(i){
  if(!STATIONS) return;
  if(obsLayer){ map.removeLayer(obsLayer); }
  const feats = STATIONS[i] || [];
  obsLayer = L.layerGroup();
  feats.forEach(f => {
    if(f.calm){
      L.circleMarker([f.lat, f.lon], {radius:5, color:'#ffd782', weight:2,
        fillColor:'#0d1420', fillOpacity:1}).bindTooltip(f.name + ': calm').addTo(obsLayer);
    } else {
      L.polyline(f.pts, {color:'#ffd782', weight:3, opacity:1})
       .bindTooltip(f.name + ': ' + f.dir + '&deg; at ' + f.spd + ' m/s').addTo(obsLayer);
    }
  });
  obsLayer.addTo(map);
}
const _show = show;
show = function(i){ _show(i); drawObs(i); };
show(0);
</script></body></html>
"""


def velocity_frame(ds: xr.Dataset, i: int) -> list:
    """Pack one timestep into the leaflet-velocity two-record JSON format."""
    lat = ds["lat"].values
    lon = ds["lon"].values
    dy = float(abs(lat[1] - lat[0]))
    dx = float(abs(lon[1] - lon[0]))
    stamp = pd.Timestamp(ds["time"].values[i]).strftime("%Y-%m-%dT%H:%M:%SZ")

    header = {
        "parameterUnit": "m.s-1", "parameterCategory": 2,
        "dx": dx, "dy": dy, "nx": len(lon), "ny": len(lat),
        # la1/lo1 is the NW corner; data runs west->east then north->south.
        "la1": float(lat[0]), "la2": float(lat[-1]),
        "lo1": float(lon[0] % 360), "lo2": float(lon[-1] % 360),
        "refTime": stamp,
    }
    out = []
    for var, number, name in (("u10", 2, "eastward_wind"),
                              ("v10", 3, "northward_wind")):
        arr = np.nan_to_num(ds[var].isel(time=i).values, nan=0.0)
        out.append({
            "header": {**header, "parameterNumber": number,
                       "parameterNumberName": name},
            "data": [round(float(x), 2) for x in arr.ravel()],
        })
    return out


def arrow(lat: float, lon: float, drct: float, length_deg: float):
    """5-point arrow polyline in GeoJSON [lon, lat] order. Mirrors the one
    in hn_wind_map.py so this script stays standalone."""
    toward = np.deg2rad((drct + 180.0) % 360.0)
    coslat = max(np.cos(np.deg2rad(lat)), 0.1)
    tip_lat = lat + length_deg * np.cos(toward)
    tip_lon = lon + length_deg * np.sin(toward) / coslat
    head = []
    for barb in (toward + np.deg2rad(150), toward - np.deg2rad(150)):
        head.append((tip_lat + 0.32 * length_deg * np.cos(barb),
                     tip_lon + 0.32 * length_deg * np.sin(barb) / coslat))
    pts = [(lat, lon), (tip_lat, tip_lon), head[0], (tip_lat, tip_lon), head[1]]
    return [[p[1], p[0]] for p in pts]


def station_frames(obs_path, times, arrow_deg: float):
    """Snap observations onto the ERA5 timestamps and pre-compute arrows."""
    df = pd.read_csv(obs_path)
    df["valid"] = pd.to_datetime(df["valid"], utc=True, errors="coerce")
    for c in ("lat", "lon", "drct", "sknt"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["valid", "lat", "lon", "drct"])
    df["speed_ms"] = df["sknt"] * KT_TO_MS

    stamps = pd.DatetimeIndex(pd.to_datetime(times)).tz_localize("UTC")
    vmax = max(float(df["speed_ms"].quantile(0.95)), 1.0)
    frames = []
    for t in stamps:
        # nearest observation within half an hour of this model timestep
        near = df[(df["valid"] - t).abs() <= pd.Timedelta("30min")]
        picked = (near.assign(off=(near["valid"] - t).abs())
                      .sort_values("off").groupby("station", as_index=False).first())
        feats = []
        for r in picked.itertuples():
            calm = (r.speed_ms == 0) or (r.drct == 0 and r.speed_ms == 0)
            item = {"name": r.station, "lat": round(r.lat, 5),
                    "lon": round(r.lon, 5), "calm": bool(calm)}
            if not calm:
                length = arrow_deg * min(r.speed_ms / vmax, 1.0)
                coords = arrow(r.lat, r.lon, r.drct, max(length, arrow_deg * 0.2))
                item["pts"] = [[round(c[1], 5), round(c[0], 5)] for c in coords]
                item["dir"] = int(r.drct)
                item["spd"] = round(r.speed_ms, 1)
            feats.append(item)
        frames.append(feats)
    n = sum(len(f) for f in frames)
    if n == 0:
        print(f"  WARNING: no overlap. Observations span "
              f"{df['valid'].min()} to {df['valid'].max()}, grid spans "
              f"{stamps.min()} to {stamps.max()}. Fetch a matching window.")
    else:
        calm = sum(1 for f in frames for i in f if i["calm"])
        print(f"  overlaid {n} station observations across {len(frames)} frames "
              f"({100*calm/n:.0f}% calm)")
    return frames


def cmd_map(a) -> int:
    root = wp.data_root(a.data_dir)
    ds = demo_grid() if a.demo else load_grid(wp.require(wp.in_path(a.nc, wp.RAW, root), "ERA5 netcdf"))
    n = ds.sizes["time"]
    if a.max_frames and n > a.max_frames:
        step = int(np.ceil(n / a.max_frames))
        ds = ds.isel(time=slice(None, None, step))
        n = ds.sizes["time"]
        print(f"  subsampled to every {step}h -> {n} frames")

    frames = [velocity_frame(ds, i) for i in range(n)]
    stamps = [pd.Timestamp(t).strftime("%Y-%m-%d %H:%M UTC")
              for t in ds["time"].values]
    lat, lon = ds["lat"].values, ds["lon"].values
    res = round(float(abs(lon[1] - lon[0])), 2)
    span = max(float(lon.max() - lon.min()), float(lat.max() - lat.min()))
    zoom = int(np.clip(round(np.log2(360.0 / max(span, 0.1))) + 1, 2, 12))

    # Scale the colour ramp to the data, not to a guessed ceiling.
    spd = np.hypot(ds["u10"].values, ds["v10"].values)
    vmax = float(np.nanpercentile(spd, 98))
    print(f"  wind speed: median {np.nanmedian(spd):.1f}, "
          f"p98 {vmax:.1f}, max {np.nanmax(spd):.1f} m/s")

    stations, sites, legend = "null", "null", ""
    if a.obs:
        obs_path = wp.require(wp.in_path(a.obs, wp.PROCESSED, root),
                              "observations csv")
        print(f"  observations: {obs_path}")
        # Station arrows sized to the network, not the country.
        sf = station_frames(obs_path, ds["time"].values, span * 0.02)
        stations = json.dumps(sf, separators=(",", ":"))
        pos = (pd.read_csv(obs_path).groupby("station", as_index=False)
                 [["lat", "lon"]].first())
        sites = json.dumps([{"name": r.station, "lat": round(r.lat, 5),
                             "lon": round(r.lon, 5)} for r in pos.itertuples()],
                           separators=(",", ":"))
        print(f"  station markers: {len(pos)} sites drawn permanently")
        legend = ("<b>&#9472;</b> observed station wind<br>"
                  "<b>&#9711;</b> station reporting calm<br>"
                  "dashed box = station network<br>"
                  "particles = ERA5 model")

    html = VELOCITY_HTML % {
        "frames": json.dumps(frames, separators=(",", ":")),
        "stamps": json.dumps(stamps),
        "stations": stations, "sites": sites, "legend": legend,
        "last": n - 1, "res": res, "km": round(res * 111),
        "vmax": round(vmax, 1), "vscale": a.velocity_scale,
        "page": a.particle_age, "pmult": a.particle_density,
        "lwidth": a.line_width,
        "clat": round(float(lat.mean()), 3),
        "clon": round(float(lon.mean()), 3),
        "zoom": zoom,
    }
    target = wp.out_path(a.out, wp.MAPS, root)
    with open(target, "w") as fh:
        fh.write(html)
    size = len(html) / 1e6
    print(f"  wrote {target} ({n} frames, {size:.1f} MB)")
    if size > 40:
        print("  NOTE: large file -- use --max-frames to thin it")
    return 0


# --------------------------------------------------------------------------
# terrain
# --------------------------------------------------------------------------
G0 = 9.80665   # standard gravity, for geopotential -> geopotential height


def srtm_elevations(lats, lons, chunk: int = 90):
    """Real ground elevation per station from the free Open Topo Data API."""
    import requests
    out = []
    for i in range(0, len(lats), chunk):
        pairs = "|".join(f"{a},{b}" for a, b in
                         zip(lats[i:i + chunk], lons[i:i + chunk]))
        r = requests.get("https://api.opentopodata.org/v1/srtm30m",
                         params={"locations": pairs}, timeout=120)
        r.raise_for_status()
        js = r.json()
        if js.get("status") != "OK":
            raise SystemExit(f"Open Topo Data error: {js.get('error')}")
        out += [x["elevation"] for x in js["results"]]
    return out


def cmd_terrain(a) -> int:
    """Compare the model's surface elevation to the real ground at each station."""
    import cdsapi

    root = wp.data_root(a.data_dir)
    obs = pd.read_csv(wp.require(wp.in_path(a.stations, wp.PROCESSED, root),
                                 "stations/tidy csv"))
    sites = (obs.groupby("station", as_index=False)[["lat", "lon"]].first()
                .sort_values("station"))

    nc = wp.out_path(a.nc, wp.RAW, root)
    if not nc.exists():
        apply_credentials(a)
        area = snap_area(a.area or AREA, 0.25)
        print("Requesting ERA5 surface geopotential (static field) ...")
        cdsapi.Client().retrieve("reanalysis-era5-single-levels", {
            "product_type": ["reanalysis"],
            "variable": ["geopotential"],
            "year": ["2024"], "month": ["01"], "day": ["01"], "time": ["00:00"],
            "area": area, "grid": [0.25, 0.25],
            "data_format": "netcdf", "download_format": "unarchived",
        }, str(nc))
    else:
        print(f"  using existing {nc}")

    ds = xr.open_dataset(nc)
    for old, new in (("valid_time", "time"), ("longitude", "lon"),
                     ("latitude", "lat")):
        if old in ds.dims or old in ds.coords:
            ds = ds.rename({old: new})
    z = ds["z"]
    if "time" in z.dims:
        z = z.isel(time=0)

    sel = z.sel(lat=xr.DataArray(sites["lat"].values, dims="i"),
                lon=xr.DataArray(sites["lon"].values, dims="i"), method="nearest")
    sites["era5_elev_m"] = np.round(sel.values / G0, 1)
    sites["era5_lat"] = np.round(sel["lat"].values, 3)
    sites["era5_lon"] = np.round(sel["lon"].values, 3)

    print("Fetching SRTM ground elevations ...")
    sites["ground_elev_m"] = srtm_elevations(list(sites["lat"]), list(sites["lon"]))
    sites["difference_m"] = np.round(sites["era5_elev_m"] - sites["ground_elev_m"], 1)
    sites["cell"] = (sites["era5_lat"].astype(str) + ", " + sites["era5_lon"].astype(str))

    target = wp.out_path(a.out, wp.PROCESSED, root)
    sites.to_csv(target, index=False)

    pd.set_option("display.float_format", lambda x: f"{x:9.1f}")
    print("\nModel terrain vs real ground:\n")
    print(sites[["station", "ground_elev_m", "era5_elev_m",
                 "difference_m", "cell"]].to_string(index=False))
    ncell = sites["cell"].nunique()
    print(f"\n  {len(sites)} stations resolve to {ncell} distinct ERA5 grid cell(s).")
    print(f"  Real ground spans {sites['ground_elev_m'].min():.0f}"
          f"-{sites['ground_elev_m'].max():.0f} m "
          f"({sites['ground_elev_m'].max()-sites['ground_elev_m'].min():.0f} m range).")
    print(f"  The model represents all of it as "
          f"{sites['era5_elev_m'].min():.0f}-{sites['era5_elev_m'].max():.0f} m.")
    print(f"\n  wrote {target}")
    return 0


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------
def cmd_validate(a) -> int:
    root = wp.data_root(a.data_dir)
    ds = demo_grid() if a.demo else load_grid(wp.require(wp.in_path(a.nc, wp.RAW, root), "ERA5 netcdf"))
    if not a.obs:
        raise SystemExit("\n--obs is required (or set it under [era5_validate] "
                         "in config.toml).")
    obs = pd.read_csv(wp.require(wp.in_path(a.obs, wp.RAW, root), "observations csv"),
                      comment="#", low_memory=False)
    obs["valid"] = pd.to_datetime(obs["valid"], utc=True, errors="coerce")
    for c in ("lat", "lon", "drct", "sknt"):
        obs[c] = pd.to_numeric(obs[c], errors="coerce")
    obs = obs.dropna(subset=["valid", "lat", "lon", "drct", "sknt"])
    obs = obs[obs["sknt"] > 0]
    obs = obs[~((obs["drct"] == 0) & (obs["sknt"] > 0))]
    obs["hour"] = obs["valid"].dt.round("h")
    obs = obs.groupby(["station", "hour"], as_index=False).first()

    obs["obs_spd"] = obs["sknt"] * KT_TO_MS
    obs["obs_dir"] = obs["drct"]

    # Nearest ERA5 gridpoint / nearest hour for each observation.
    times = pd.DatetimeIndex(pd.to_datetime(ds["time"].values)).tz_localize("UTC")
    keep = obs["hour"].isin(times)
    if not keep.any():
        print("No overlap between the ERA5 window and the observations.")
        return 1
    obs = obs[keep].copy()

    sel = ds.sel(
        lat=xr.DataArray(obs["lat"].values, dims="i"),
        lon=xr.DataArray(obs["lon"].values, dims="i"),
        time=xr.DataArray(obs["hour"].dt.tz_localize(None).values, dims="i"),
        method="nearest",
    )
    u, v = sel["u10"].values, sel["v10"].values
    obs["era_spd"] = np.hypot(u, v)
    obs["era_dir"] = (np.rad2deg(np.arctan2(-u, -v))) % 360

    d = (obs["era_dir"] - obs["obs_dir"] + 180) % 360 - 180
    obs["dir_diff"] = d

    rows = []
    for stn, g in obs.groupby("station"):
        bias = (g["era_spd"] - g["obs_spd"]).mean()
        rmse = np.sqrt(((g["era_spd"] - g["obs_spd"]) ** 2).mean())
        r = g["era_spd"].corr(g["obs_spd"])
        # Circular mean of the direction difference.
        ang = np.deg2rad(g["dir_diff"])
        dbias = np.rad2deg(np.arctan2(np.sin(ang).mean(), np.cos(ang).mean()))
        rows.append({"station": stn, "n": len(g),
                     "obs_mean": g["obs_spd"].mean(), "era_mean": g["era_spd"].mean(),
                     "speed_bias": bias, "rmse": rmse, "r": r, "dir_bias": dbias})
    summary = pd.DataFrame(rows).sort_values("rmse", ascending=False)

    pd.set_option("display.float_format", lambda x: f"{x:7.2f}")
    print("\nERA5 vs observed, per station "
          "(speed m/s; positive bias = ERA5 too windy)\n")
    print(summary.to_string(index=False))
    target = wp.out_path(a.out, wp.PROCESSED, root)
    summary.to_csv(target, index=False)
    print(f"\n  wrote {target}")
    print("\nRead the bias column as terrain, not error: the largest")
    print("disagreements are where the model's smoothed topography differs")
    print("most from the real valley the station sits in.")
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download ERA5 from the CDS")
    f.add_argument("--start")
    f.add_argument("--end")
    f.add_argument("--area", nargs=4, type=float,
                   metavar=("N", "W", "S", "E"),
                   help="bounding box, e.g. --area 17.0 -89.5 12.5 -83.0 "
                        "(west/south negative in the Americas)")
    f.add_argument("--land", action="store_true",
                   help="ERA5-Land at 0.1 deg (land only, no Bay Islands)")
    f.add_argument("--out", default="era5_honduras.nc")
    add_credential_args(f)
    wp.add_data_arg(f)
    f.set_defaults(func=cmd_fetch)

    m = sub.add_parser("map", help="render the particle-flow map")
    m.add_argument("--nc", default="era5_honduras.nc")
    m.add_argument("--demo", action="store_true")
    m.add_argument("--max-frames", type=int, default=240)
    m.add_argument("--obs", help="tidy observations csv to overlay "
                                 "(e.g. simet_tidy.csv)")
    m.add_argument("--particle-age", type=int, default=45,
                   help="frames a particle lives; lower = shorter trails (45)")
    m.add_argument("--particle-density", type=float, default=1 / 500,
                   help="particles per pixel; lower = sparser (0.002)")
    m.add_argument("--velocity-scale", type=float, default=0.01,
                   help="how far particles travel per frame (0.01)")
    m.add_argument("--line-width", type=float, default=1.2,
                   help="particle stroke width (1.2)")
    m.add_argument("--out", default="hn_era5_map.html")
    wp.add_data_arg(m)
    m.set_defaults(func=cmd_map)

    c = sub.add_parser("creds", help="check CDS credentials are readable")
    add_credential_args(c)
    c.set_defaults(func=cmd_creds)

    t = sub.add_parser("terrain",
                       help="compare model surface elevation to real ground")
    t.add_argument("--stations", default="simet_tidy.csv",
                   help="tidy csv holding station lat/lon")
    t.add_argument("--nc", default="era5_geopotential.nc")
    t.add_argument("--area", nargs=4, type=float, metavar=("N", "W", "S", "E"))
    t.add_argument("--out", default="era5_terrain.csv")
    add_credential_args(t)
    wp.add_data_arg(t)
    t.set_defaults(func=cmd_terrain)

    v = sub.add_parser("validate", help="compare ERA5 to station obs")
    v.add_argument("--nc", default="era5_honduras.nc")
    v.add_argument("--obs", help="IEM CSV from the arrows script")
    v.add_argument("--demo", action="store_true")
    v.add_argument("--out", default="hn_era5_validation.csv")
    wp.add_data_arg(v)
    v.set_defaults(func=cmd_validate)

    # Subparsers get their own config section, so `map` and `fetch` can
    # carry different settings under [era5_map] and [era5_fetch]. Defaults
    # are applied to all of them, but only the invoked one reports.
    sections = {"fetch": ("era5_fetch", f), "map": ("era5_map", m),
                "terrain": ("era5_terrain", t), "validate": ("era5_validate", v),
                "creds": ("era5_creds", c)}
    invoked = next((w for w in sys.argv[1:] if w in sections), None)
    for name, (sect, sub_parser) in sections.items():
        wp.add_config_arg(sub_parser)
        wp.apply_config(sub_parser, sect, quiet=(name != invoked))

    a = p.parse_args()
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
