# AKSInvestigator

AKS-flavored fork of the [HolmesGPT](https://github.com/HolmesGPT/holmesgpt)
Helm chart. This repo contains **Helm/deployment, model packaging, and a native
kubectl CLI wrapper**, not the HolmesGPT runtime application source. HolmesGPT
itself lives upstream; this repo packages and deploys it for AKS with
AKS-specific add-ons layered on top. The
[kubectl plugin](kubectl-plugin/README.md) invokes Holmes inside the cluster;
it is not a local HolmesGPT runtime.
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
HolmesGPT has a model to talk to out of the box. `.Values.saturn.enabled` is the
only activation switch (default true), independent of `.Values.modelList`.
When enabled, the generated Saturn entry is added to a deep copy of the user's
catalog without mutating Helm values. A key matching `saturn.servedModelName`
(default `saturn-vllm`) fails rendering instead of overwriting a user model.
Set `saturn.enabled=false` for external-model-only installs. This changes the
old non-empty-modelList suppression behavior: upgrades of external-only releases
must explicitly disable Saturn to avoid unexpectedly reserving its resources.
Keep the activation condition consistent
in the Saturn template and the integration patch's Holmes/model-config hunks.
CPU/GPU context limits and the `envRef:` secret substitution still apply.
The Holmes `OVERRIDE_MAX_OUTPUT_TOKEN=20000` remains process-wide when Saturn
is active, including coexistence mode. See the README for Azure OpenAI setup.

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

## Native kubectl plugin

Linux, macOS, and Windows share [kubectl-plugin/go.mod](kubectl-plugin/go.mod),
[kubectl-plugin/main.go](kubectl-plugin/main.go),
[kubectl-plugin/exec_unix.go](kubectl-plugin/exec_unix.go),
and [kubectl-plugin/exec_windows.go](kubectl-plugin/exec_windows.go). The old Linux/macOS Bash
script and separate Windows module have been replaced. Build from the shared
module, not a Windows subdirectory. `golang.org/x/term v0.34.0` and indirect
`golang.org/x/sys v0.35.0` require Go 1.23 or newer; CI uses **1.24.x**.
The native plugin needs no WSL, local Python, or local Bash. Only timing mode
requires Bash inside the Holmes container.

The reusable [binary workflow](.github/workflows/plugin-binaries.yaml) is
called by the existing [E2E PR check-in](.github/workflows/e2e-test.yaml).
It checks compiled executables on hosted `ubuntu-24.04` (`linux/amd64`),
`windows-2022` (`windows/amd64`), and `macos-15` (native architecture from
`go env`, typically `darwin/arm64`), without Azure secrets. Fork/Dependabot PRs
run these checks but skip Azure jobs. The
[check driver](kubectl-plugin/tools/check-binary/main.go) uses a recording
kubectl fixture plus real kubectl discovery/dispatch without cluster/network
access or secrets; Windows also has a PowerShell smoke check. No unit-test
suite was added.

CPU/GPU live E2E checksum-verifies and uses the exact tested Linux artifact.
The [release workflow](.github/workflows/release.yaml) supports only unprefixed
`X.Y.Z` tags, allowing SemVer prereleases but rejecting `v` prefixes and
`+buildmetadata`. GitHub, chart, and OCI versions use that same release version.
The sequence is validated tag → native checks → CPU/GPU E2E → chart source ACR
publication → raw plugin source ACR publication/verification → GitHub assets.

Native build artifacts contain only the tested executable and internal
**SHA256SUMS**, not a license. There is no separate packaging-check job.
Publication checksum-verifies the downloaded native artifacts and renames the
same tested binaries **without rebuilding**. Exactly three native artifacts,
one per OS, are required:
`linux/amd64`, `windows/amd64`, and the macOS runner's actual `go env` target,
usually `darwin/arm64` (`darwin/amd64` is accepted for runner changes). Do not
claim that all six manual cross-build targets are released.

Plugin publication uses the same `AZURE_CONTAINER_REGISTRY` secret and
managed-identity self-hosted `1ES.Pool=1es-aks-investigator-pool-ubuntu` runner
as chart publication, with registry listing/push/pull permissions:

- Source: `<acr>.azurecr.io/public/microsoft.aks.investigator/kubectl-investigate:<version>-<goos>-<goarch>`.
- Expected MCR: `mcr.microsoft.com/microsoft.aks.investigator/kubectl-investigate:<version>-<goos>-<goarch>`.
- Artifact type: `application/vnd.microsoft.aks.investigator.kubectl-plugin.v1`.
  Direct `oras push --registry-config` publishes a single raw binary layer
  (`application/octet-stream`), not a runnable container image. No archive,
  checksum, license, or inventory payload is published. **SHA256SUMS** remains
  internal to Actions handoffs for E2E and release publication.
- Existing source tags are reused only when the artifact type, single-layer
  binary digest, and filename match; different content fails. The preflight
  check and post-publish verification are **not a global lock** or
  registry-enforced immutability. Concurrency is keyed by the release tag and
  does not lock out external publishers. No `latest` registry tag is published.
- The workflow pulls source ACR artifacts by manifest digest and compares
  the served binary bytes with the tested executables, then
  creates a missing GitHub release with `--verify-tag` and `--generate-notes`,
  marking newly created prereleases. It uploads only the same versioned raw
  **kubectl-investigate-\<version\>-\<goos\>-\<goarch\>** binaries (with **.exe**
  on Windows), replacing same-named GitHub assets on reruns (`--clobber`).

For example, source ACR
`<acr>.azurecr.io/public/microsoft.aks.investigator/kubectl-investigate:0.1.0-linux-amd64`
contains **kubectl-investigate-0.1.0-linux-amd64**, also the GitHub asset name.
The Windows tag `0.1.0-windows-amd64` contains
**kubectl-investigate-0.1.0-windows-amd64.exe**; `.exe` is not part of the tag.
Installation requires no extraction: rename to **kubectl-investigate** or
**kubectl-investigate.exe**, put it on `PATH`, and use `chmod +x` on Linux/macOS.
ORAS pulls may not preserve executable permission.

MCR requires repository onboarding, compatible ingestion policy, and completed
propagation; the workflow does **not** verify MCR. Source publication code is
not evidence of a successful release. Verify actual assets before recommending
a download; plugin version `0.1.0` in examples is illustrative only. See the
[plugin installation guide](kubectl-plugin/README.md) for source builds,
registry authentication/pull permissions, raw binary installation, and
manual cross-builds, including Windows ARM64.

The [benchmark collector](scripts/performance_benchmark.py) remains Linux/macOS
only. It requires Go on `PATH`, builds this checkout's plugin once per benchmark
invocation before cluster access/timing, puts its private output binary's
directory first on `PATH`, and records `plugin_binary`, `plugin_sha256`, and a build log.
Dependencies may need downloading. The local Braintrust SDK remains mandatory,
including with `--no-braintrust`; see [CPU performance](docs/cpu-performance.md).

## Layout

```
AKSInvestigator/
├── kubectl-plugin/            # shared native CLI wrapper, not Holmes runtime
│   ├── go.mod
│   ├── main.go
│   ├── exec_unix.go
│   ├── exec_windows.go
│   └── README.md
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
helm install aks-investigator ./helm -n aks-investigator --create-namespace

# GPU — requires an NVIDIA node pool
helm install aks-investigator ./helm -n aks-investigator --create-namespace \
  --set saturn.gpu=true

# Bring your own model only (disable Saturn explicitly)
helm install aks-investigator ./helm -n aks-investigator --create-namespace \
  --set saturn.enabled=false \
  --set-json 'modelList={"gpt-5":{"model":"openai/gpt-5","api_key":"sk-..."}}'
```

When Saturn is active, the chart automatically adds `openai/saturn-vllm` and
its in-cluster endpoint to HolmesGPT's model list, preserving user models when
Saturn is enabled. The generated
`max_context_size` matches the selected CPU/GPU serving limit.

## Working conventions

- **Prefer editing the upstream chart upstream**, then `./scripts/sync.sh`.
  Change AKS-specific Saturn defaults in `patches/saturn-integration.patch`, not
  directly in `values.yaml`, because sync regenerates that file.
- New AKS-only chart files must be added to `KEEP_FILES` in `scripts/sync.sh`;
  the native CLI sources are outside chart syncing.
- Validate template changes with `helm template ./helm`
  and `helm lint ./helm` before committing.
- Commits use semantic PR title format: `<type>(<scope>): <description>`
  (e.g. `feat(helm): ...`).
</content>
</invoke>
