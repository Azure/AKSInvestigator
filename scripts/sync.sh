#!/usr/bin/env bash
#
# Sync the upstream HolmesGPT Helm chart into this repo.
#
# This script pulls the latest `helm/holmes` chart from a local clone of
# https://github.com/HolmesGPT/holmesgpt and copies it into ./helm/ in this
# repo, preserving AKSInvestigator-only Saturn files and applying the tracked
# Saturn integration patch to upstream templates in a staging directory.
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
SATURN_PATCH="${REPO_ROOT}/patches/saturn-integration.patch"

# Files/directories under ./helm/ that contain AKSInvestigator-specific behavior
# and must be preserved across syncs. Paths are relative to ${DST_CHART}.
KEEP_FILES=(
  "templates/saturn"
  "values-saturn.yaml"
)

if [[ ! -d "${SRC_CHART}" ]]; then
  echo "error: upstream chart not found at ${SRC_CHART}" >&2
  echo "       set HOLMESGPT_DIR to your local holmesgpt clone" >&2
  exit 1
fi

if [[ ! -f "${SATURN_PATCH}" ]]; then
  echo "error: Saturn integration patch not found at ${SATURN_PATCH}" >&2
  exit 1
fi

echo "==> Syncing helm chart from ${SRC_CHART}"
echo "    into                  ${DST_CHART}"

# Build the complete chart in a temporary staging area. If the Saturn patch no
# longer applies to a new upstream version, fail before modifying ./helm.
STASH="$(mktemp -d)"
trap 'rm -rf "${STASH}"' EXIT

STAGED_CHART="${STASH}/helm"
mkdir -p "${STAGED_CHART}"
rsync -a --delete "${SRC_CHART}/" "${STAGED_CHART}/"

for path in "${KEEP_FILES[@]}"; do
  abs="${DST_CHART}/${path}"
  if [[ -e "${abs}" ]]; then
    mkdir -p "${STAGED_CHART}/$(dirname "${path}")"
    cp -a "${abs}" "${STAGED_CHART}/${path}"
  fi
done

if ! git -C "${STASH}" apply --check "${SATURN_PATCH}"; then
  echo "error: Saturn integration patch does not apply to this upstream chart" >&2
  echo "       update patches/saturn-integration.patch for the new release" >&2
  exit 1
fi
git -C "${STASH}" apply "${SATURN_PATCH}"

mkdir -p "${DST_CHART}"
rsync -a --delete "${STAGED_CHART}/" "${DST_CHART}/"

# Record which upstream commit we synced. Written at the repo root (outside
# ${DST_CHART}) so rsync --delete never touches it.
UPSTREAM_REF="$(git -C "${HOLMESGPT_DIR}" describe --tags --always --dirty 2>/dev/null || echo unknown)"
UPSTREAM_SHA="$(git -C "${HOLMESGPT_DIR}" rev-parse HEAD 2>/dev/null || echo unknown)"
UPSTREAM_DATE="$(git -C "${HOLMESGPT_DIR}" show -s --format=%cs HEAD 2>/dev/null || echo unknown)"

if [[ "${UPSTREAM_REF}" == *-dirty ]]; then
  echo "warning: upstream clone at ${HOLMESGPT_DIR} has uncommitted changes;" >&2
  echo "         recorded version may not be reproducible." >&2
fi

# Pin the chart's HolmesGPT image tag to the upstream version we just synced and
# point it at the AKS Investigator MCR namespace. Upstream ships a `holmes:0.0.0`
# placeholder under `robustadev`; these deliberate fork defaults are re-applied
# on every sync so values.yaml never has to be hand-patched.
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

  if grep -qE '^registry:[[:space:]]*robustadev[[:space:]]*$' "${VALUES}"; then
    sed -i -E '0,/^registry:[[:space:]]*robustadev[[:space:]]*$/s||registry: mcr.microsoft.com/microsoft.aks.investigator|' "${VALUES}"
    echo "==> Set chart registry to mcr.microsoft.com/microsoft.aks.investigator in values.yaml"
  else
    echo "warning: could not find 'registry: robustadev' in ${VALUES};" >&2
    echo "         registry left unchanged." >&2
  fi
fi

# Rebrand the chart name from upstream's `holmes` to `aks-investigator-chart`. Chart.yaml
# is synced verbatim from upstream, so this rename must be re-applied on every sync
# (idempotent) rather than hand-patched, or the next sync would revert it.
CHART="${DST_CHART}/Chart.yaml"
if [[ -f "${CHART}" ]] && grep -qE '^name:[[:space:]]*holmes[[:space:]]*$' "${CHART}"; then
  sed -i -E 's|^name:[[:space:]]*holmes[[:space:]]*$|name: aks-investigator-chart|' "${CHART}"
  echo "==> Renamed chart to aks-investigator-chart in Chart.yaml"
fi

cat > "${REPO_ROOT}/UPSTREAM_VERSION" <<EOF
# Upstream HolmesGPT chart version synced by scripts/sync.sh. Do not edit by hand.
ref:  ${UPSTREAM_REF}
sha:  ${UPSTREAM_SHA}
date: ${UPSTREAM_DATE}
EOF

echo "==> Recorded upstream version ${UPSTREAM_REF} in UPSTREAM_VERSION"
echo "==> Done. Review the diff with: git -C ${REPO_ROOT} status"
