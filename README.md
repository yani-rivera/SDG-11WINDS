# hn_era5_wind

Download ERA5 reanalysis winds for any bounding box, render them as an
animated particle-flow map, and compare the model's terrain and winds against
ground observations.

Nothing here is specific to Honduras despite the filename — the bounding box
is an argument, and the map centres and scales itself on whatever you fetch.

---

## What it does

| Command | Output |
|---|---|
| `creds` | check your Copernicus credentials are readable |
| `fetch` | download 10 m winds for a box and date range → netCDF |
| `map` | animated particle-flow map → standalone HTML |
| `terrain` | model surface elevation vs real ground → CSV |
| `validate` | model winds vs station observations → CSV |

`fetch` and `terrain` need a free Copernicus account. `map` needs only a
netCDF, and runs on synthetic data with `--demo` if you just want to see the
renderer work.

---

## Install

```bash
conda create -n era5wind -c conda-forge python=3.12 \
    numpy pandas scipy xarray netcdf4 requests
conda activate era5wind
pip install "cdsapi>=0.7.7"
```

Two files are required: `hn_era5_wind.py` and `wind_paths.py`, in the same
directory.

---

## Credentials

Register at <https://cds.climate.copernicus.eu> and copy your personal access
token from your profile page.

Recommended — keep it in the environment, out of the repository:

```bash
conda env config vars set CDSAPI_URL=https://cds.climate.copernicus.eu/api
conda env config vars set CDSAPI_KEY=<your token>
conda deactivate && conda activate era5wind
```

Alternatives, in order of precedence: `--key` on the command line, then
environment variables, then a `.env` file in the project root, then
`~/.cdsapirc`.

Verify without printing the token:

```bash
python hn_era5_wind.py creds
```

```
  CDSAPI_URL     : https://cds.climate.copernicus.eu/api
  CDSAPI_KEY     : set, 36 chars
  client initialised OK.
```

**One more step catches most first-run failures:** open the
[ERA5 single-levels dataset page](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels)
in a browser, scroll to the download form, and accept the terms. Without that,
credentials are valid but every retrieve returns 403.

---

## Quickstart

```bash
python hn_era5_wind.py fetch --start 2026-07-10 --end 2026-07-17 \
    --area 18.5 -91.0 11.5 -82.0
python hn_era5_wind.py map --nc era5_honduras.nc
```

Open `data/maps/hn_era5_map.html` in any browser. It is self-contained — all
frames are embedded, so it can be emailed or hosted anywhere.

---

## Bounding boxes

**The order is North, West, South, East.** This is the Copernicus convention,
not the more common west/south/east/north. Getting it wrong usually produces
an empty grid rather than an error.

In the Americas, west and east are both negative.

```bash
--area 18.5 -91.0 11.5 -82.0     # Honduras plus surrounding ocean
--area 14.25 -87.35 13.95 -87.05 # Tegucigalpa only
--area 0.30 -78.80 -0.60 -78.20  # Quito, across the equator
--area 28.10 84.90 27.30 85.90   # Kathmandu, eastern hemisphere
```

The box is snapped outward to the reanalysis grid before requesting, so
nothing you asked for is clipped, and the grid size is printed before the
download starts:

```
box snapped to the 0.25 deg grid: [18.5, -91.0, 11.5, -82.0]
grid: 37 x 29 = 1073 points per hour
```

That number is the best predictor of download time and file size. Queue times
scale with request size — a tight box can turn a twenty-minute wait into under
a minute.

**Leave ocean on both sides if you want the flow map to look right.** Particles
spawn at the edge of the data, so a box cut tight to a coastline produces a
visible hard line where they appear from nothing.

---

## Resolution

Default is ERA5 at 0.25°, roughly 28 km. `--land` switches to ERA5-Land at
0.1°, roughly 9 km — better over terrain, but land-only, so islands and
coastal water drop out of the field entirely.

Neither resolves cities. At 0.25° a metropolitan area is one or two grid
cells, and the model's terrain is an average over the whole cell. Shrinking
the box does not add detail.

---

## The flow map

```bash
python hn_era5_wind.py map --nc era5_honduras.nc \
    --line-width 2.5 --particle-age 45 --particle-density 0.0015
```

| Option | Effect |
|---|---|
| `--particle-age` | frames a particle lives; lower = shorter trails |
| `--particle-density` | particles per pixel; lower = sparser |
| `--velocity-scale` | distance travelled per frame |
| `--line-width` | stroke width |
| `--max-frames` | thin long windows so the file stays usable |

The colour ramp is scaled to the 98th percentile of your actual wind speeds,
printed when the map is built. This matters: a hardcoded ceiling far above
the real range collapses every particle to one shade.

All frames are embedded in the HTML, so file size grows with the window.
`--max-frames` subsamples; the size is printed and warned about past 40 MB.

### Overlaying observations

```bash
python hn_era5_wind.py map --nc era5.nc --obs stations.csv
```

