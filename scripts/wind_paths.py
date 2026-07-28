"""
Shared path handling for the wind scripts.

Layout, created on demand:

    data/
      raw/        downloads exactly as they arrived (IEM csv, ERA5 nc, SIMET pulls)
      processed/  anything we derived (tidy csv, validation tables)
      maps/       rendered html

Rules for any --in/--out argument:

  * a bare filename          -> placed in the right subfolder for its kind
  * anything with a slash    -> used exactly as given
  * an absolute path         -> used exactly as given

So `--out simet_tidy.csv` becomes `data/processed/simet_tidy.csv`, while
`--out ./scratch/thing.csv` stays put. Override the root with --data-dir or
the WIND_DATA_DIR environment variable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

RAW = "raw"
PROCESSED = "processed"
MAPS = "maps"


def load_dotenv(start: Path | None = None) -> str | None:
    """Load KEY=VALUE lines from the nearest .env, walking up from cwd.

    Existing environment variables always win, so an explicit
    `export CDSAPI_KEY=...` still overrides the file.
    """
    here = (start or Path.cwd()).resolve()
    for folder in [here, *here.parents]:
        env = folder / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v
            return str(env)
        if (folder / ".git").exists():
            break
    return None


def credential_source() -> str:
    """Describe where cdsapi will get its credentials, without printing them."""
    if os.environ.get("CDSAPI_URL") and os.environ.get("CDSAPI_KEY"):
        return "environment (CDSAPI_URL / CDSAPI_KEY)"
    rc = Path(os.environ.get("CDSAPI_RC", "~/.cdsapirc")).expanduser()
    if rc.is_file():
        return f"{rc}"
    return ("NOT FOUND -- set CDSAPI_URL and CDSAPI_KEY in a .env file, "
            "or create ~/.cdsapirc")


# --------------------------------------------------------------------------
# config file
# --------------------------------------------------------------------------
# Precedence, highest first:
#   1. command-line argument
#   2. environment variable (credentials only)
#   3. config.toml
#   4. the default coded into the script
#
# Config keys are the long option names with dashes as underscores, so
# `--particle-age 30` on the command line is `particle_age = 30` in the file.

def add_config_arg(parser) -> None:
    parser.add_argument("--config", metavar="TOML",
                        help="path to config.toml (default: nearest one found "
                             "walking up from the working directory)")
    parser.add_argument("--show-config", action="store_true",
                        help="print which settings came from the config file")


def find_config(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    here = Path.cwd().resolve()
    for folder in [here, *here.parents]:
        cfg = folder / "config.toml"
        if cfg.is_file():
            return cfg
        if (folder / ".git").exists():
            break
    return None


def apply_config(parser, section: str | None = None, argv=None,
                 quiet: bool = False):
    """Set parser defaults from config.toml. Call before parse_args().

    Anything given on the command line still wins, because argparse only
    falls back to defaults for options the user did not supply.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    explicit = None
    for i, tok in enumerate(argv):
        if tok == "--config" and i + 1 < len(argv):
            explicit = argv[i + 1]
        elif tok.startswith("--config="):
            explicit = tok.split("=", 1)[1]

    path = find_config(explicit)
    if path is None:
        return None, {}
    if not path.is_file():
        raise SystemExit(f"\nconfig file not found: {path}")

    try:
        import tomllib
    except ModuleNotFoundError:                      # Python < 3.11
        raise SystemExit("config.toml needs Python 3.11+ (tomllib). "
                         "Pass settings as arguments instead.")

    try:
        data = tomllib.loads(path.read_text())
    except Exception as exc:
        raise SystemExit(f"\ncould not parse {path}:\n  {exc}")

    merged = {k: v for k, v in data.items() if not isinstance(v, dict)}
    if section:
        block = data.get(section)
        if isinstance(block, dict):
            merged.update({k: v for k, v in block.items()
                           if not isinstance(v, dict)})

    known = {act.dest for act in parser._actions}
    applied, ignored = {}, []
    for k, v in merged.items():
        (applied.__setitem__(k, v) if k in known else ignored.append(k))
    parser.set_defaults(**applied)

    if ignored and section and not quiet:
        print(f"  note: {path.name} [{section}] has keys this command does not "
              f"use: {', '.join(sorted(ignored))}")
    if "--show-config" in argv and not quiet:
        print(f"  config: {path}")
        for k in sorted(applied):
            print(f"    {k} = {applied[k]!r}")
    return path, applied


def add_data_arg(parser) -> None:
    parser.add_argument(
        "--data-dir", default=None,
        help="root for data/ (default: ./data, or $WIND_DATA_DIR)")


def data_root(cli_value: str | None = None) -> Path:
    return Path(cli_value or os.environ.get("WIND_DATA_DIR") or "data").expanduser()


def _is_explicit(name: str) -> bool:
    p = Path(name)
    return p.is_absolute() or len(p.parts) > 1


def out_path(name: str, kind: str, root: Path) -> Path:
    """Where to write. Creates the parent directory."""
    target = Path(name) if _is_explicit(name) else root / kind / name
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def in_path(name: str, kind: str, root: Path) -> Path:
    """Where to read from. Falls back to a search under the data root."""
    if _is_explicit(name):
        return Path(name)

    candidate = root / kind / name
    if candidate.exists():
        return candidate

    # Be forgiving: the file may have been dropped in a sibling folder.
    for alt in (root, Path(".")):
        if (alt / name).exists():
            return alt / name
    matches = sorted(root.rglob(name)) if root.exists() else []
    if matches:
        return matches[0]

    # Nothing found -- return the expected location so the error names it.
    return candidate


def require(path: Path, label: str = "input") -> Path:
    """Fail with a readable message instead of a parser traceback."""
    if not path.exists():
        raise SystemExit(
            f"\n{label} not found: {path}\n"
            f"  Nothing matching that name under {path.parents[1] if len(path.parents) > 1 else path.parent}.\n"
            f"  Check the filename, or run the step that produces it first.")
    if path.is_dir():
        raise SystemExit(
            f"\n{label} is a directory, not a file: {path}\n"
            f"  This argument wants a single csv. Did you mean one of these?\n" +
            "".join(f"    {c.name}\n" for c in sorted(path.rglob('*.csv'))[:8]))
    if path.stat().st_size == 0:
        raise SystemExit(
            f"\n{label} is empty (0 bytes): {path}\n"
            f"  The step that wrote it probably failed or returned no data.\n"
            f"  Re-run that pull and check its row count before continuing.")
    return path


def report(label: str, path: Path) -> None:
    print(f"  {label}: {path}")
