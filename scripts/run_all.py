"""
LightSignal — run_all.py
==========================
Master pipeline script. Runs the full LightSignal pipeline
from raw input files to final dashboard output in one command.

Usage:
  python scripts/run_all.py              # full run
  python scripts/run_all.py --skip-h3   # skip H3 generation (use existing h3_r*.csv)
  python scripts/run_all.py --no-archive    # skip archiving input files

Pipeline stages:
  1. Archive    — timestamped copies of all raw inputs saved to data/archive/inputs/
  2. Transform  — raw inputs → clean processed CSVs in data/processed/
  3. H3         — processed points → h3_r3/r4/r5.csv with MW values + percentile ranks
  4. Build      — H3 + processed CSVs → index.html + lib.js + LightSignal.kmz

Each stage logs clearly. If any stage fails the pipeline stops
and tells you exactly which script to investigate.
"""

import sys
import shutil
import logging
import argparse
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Path setup ────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
ROOT       = SCRIPT_DIR.parent
sys.path.insert(0, str(ROOT / "scripts"))

from utils.config import (
    FILE_DC, FILE_ARTICLES, FILE_EIA, FILE_HEX,
    ARCHIVE_DIR,
    FILE_KMZ_LATEST,
    KMZ_OUTPUT_FILENAME,
    SHAREPOINT_SYNC_PATH,
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Input files to archive ────────────────────────────────────────────────────
INPUT_FILES = [
    FILE_DC,
    FILE_ARTICLES,
    FILE_EIA,
    FILE_HEX,
]


def banner(title: str):
    log.info("")
    log.info("═" * 55)
    log.info(f"  {title}")
    log.info("═" * 55)


def stage(num: int, title: str):
    log.info("")
    log.info(f"  ── Stage {num}: {title} " + "─" * max(1, 40 - len(title)))


def run_stage(label: str, func):
    """Runs a pipeline stage function. Exits cleanly on failure."""
    try:
        func()
    except SystemExit as e:
        log.error(f"  Pipeline stopped at stage: {label}")
        log.error(f"  Fix the issue above and re-run.")
        sys.exit(e.code)
    except Exception as e:
        log.error(f"  Unexpected error in stage: {label}")
        log.error(f"  {type(e).__name__}: {e}")
        log.error(traceback.format_exc())
        sys.exit(1)


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 1 — ARCHIVE INPUTS
# ═══════════════════════════════════════════════════════════════════════════════

def archive_inputs():
    """Saves timestamped copies of all input files to data/archive/inputs/."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

    archived = 0
    for src in INPUT_FILES:
        if src.exists():
            stem   = src.stem
            suffix = src.suffix
            dest   = ARCHIVE_DIR / f"{stem}_{timestamp}{suffix}"
            shutil.copy2(src, dest)
            log.info(f"    Archived: {src.name} → {dest.name}")
            archived += 1
        else:
            log.warning(f"    Input not found (skipped): {src.name}")

    log.info(f"    {archived}/{len(INPUT_FILES)} input files archived.")
    _prune_archives(ARCHIVE_DIR, retention_days=7)


def _prune_archives(archive_dir: Path, retention_days: int) -> None:
    """Delete archive files older than retention_days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    pruned = 0
    for f in archive_dir.glob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff.timestamp():
            f.unlink()
            pruned += 1
    if pruned:
        log.info(f"    Archive: pruned {pruned} file(s) older than {retention_days} days")


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 2 — TRANSFORM
# ═══════════════════════════════════════════════════════════════════════════════

def run_transforms():
    from transform.transform_dc            import transform_dc
    from transform.transform_power_plants  import transform_power_plants
    from transform.transform_articles      import transform_articles
    from transform.transform_build_cost    import transform_build_cost

    log.info("  Running transform_dc...")
    transform_dc()

    log.info("  Running transform_power_plants...")
    transform_power_plants()

    log.info("  Running transform_articles...")
    transform_articles()

    log.info("  Running transform_build_cost...")
    transform_build_cost()


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 3 — H3 GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def run_h3_generation():
    import importlib.util, sys as _sys

    # scripts/h3/ shadows the real h3 library when 'scripts' is on sys.path.
    # Temporarily remove 'scripts' from sys.path while loading h3 scripts so
    # that `import h3` resolves to the installed package, not our folder.
    scripts_path = str(ROOT / "scripts")
    path_backup  = [p for p in _sys.path if p == scripts_path]
    for p in path_backup:
        _sys.path.remove(p)

    # Remove any cached fake 'h3' module from previous import attempts
    _sys.modules.pop("h3", None)

    def load_h3_script(name):
        """Loads a script from scripts/h3/ by path to avoid collision with the h3 library."""
        path = ROOT / "scripts" / "h3" / f"{name}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        mod  = importlib.util.module_from_spec(spec)
        _sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    try:
        log.info("  Running generate_h3_dc...")
        load_h3_script("generate_h3_dc").generate_h3_dc()

        log.info("  Running generate_h3_pp...")
        load_h3_script("generate_h3_pp").generate_h3_pp()

        log.info("  Running generate_h3_build_cost...")
        load_h3_script("generate_h3_build_cost").generate_h3_build_cost()

        log.info("  Running generate_h3_composites...")
        load_h3_script("generate_h3_composites").generate_h3_composites()
    finally:
        # Restore scripts path so subsequent stages work normally
        for p in path_backup:
            _sys.path.insert(0, p)


# ═══════════════════════════════════════════════════════════════════════════════
# STAGE 4 — BUILD OUTPUTS
# ═══════════════════════════════════════════════════════════════════════════════

def _load_script(name, folder="kml"):
    import importlib.util, sys as _sys
    path = ROOT / "scripts" / folder / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    _sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

def run_newsletter():
    log.info("  Running generate_newsletter...")
    import importlib.util, sys as _sys
    path = ROOT / "scripts" / "generate_newsletter.py"
    spec = importlib.util.spec_from_file_location("generate_newsletter", path)
    mod  = importlib.util.module_from_spec(spec)
    _sys.modules["generate_newsletter"] = mod
    spec.loader.exec_module(mod)
    mod.generate_newsletter()

def run_kepler_build():
    log.info("  Running build_deckgl...")
    _load_script("build_deckgl").build_deckgl()

def run_dc_kmz_build():
    log.info("  Running build_kmz_dc...")
    _load_script("build_kmz_dc").build_kmz_dc()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="LightSignal — full pipeline runner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  Full run:
    python scripts/run_all.py

  Skip H3 regeneration (use existing h3_r*.csv — faster for marker-only changes):
    python scripts/run_all.py --skip-h3

  Rebuild marker KML only (skips transforms and H3):
    python scripts/run_all.py --markers-only

  Full run without archiving input files:
    python scripts/run_all.py --no-archive
        """
    )
    parser.add_argument(
        "--skip-h3",
        action="store_true",
        help="Skip H3 generation — use existing h3_r*.csv files",
    )
    parser.add_argument(
        "--markers-only",
        action="store_true",
        help="Rebuild marker KML only — skip transforms, H3, and h3 KML",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Skip archiving input files",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="After build, copy index.html + lib.js to SHAREPOINT_SYNC_PATH in config.py",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    start_time = datetime.now()

    banner("LightSignal Pipeline")
    log.info(f"  Started:  {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"  Mode:     {'markers-only' if args.markers_only else 'skip-h3' if args.skip_h3 else 'full run'}")
    log.info(f"  Archive:  {'no' if args.no_archive else 'yes'}")

    # ── Validate input files exist ────────────────────────────────────────────
    if not args.markers_only:
        log.info("")
        log.info("  Checking input files...")
        missing = [f for f in INPUT_FILES if not f.exists()]
        if missing:
            log.error("  Missing input files:")
            for f in missing:
                log.error(f"    {f}")
            log.error("")
            log.error("  Place all source files in data/raw/inputs/ and re-run.")
            log.error("  Expected files:")
            log.error("    dc_consolidated.csv")
            log.error("    news_feed.csv")
            log.error("    eia_generators_latest.xlsx")
            log.error("    hex_master_r6.csv")
            sys.exit(1)
        log.info("  All input files found.")

    # ── Stage 1: Archive ──────────────────────────────────────────────────────
    if not args.no_archive and not args.markers_only:
        stage(1, "Archive Inputs")
        run_stage("Archive", archive_inputs)
    else:
        log.info("  Stage 1: Archive — skipped")

    # ── Stage 2: Transform ────────────────────────────────────────────────────
    if not args.markers_only:
        stage(2, "Transform")
        run_stage("Transform", run_transforms)
    else:
        log.info("  Stage 2: Transform — skipped (--markers-only)")

    # ── Stage 3: H3 Generation ────────────────────────────────────────────────
    if not args.skip_h3 and not args.markers_only:
        stage(3, "H3 Generation")
        run_stage("H3 Generation", run_h3_generation)
    else:
        log.info("  Stage 3: H3 Generation — skipped")

    # ── Stage 4: Build Outputs ────────────────────────────────────────────────
    stage(4, "Build Outputs")
    run_stage("Newsletter",   run_newsletter)
    run_stage("Kepler Build", run_kepler_build)
    run_stage("DC KMZ",       run_dc_kmz_build)

    # ── Done ──────────────────────────────────────────────────────────────────
    elapsed = datetime.now() - start_time
    minutes = int(elapsed.total_seconds() // 60)
    seconds = int(elapsed.total_seconds() % 60)

    log.info("")
    log.info("═" * 55)
    log.info("  ✓  LightSignal pipeline complete!")
    log.info(f"  Time elapsed: {minutes}m {seconds}s")
    log.info(f"  Output: {FILE_KMZ_LATEST}")

    html_out = ROOT / "output" / "latest" / "LightSignal" / "index.html"
    if html_out.exists():
        size_mb = html_out.stat().st_size / 1_000_000
        log.info(f"  HTML:   {size_mb:.1f} MB")

    log.info("")
    log.info("  Open in Chrome or Firefox:")
    log.info(f"  {ROOT / 'output' / 'latest' / 'LightSignal' / 'index.html'}")
    log.info("")
    log.info("  For SharePoint/Power BI: host the LightSignal/ folder")
    log.info("  and reference index.html — both files required.")
    log.info("═" * 55)


if __name__ == "__main__":
    main()
