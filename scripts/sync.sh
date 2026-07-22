#!/usr/bin/env bash
#
# Sync the upstream HolmesGPT Helm chart into this repo.
#
# This script pulls the latest `helm/holmes` chart from a local clone of
# https://github.com/HolmesGPT/holmesgpt and copies it into ./helm/ in this
# repo, preserving the AKSInvestigator-specific Saturn add-ons under
# ./helm/templates/saturn/ (those files are listed in KEEP_FILES below and are
# restored after the rsync).
#
# Usage:
#   HOLMESGPT_DIR=/path/to/holmesgpt ./sync.sh
#
# Defaults to ${HOME}/llm/holmesgpt if HOLMESGPT_DIR is unset.

set -euo pipefail

HOLMESGPT_DIR="${HOLMESGPT_DIR:-${HOME}/llm/holmesgpt}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC_CHART="${HOLMESGPT_DIR}/helm/holmes"
DST_CHART="${REPO_ROOT}/helm"

# Files/directories under ./helm/ that are AKSInvestigator-specific and must be
# preserved across syncs. Paths are relative to ${DST_CHART}.
KEEP_FILES=(
  "templates/saturn"
  "values-saturn.yaml"
)

if [[ ! -d "${SRC_CHART}" ]]; then
  echo "error: upstream chart not found at ${SRC_CHART}" >&2
  echo "       set HOLMESGPT_DIR to your local holmesgpt clone" >&2
  exit 1
fi

echo "==> Syncing helm chart from ${SRC_CHART}"
echo "    into                  ${DST_CHART}"

# Stash the AKSInvestigator-specific files so rsync --delete doesn't drop them.
STASH="$(mktemp -d)"
trap 'rm -rf "${STASH}"' EXIT

for path in "${KEEP_FILES[@]}"; do
  abs="${DST_CHART}/${path}"
  if [[ -e "${abs}" ]]; then
    mkdir -p "${STASH}/$(dirname "${path}")"
    cp -a "${abs}" "${STASH}/${path}"
  fi
done

mkdir -p "${DST_CHART}"
rsync -a --delete "${SRC_CHART}/" "${DST_CHART}/"

# Restore the AKSInvestigator-specific files.
for path in "${KEEP_FILES[@]}"; do
  if [[ -e "${STASH}/${path}" ]]; then
    mkdir -p "${DST_CHART}/$(dirname "${path}")"
    cp -a "${STASH}/${path}" "${DST_CHART}/${path}"
  fi
done

echo "==> Done. Review the diff with: git -C ${REPO_ROOT} status"