`--obs` takes a CSV with columns `station, valid, lat, lon, drct, sknt` —
timestamp in UTC, direction in degrees the wind comes *from*, speed in knots.
Stations are drawn as arrows, or hollow circles when reporting calm, matched
to the nearest model hour within 30 minutes.

The two windows must overlap. If they do not, the map still draws station
markers and prints both date ranges rather than silently showing nothing.
**ERA5 lags real time by about five days**, so very recent observations will
have no model to compare against yet.

---

## Terrain comparison

```bash
python hn_era5_wind.py terrain --stations stations.csv
python hn_era5_wind.py terrain --stations stations.csv --dem local_dtm.tif
```

Downloads ERA5's static surface geopotential, converts it to elevation
(divide by g = 9.80665), and compares it against real ground at each station —
from a local DEM if you have one, otherwise SRTM 30 m via the free
[Open Topo Data](https://www.opentopodata.org/) API. Points outside a local
DEM fall back to SRTM rather than being dropped.

This answers "how far is the model's ground from the real ground", which
bounds how much any wind comparison can mean. In a city sitting in a basin,
a single grid cell may be a hundred metres or more from every station in it,
in both directions at once.

A bare-earth DTM is the right input here. A surface model includes vegetation
and buildings, which is a few metres against a discrepancy that is usually far
larger — but state which you used.

---

## Validation

```bash
python hn_era5_wind.py validate --nc era5.nc --obs stations.csv
```

Nearest gridpoint and nearest hour for each observation, then per station:
speed bias, RMSE, correlation, and a circular-mean direction bias. Sorted by
RMSE, worst first.

Read the bias as terrain rather than error. The largest disagreements are
where the model's smoothed topography differs most from the real ground the
station stands on — which is what `terrain` quantifies.

---

## Configuration

Every option can live in a `config.toml` beside the scripts instead of on the
command line.

**Precedence, highest first:** command-line argument → environment variable
(credentials only) → `config.toml` → coded default.

Keys are the long option names with dashes as underscores, in a section per
subcommand:

```toml
data_dir = "data"

[era5_fetch]
area = [18.5, -91.0, 11.5, -82.0]
out  = "era5_honduras.nc"

[era5_map]
nc               = "era5_honduras.nc"
particle_age     = 45
particle_density = 0.002
line_width       = 2.5
```

```bash
python hn_era5_wind.py map --show-config    # what was picked up, and from where
python hn_era5_wind.py map --config other.toml
```

The file is found by walking up from the working directory, so commands work
from any subdirectory. **Never put credentials in it** — it is meant to be
committed.

---

## Where files go

```
data/
  raw/         downloads as they arrived (netCDF)
  processed/   derived tables (terrain, validation)
  maps/        rendered HTML
```

A bare filename in any argument is routed to the right subfolder; anything
containing a slash is used exactly as given. Inputs also fall back to
searching the data tree, so a file written by one step is found by the next
without you tracking which folder it went to.

Override the root with `--data-dir`, `WIND_DATA_DIR`, or `data_dir` in the
config.

---

## Known quirks

**`~/.cdsapirc` does not strip quotes.** `key: "abc123"` is read literally,
quote characters included, and fails with an unhelpful message. No quotes.

**No `UID:` prefix.** The current CDS wants the token alone. The older format
had a numeric prefix; `creds` flags it if it sees one.

**Time axis naming.** The current CDS writes `valid_time` rather than `time`.
Handled automatically, along with the `expver` dimension that appears on
requests spanning the ERA5/ERA5T boundary.

**leaflet-velocity is pinned to 2.1.4** in the generated HTML, loaded from a
CDN. The map needs an internet connection to render, though not to exist.

**`terrain` needs the geopotential file, not the wind file.** It downloads its
own; pointing `--nc` at a wind netCDF produces a clear error rather than a
traceback.

---

## Attribution

ERA5 is distributed under **CC-BY 4.0** (since 2 July 2025), which permits
redistribution including of modified versions, with attribution.

Cite:

> Copernicus Climate Change Service (2023): ERA5 hourly data on single levels
> from 1940 to present. Copernicus Climate Change Service (C3S) Climate Data
> Store (CDS), DOI: [10.24381/cds.adbb2d47](https://doi.org/10.24381/cds.adbb2d47)
> (Accessed on 20-07-2026)

And include:

> Contains modified Copernicus Climate Change Service information YYYY.
> Neither the European Commission nor ECMWF is responsible for any use that
> may be made of the Copernicus information or data it contains.

Use *Contains modified* rather than *Generated using* — subsetting and
regridding count as modification.

ERA5-Land has its own DOI: `10.24381/cds.e2161bac`.

If you use the SRTM fallback in `terrain`, attribute Open Topo Data and NASA
SRTM as well.

---

## Limitations worth stating in any writeup

ERA5 is a reanalysis — a forecast model rerun over the past with observations
assimilated. It is not satellite imagery, and `u10`/`v10` are winds at
**10 metres**, the same nominal height as a weather station mast.



That makes the model good at the synoptic picture and structurally unable to
represent what happens inside a city. Both statements can be true at once, and
a comparison that treats the model as ground truth — or as simply wrong — will
mislead in either direction.
