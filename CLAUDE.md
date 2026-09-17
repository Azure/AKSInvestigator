# AKSInvestigator

AKS-flavored fork of the [HolmesGPT](https://github.com/HolmesGPT/holmesgpt)
Helm chart. This repo contains **Helm/deployment and model packaging**, not the
HolmesGPT runtime application source. HolmesGPT itself lives upstream; this
repo packages and deploys it for AKS with AKS-specific add-ons layered on top.
The [model-image Dockerfile](docker/saturn-model/Dockerfile) is build packaging
for model files and copy tools, not a new runtime application.

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
- `saturn.gpu=false` — CPU vLLM, `--dtype=auto`,
  `maxModelLen=65536`. Requires an **AVX-512 Intel** node pool (Standard_D*s_v5);
  the prebuilt CPU image SIGILLs on AMD EPYC (AVX2-only).
- `saturn.gpu=true` — GPU vLLM, `--dtype=auto`, `maxModelLen=262144`, requests
  `nvidia.com/gpu: 1`. Set `saturn.nodeSelector` to land on the GPU pool.

The current Saturn model declares BF16, which `auto` selects on supported
backends. BF16 weights are ~8.2 GiB, and one 65536-token sequence needs ~9 GiB
of BF16 KV data. The 20 GiB CPU KV pool and 38Gi/48Gi memory request/limit are
retained budgets, not measured BF16 usage; re-measure before reducing them.

### Model delivery and publication

- `saturn.modelSource=oci` is an explicit opt-in:
  the ORAS init container pulls `modelArtifact` from
  `mcr.microsoft.com/microsoft.aks.investigator/saturn:v1-20260202`, using
  `modelPullImage` (`mcr.microsoft.com/oss/v2/oras-project/oras:v1.2.3`).
- `saturn.modelSource=image` is the default (including the template fallback).
  Existing releases with saved `modelSource: oci` need an explicit image override
  to switch delivery mode. Image mode uses `modelImage.repository` =
  `mcr.microsoft.com/microsoft.aks.investigator/saturn` and
  `modelImage.tag` = `0.1.0` (same model as artifact `v1-20260202`).
  Kubelet/the container runtime pulls the image
  (`IfNotPresent`); root-level `imagePullSecrets` now also applies to Saturn.
  Users must create private-registry credentials in the release namespace;
  these secrets do not authenticate ORAS artifact pulls.
- The `copy-model` init container invokes the image entrypoint with one literal
  destination argument, `saturn.modelPath` (default `/models/saturn-0202`). It
  copies `/model` into the shared disk-backed `emptyDir` at `/models`. Custom
  images must honor that contract with a read-only root filesystem and keep
  the destination under `/models`; the chart does not override the entrypoint.
  The built image contains Azure Linux 3.0 core, model files and `sh`/`cp`, not
  vLLM, Python or ORAS. Image mode has no in-pod ORAS or HF model download.
- One `linux/amd64` model image serves both current x86 CPU and NVIDIA GPU
  pools; CPU/GPU vLLM images, scheduling requirements and RAM/KV budgets stay
  unchanged. This is not zero-copy or network-free: nodes first download/cache
  image layers, then init copies the files on every pod recreation. Losing the
  `emptyDir` requires another copy; a retained node image cache helps avoid
  re-downloading. Budget ~16.4 GiB (~17.6 GB) for two ~8.2 GiB model disk copies
  (image + volume), plus runtime images and overhead. The model mount is not
  tmpfs.

The same **Publish Saturn model to MCR** workflow
([.github/workflows/publish-saturn-model.yaml](.github/workflows/publish-saturn-model.yaml))
resolves the HF revision once, uses AIKit to download/package one local
ModelPack OCI layout, and extracts its local files with `oras pull --oci-layout`
without another HF/registry model download. It publishes the existing OCI
artifact and builds/pushes the model-only image using
[docker/saturn-model/Dockerfile](docker/saturn-model/Dockerfile). Separate
workflow inputs select the artifact `tag` (default `v1-20260202`) and
`image_tag` (default `0.1.0`). In the shared repository, artifact SHA tags use
`sha-<first-12-commit-characters>` and image SHA tags use
`image-sha-<first-12-commit-characters>` so the manifests cannot overwrite each
other. Release tags must differ; opposite-format SHA prefixes are rejected:

| Output | Source ACR | Expected MCR |
| --- | --- | --- |
| ModelPack | `<acr>.azurecr.io/public/microsoft.aks.investigator/saturn:<tag>` | `mcr.microsoft.com/microsoft.aks.investigator/saturn:<tag>` |
| Model image | `<acr>.azurecr.io/public/microsoft.aks.investigator/saturn:<image_tag>` | `mcr.microsoft.com/microsoft.aks.investigator/saturn:<image_tag>` |

Image `0.1.0` maps to artifact `v1-20260202`; both are built from the same
resolved HF revision but use distinct tags in the same repository.
MCR policy must allow both formats and propagation must complete; the workflow verifies
ACR, not MCR. Verify the target image before deploying with the image default; do not treat
configured defaults as evidence of publication or a passed end-to-end deployment.
See [README.md](README.md) for defaults and image-mode/private-ACR install examples.

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
