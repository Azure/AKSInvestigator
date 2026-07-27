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

# Record which upstream commit we synced. Written at the repo root (outside
# ${DST_CHART}) so rsync --delete never touches it.
UPSTREAM_REF="$(git -C "${HOLMESGPT_DIR}" describe --tags --always --dirty 2>/dev/null || echo unknown)"
UPSTREAM_SHA="$(git -C "${HOLMESGPT_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)"
UPSTREAM_DATE="$(git -C "${HOLMESGPT_DIR}" show -s --format=%cs HEAD 2>/dev/null || echo unknown)"

if [[ "${UPSTREAM_REF}" == *-dirty ]]; then
  echo "warning: upstream clone at ${HOLMESGPT_DIR} has uncommitted changes;" >&2
  echo "         recorded version may not be reproducible." >&2
fi

# Pin the chart's HolmesGPT image tag to the upstream version we just synced, so
# the AKSInvestigator chart deploys the same HolmesGPT version as the original
# upstream chart. Upstream ships a `holmes:0.0.0` placeholder that it only fills
# in at its own release time; here we fill it in with the synced ref. This is a
# deliberate fork behavior re-applied on every sync, so values.yaml never has to
# be hand-patched.
VALUES="${DST_CHART}/values.yaml"
if [[ "${UPSTREAM_REF}" != "unknown" && -f "${VALUES}" ]]; then
  # Strip a leading `v` so the tag is a clean SemVer (e.g. v0.36.0 -> 0.36.0).
  IMAGE_TAG="${UPSTREAM_REF#v}"
  if grep -qE '^image:[[:space:]]*holmes:' "${VALUES}"; then
    sed -i -E "s|^(image:[[:space:]]*holmes:).*|\1${IMAGE_TAG}|" "${VALUES}"
    echo "==> Pinned chart image tag to holmes:${IMAGE_TAG} in values.yaml"
  else
    echo "warning: could not find 'image: holmes:<tag>' in ${VALUES};" >&2
    echo "         image tag left unchanged." >&2
  fi
fi

cat > "${REPO_ROOT}/UPSTREAM_VERSION" <<EOF
# Upstream HolmesGPT chart version synced by scripts/sync.sh. Do not edit by hand.
ref:  ${UPSTREAM_REF}
sha:  ${UPSTREAM_SHA}
date: ${UPSTREAM_DATE}
EOF

echo "==> Recorded upstream version ${UPSTREAM_REF} in UPSTREAM_VERSION"
echo "==> Done. Review the diff with: git -C ${REPO_ROOT} status"
