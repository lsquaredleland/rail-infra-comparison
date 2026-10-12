#!/bin/bash
# Rebuild the map data from an OSM planet file: extraction (Rust), region build (Python, parallel per group), station model.
#
#   pipeline/refresh.sh                     # everything: all groups in pipeline/groups.json, all regions, station files
#   pipeline/refresh.sh prd hk,sz           # one-off: extract only group prd, build only regions hk and sz
#
# Environment:
#   RA_PLANET   planet file            (default ../osm-rail/planet-260928.osm.pbf, next to this repository)
#   PYTHON      Python with shapely, pyproj, numpy (default python3)
#   RA_STATIONS regions that get a station model (default "hk sz"; any built region id works)
#   RA_SLOTS    parallel build budget  (default 4; a group with a big region takes 2)
#
# Monthly refresh: download the new planet, point RA_PLANET at it, run this script, then check the site (tests/smoke.py).
# New city: add its group to pipeline/groups.json and its region to REGIONS in pipeline/build.py, then run
#   pipeline/refresh.sh <group> <region>
set -euo pipefail
cd "$(dirname "$0")/.."
PLANET="${RA_PLANET:-../osm-rail/planet-260928.osm.pbf}"
PY="${PYTHON:-python3}"
EXTRACT_GROUPS="${1:-}"   # (not GROUPS: that is a reserved bash variable)
REGIONS="${2:-}"
STATIONS="${RA_STATIONS:-hk sz}"

t0=$(date +%s)
step() { echo "[$(( $(date +%s) - t0 ))s] $*"; }

step "building the extractor"
(cd extract && cargo build --release --quiet)

step "extracting ${EXTRACT_GROUPS:-all groups} from $PLANET"
extract/target/release/ra-extract --planet "$PLANET" --config pipeline/groups.json --out run2 ${EXTRACT_GROUPS:+--groups "$EXTRACT_GROUPS"}

step "building ${REGIONS:-all regions}"
(cd pipeline && "$PY" run_all.py $REGIONS)

for r in $STATIONS; do
  if [ -z "$REGIONS" ] || [[ ",$REGIONS," == *",$r,"* ]]; then
    step "station model for $r"
    (cd pipeline && "$PY" stations.py "$r" | head -1)
  fi
done
step "done"
