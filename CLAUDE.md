# AKSInvestigator

AKS-flavored fork of the [HolmesGPT](https://github.com/HolmesGPT/holmesgpt)
Helm chart. This repo is **Helm chart only** — there is no application source
code here. HolmesGPT itself lives upstream; this repo packages and deploys it
for AKS with AKS-specific add-ons layered on top.

## Core model: sync + overlay

`helm/` is synced from the upstream `holmesgpt/helm/holmes` chart. On top of
that, AKSInvestigator-only files are preserved and a small integration patch is
applied to upstream templates. Understanding this split is essential before
editing anything:

- **Upstream-tracked (do NOT hand-patch):** everything under `helm/` that comes
  from upstream, including the base content of `helm/values.yaml`. The sync script
  pins the HolmesGPT image and registry, while the integration patch adds Saturn
  defaults to `values.yaml`. Otherwise changes here should come from an upstream sync.
- **AKSInvestigator-only (safe to edit, preserved by `sync.sh`):**
  - `helm/templates/saturn/` — the Saturn vLLM add-on
  - `patches/saturn-integration.patch` — Saturn defaults, model-list, checksum,
    and Holmes environment changes applied to the upstream chart

`scripts/sync.sh` stages the upstream chart, restores the files listed in
`KEEP_FILES`, and applies `patches/saturn-integration.patch`. The destination is
updated only if the patch applies cleanly. If a new upstream release conflicts,
port the Saturn hunks to the new templates and refresh the patch; never add an
upstream template to `KEEP_FILES`, because that would mask future upstream
changes. Add only new AKSInvestigator-owned files to `KEEP_FILES`.

```bash
# Defaults to ~/llm/holmesgpt
./scripts/sync.sh
HOLMESGPT_DIR=/path/to/holmesgpt ./scripts/sync.sh
# then review upstream changes with: git status / git diff
```

## The Saturn add-on

`helm/templates/saturn/saturn-vllm.yaml` deploys an in-cluster vLLM serving
`saturn-ms/saturn-0202` with an OpenAI-compatible API at `/v1` on port 8000, so
HolmesGPT has a model to talk to out of the box. Rendering rules (all must hold):

- `.Values.saturn.enabled` is true (default in `values.yaml`), AND
- `.Values.modelList` is **empty** — as soon as `modelList` is non-empty, Saturn
  is fully suppressed and HolmesGPT uses the external models instead.

CPU vs GPU is selected by `saturn.gpu` (default `false`):
- `saturn.gpu=false` — CPU vLLM, `--dtype=float32`,
  `maxModelLen=65536`. Requires an **AVX-512 Intel** node pool (Standard_D*s_v5);
  the prebuilt CPU image SIGILLs on AMD EPYC (AVX2-only).
- `saturn.gpu=true` — GPU vLLM, `--dtype=auto`, `maxModelLen=262144`, requests
  `nvidia.com/gpu: 1`. Set `saturn.nodeSelector` to land on the GPU pool.

### Mirroring the vLLM images to ACR

`.github/workflows/mirror-vllm-images.yaml` is a **standalone** pipeline
(independent of local chart syncing and `release.yaml`) that mirrors the
upstream vLLM images Saturn uses — `vllm/vllm-openai` (GPU) and
`vllm/vllm-openai-cpu` (CPU) — from Docker Hub into the AKS ACR as
`saturn/vllm-openai:<tag>` and `saturn/vllm-openai-cpu:<tag>`, via server-side
`az acr import`. It runs weekly on a schedule and on manual dispatch (with an
optional `tag` input). It mirrors the floor tag `FLOOR_TAG` (the version pinned
in `values.yaml`, currently `v0.28.0`) plus any newer `vX.Y.Z` release,
and is idempotent (skips tags already in ACR).

It **never edits the chart** — bumping the vLLM version Saturn actually runs
requires updating `gpuImage.tag`/`cpuImage.tag` in the integration patch and
regenerating `values.yaml` with `scripts/sync.sh`. Also bump `FLOOR_TAG` in the
workflow.

## Layout

```
AKSInvestigator/
├── helm/                      # synced from holmesgpt/helm/holmes
│   ├── Chart.yaml             # name: holmes; version placeholder 0.0.1
│   ├── values.yaml            # synced upstream values; image tag pinned by sync
│   ├── crds/                  # healthcheck, scheduledhealthcheck, triggeredhealthcheck
│   └── templates/
│       ├── holmes*.yaml, operator-*.yaml, toolset-config.yaml, ...  # upstream
│       ├── mcp-servers/       # per-provider MCP server deploy/netpol/rbac
│       │   ├── kubernetes-remediation/  # has rbac.yaml (remediation perms)
│       │   ├── github/, aws/, azure/, gcp/, kubernetes/, sentry/, ...
│       └── saturn/            # AKS-specific add-on (preserved by sync.sh)
│           └── saturn-vllm.yaml
└── scripts/sync.sh             # pull latest chart from upstream (KEEP_FILES)
```

## Install

```bash
# CPU (default) — needs AVX-512 (Standard_D*s_v5)
helm install aksi ./helm -n aks-investigator --create-namespace

# GPU — requires an NVIDIA node pool
helm install aksi ./helm -n aks-investigator --create-namespace \
  --set saturn.gpu=true

# Bring your own model (a non-empty modelList suppresses Saturn)
helm install aksi ./helm -n aks-investigator --create-namespace \
  --set-json 'modelList={"gpt-5":{"model":"openai/gpt-5","api_key":"sk-..."}}'
```

When Saturn is active, the chart automatically adds `openai/saturn-vllm` and
its in-cluster endpoint to HolmesGPT's model list. The generated
`max_context_size` matches the selected CPU/GPU serving limit.

## Working conventions

- **Prefer editing the upstream chart upstream**, then `./scripts/sync.sh`.
  Change AKS-specific Saturn defaults in `patches/saturn-integration.patch`, not
  directly in `values.yaml`, because sync regenerates that file.
- New AKS-only files must be added to `KEEP_FILES` in `scripts/sync.sh`.
- Validate template changes with `helm template ./helm`
  and `helm lint ./helm` before committing.
- Commits use semantic PR title format: `<type>(<scope>): <description>`
  (e.g. `feat(helm): ...`).
</content>
</invoke>
