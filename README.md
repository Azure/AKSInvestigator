# AKSInvestigator

AKS Investigator is an AI-powered troubleshooting agent for AKS/Kubernetes
clusters: it deploys [HolmesGPT](https://github.com/HolmesGPT/holmesgpt) — which
investigates incidents, analyzes cluster state, and surfaces root causes — into
your cluster, bundled with an in-cluster LLM so it works out of the box with no
external model or API key required. It packages the upstream HolmesGPT Helm
chart (tracked verbatim) and layers an AKS-specific Saturn vLLM add-on on top,
so you can `helm install` and start diagnosing immediately, or bring your own
model when you'd rather use an external provider.

After installation, the bundled [`kubectl investigate`](kubectl-plugin/README.md)
plugin provides a native Linux/macOS/Windows CLI wrapper for asking the
in-cluster agent questions. It needs no WSL, local Python, or local Bash;
HolmesGPT still runs inside the cluster.

## What's added vs upstream

- **In-cluster Saturn vLLM model** ([template](helm/templates/saturn/saturn-vllm.yaml),
  defaults in [helm/values.yaml](helm/values.yaml)).
  When you `helm install` and `modelList` is empty, an init container prepares
  `saturn-ms/saturn-0202` in pod-local storage: by default it copies files from
  the model-only container image; optional OCI mode pulls the ModelPack
  artifact with ORAS. vLLM then serves that local model through an
  OpenAI-compatible API at `/v1`. The chart automatically registers
  it in HolmesGPT's model list. Toggle the backend with:
  - `saturn.gpu=false` (default) — CPU build of vLLM,
    `--dtype=auto`, `--max-model-len=65536`. Requires an AVX-512 Intel
    node pool (the prebuilt CPU image SIGILLs on AMD EPYC).
  - `saturn.gpu=true` — GPU build, `--dtype=auto`, `--max-model-len=262144`,
    requests `nvidia.com/gpu: 1`. Set `saturn.nodeSelector` to land on
    your GPU pool.

  Both variants let vLLM resolve `auto` from the model configuration and
  supported platform dtypes; it does not benchmark for the fastest precision.
  The current Saturn model declares `bfloat16`, so supported CPU/GPU backends
  select BF16 (~8.2 GiB of weights, versus ~16.4 GiB with FP32). For one
  65,536-token sequence, KV data needs ~9 GiB at BF16 (~18 GiB at FP32).
  CPU defaults retain a 20 GiB KV-cache budget and the previous 38 GiB memory
  request / 48 GiB limit. These are conservative budgets, not measured BF16
  usage; `auto` does not shrink the cache allocation or Kubernetes resources.

  As soon as `modelList` is non-empty, the Saturn deployment is fully
  suppressed (HolmesGPT uses your external models instead). Set
  `saturn.enabled=false` to disable Saturn without configuring another model.

  See the original standalone manifests this template is derived from at
  `~/llm/holmesgpt/deploy/models/` for the rationale behind each flag.

## Layout

```
AKSInvestigator/
├── kubectl-plugin/             # shared native kubectl investigate client
│   ├── go.mod
│   ├── main.go
│   ├── exec_unix.go
│   ├── exec_windows.go
│   └── README.md
├── helm/                       # synced from holmesgpt/helm/holmes
│   ├── Chart.yaml
│   ├── values.yaml             # synced upstream values; image tag pinned by sync
│   ├── crds/
│   └── templates/
│       ├── ... upstream templates ...
│       └── saturn/             # AKS-specific add-ons (preserved by sync.sh)
│           └── saturn-vllm.yaml
├── patches/
│   └── saturn-integration.patch # Saturn changes applied to upstream templates
└── scripts/sync.sh             # pull latest chart from upstream
```

## Sync from upstream

Follow [Sync HolmesGPT from upstream](docs/sync-holmesgpt.md) to import a
HolmesGPT release locally, validate the generated chart, and push the sync
branch.

## Install

Saturn uses the model image by default. **Verify that
`mcr.microsoft.com/microsoft.aks.investigator/saturn:0.1.0` is available** before
installing (see [publication details](#publish-the-saturn-model)).

```bash
# CPU (default) — installs anywhere with AVX-512 (Standard_D*s_v5)
helm install aks-investigator ./helm -n aks-investigator --create-namespace

# GPU — requires an NVIDIA-equipped node pool
helm install aks-investigator ./helm -n aks-investigator --create-namespace \
  --set saturn.gpu=true

# Bring your own model (a non-empty modelList suppresses Saturn)
helm install aks-investigator ./helm -n aks-investigator --create-namespace \
  --set-json 'modelList={"gpt-5":{"model":"openai/gpt-5","api_key":"sk-..."}}'
```

### Model source options

The default `saturn.modelSource=image` uses the model-only image without ORAS
in the pod. To use the OCI artifact instead, explicitly select `oci`. When
upgrading an existing release with saved `modelSource: oci`, explicitly set
`saturn.modelSource=image` to switch it to image delivery.

```bash
# Optional: download the ModelPack OCI artifact with ORAS
helm install aks-investigator ./helm -n aks-investigator --create-namespace \
  --set saturn.modelSource=oci

# Optional: use the model image directly from a private source ACR
helm install aks-investigator ./helm -n aks-investigator --create-namespace \
  --set 'saturn.modelImage.repository=<acr>.azurecr.io/public/microsoft.aks.investigator/saturn' \
  --set 'imagePullSecrets[0].name=acr-pull'
```

Replace `<acr>` with your registry name. Create the registry pull secret
yourself in the
`aks-investigator` namespace before installation; the chart does not create
credentials. Root-level `imagePullSecrets` now also applies to the Saturn pod
for kubelet/container-runtime image pulls, not ORAS artifact authentication.
Set `saturn.nodeSelector` to your existing compatible pool as needed; GPU mode
defaults to `agentpool: gpu`, while CPU mode has no default selector. Image mode
does not create node pools or change the CPU/GPU vLLM images.

## Install the kubectl plugin

On Windows PowerShell, use the native `kubectl-investigate.exe`; see the
[Windows installation instructions](kubectl-plugin/README.md#windows--powershell).
For Linux/macOS, build from the repository root with **Go 1.23 or newer**
(CI uses **1.24.x**). The shared module's `golang.org/x/term v0.34.0` and indirect
`golang.org/x/sys` dependency require Go 1.23; a first build may download them.

```bash
go -C kubectl-plugin build -mod=readonly -trimpath -o bin/host/kubectl-investigate .
mkdir -p ~/.local/bin
install -m 0755 kubectl-plugin/bin/host/kubectl-investigate ~/.local/bin/kubectl-investigate
export PATH="$HOME/.local/bin:$PATH"
kubectl plugin list | grep investigate
kubectl investigate "are any workloads unhealthy?"
```

Keep the installation directory on `PATH` in future terminals. Alternatively,
check the selected [GitHub Release](https://github.com/Azure/AKSInvestigator/releases)
for **kubectl-investigate-\<version\>-\<goos\>-\<goarch\>** (with **.exe** on
Windows). Download the raw executable, rename it to **kubectl-investigate**
(Linux/macOS) or **kubectl-investigate.exe** (Windows), and place it on `PATH`;
on Linux/macOS, set executable permission with `chmod +x`. See the
[release binary instructions](kubectl-plugin/README.md#release-binaries-when-published).
No extraction is needed, and no checksum or metadata files are published.
This does not assert that a binary is already available. Downloaded binaries
need no Go installation at runtime. Releases publish only native-checked
`linux/amd64`, `windows/amd64`, and the macOS runner's actual `go env`
architecture (usually `darwin/arm64`; `darwin/amd64` is also accepted by the
release workflow). Other architectures can be built manually, not assumed released.

The plugin defaults to the `aks-investigator` release in namespace `aks-investigator`
and falls back to a single Holmes deployment if that name is absent. See the
[plugin documentation](kubectl-plugin/README.md) for custom release and
namespace settings, manual cross-builds, and executable checks. Timing mode
requires Bash only inside the Holmes container, not on the client.

### Per-release plugin publication

The [release workflow](.github/workflows/release.yaml) validates pushed
unprefixed `X.Y.Z` tags (prereleases allowed; `v` prefixes and `+buildmetadata`
are not supported), then runs native binary checks and CPU/GPU E2E. After
publishing the chart to source ACR, it checksum-verifies and renames the tested
executables **without rebuilding**, publishes and verifies one OCI artifact
per native platform, then uploads only those versioned raw binaries to the
matching GitHub release, replacing same-named assets on reruns (`--clobber`).
A missing GitHub release is created with `--verify-tag` and generated notes;
new prereleases are marked as such. GitHub, chart, and OCI versions use the
same unprefixed release version; no `latest` registry tag is published.

| Location | Plugin artifact reference |
| --- | --- |
| Source ACR | `<acr>.azurecr.io/public/microsoft.aks.investigator/kubectl-investigate:<version>-<goos>-<goarch>` |
| Expected MCR | `mcr.microsoft.com/microsoft.aks.investigator/kubectl-investigate:<version>-<goos>-<goarch>` |

For example, the source ACR reference
`<acr>.azurecr.io/public/microsoft.aks.investigator/kubectl-investigate:0.1.0-linux-amd64`
contains **kubectl-investigate-0.1.0-linux-amd64**, also the GitHub asset name.
The Windows tag is `0.1.0-windows-amd64` (no `.exe` in the tag), containing
**kubectl-investigate-0.1.0-windows-amd64.exe**. These are illustrative names,
not evidence that version `0.1.0` is published.

Source publication uses the chart's `AZURE_CONTAINER_REGISTRY` secret and
managed-identity self-hosted 1ES runner. The artifact type is
`application/vnd.microsoft.aks.investigator.kubectl-plugin.v1`: it carries
one raw binary layer (`application/octet-stream`), **not a runnable container
image**. No archive, checksum, license, or inventory payload is published.
**SHA256SUMS** is internal to Actions handoffs for E2E and release publication.
An existing source tag is reused only when its artifact type, single-layer
binary digest, and filename match; different content fails. The workflow
pulls by manifest digest and compares the served binary bytes before GitHub
upload. This verification is not registry-enforced immutability or a global
lock against external publishers.

The workflow verifies source ACR, **not MCR onboarding or availability**. MCR
needs an appropriate ingestion policy and completed propagation. See the
[release flow](kubectl-plugin/README.md#executable-checks-and-release-flow)
and [optional registry download guidance](kubectl-plugin/README.md#optional-download-from-an-oci-registry)
for permissions, digest-based pulls, and installation. These are workflow
capabilities, not claims that a specific plugin version has been published.

## Saturn model configuration

When Saturn is enabled and `modelList` is empty, the chart automatically adds
the following entry to HolmesGPT's model catalog. The namespace and
`max_context_size` are generated from the Helm release and selected runtime:

```yaml
saturn-vllm:
  api_base: http://saturn-vllm.<namespace>.svc.cluster.local:8000/v1
  api_key: dummy
  model: openai/saturn-vllm
  custom_args:
    max_context_size: 65536 # CPU default; GPU default is 262144
```

Model delivery defaults are below. The artifact, model-only image, ORAS image,
and CPU/GPU vLLM images have independent `repository` and `tag` values:

```yaml
imagePullSecrets: [] # Root-level; also applied to the Saturn pod
saturn:
  modelSource: image # Default; set to oci to use modelArtifact with ORAS
  modelPath: /models/saturn-0202
  modelArtifact:
    repository: mcr.microsoft.com/microsoft.aks.investigator/saturn
    tag: "v1-20260202"
  modelPullImage:
    repository: mcr.microsoft.com/oss/v2/oras-project/oras
    tag: v1.2.3
  modelImage:
    repository: mcr.microsoft.com/microsoft.aks.investigator/saturn
    tag: "0.1.0" # Same model as the v1-20260202 OCI artifact
  gpuImage:
    repository: mcr.microsoft.com/microsoft.aks.investigator/vllm-openai
    tag: v0.28.0
  cpuImage:
    repository: mcr.microsoft.com/microsoft.aks.investigator/vllm-openai-cpu
    tag: v0.28.0
```

- **`oci` (opt-in):** the ORAS init container downloads `modelArtifact` into
  `saturn.modelPath`; existing artifact defaults remain supported.
- **`image` (default):** kubelet/the container runtime pulls `modelImage` with
  `IfNotPresent` caching. The `copy-model` init container uses the image's
  entrypoint to copy `/model` into `saturn.modelPath` on the shared, disk-backed
  `emptyDir` mounted at `/models`. No ORAS runs in the pod, and neither the init
  container nor vLLM downloads model files from Hugging Face.

The [model-image Dockerfile](docker/saturn-model/Dockerfile) packages Azure Linux
3.0 core, model files, and `sh`/`cp` tools, not vLLM, Python, or ORAS. The shared
`linux/amd64` image works with the chart's current x86 CPU and NVIDIA GPU pools;
the separate vLLM runtime images stay unchanged. A custom model image must keep
the entrypoint contract: accept one destination argument (`saturn.modelPath`,
under `/models`) and copy the bundled model there with a read-only root
filesystem; the chart supplies the argument, not a replacement command.

**Image mode is not zero-copy or network-free.** Image layers must first be
downloaded and cached on each node, then init copies the model into `emptyDir`.
Budget roughly two model-sized disk copies (image plus volume): ~16.4 GiB
(~17.6 GB) for ~8.2 GiB of model files, plus runtime images and storage overhead.
Every pod recreation loses the volume and repeats the copy; a retained node
image cache can avoid another image download. The model mount is disk-backed,
not tmpfs; serving RAM/KV-cache budgets remain unchanged.

## Publish the Saturn model

The **Publish Saturn model to MCR** workflow
([.github/workflows/publish-saturn-model.yaml](.github/workflows/publish-saturn-model.yaml))
accepts a Hugging Face `revision` and separate optional release tags on manual
dispatch: `tag` for the OCI artifact (default `v1-20260202`) and `image_tag`
for the container image (default `0.1.0`). **Image `0.1.0` corresponds to OCI
artifact `v1-20260202`.** Use new release tags when publishing changed models.
The same workflow produces both model formats:

1. Resolve the HF revision once to an immutable commit; AIKit downloads and
   packages `saturn-ms/saturn-0202` once into a local ModelPack OCI layout.
2. Extract files from that **local layout** with `oras pull --oci-layout`, not
   a second Hugging Face or registry download, and validate the model shards.
3. Publish the existing ModelPack artifact and build/push the new model-only
  container image from those extracted files. The artifact receives
  `sha-<first-12-commit-characters>`; the image receives
  `image-sha-<first-12-commit-characters>`, plus their respective release tags.

| Output | Source ACR publication path | Expected MCR path |
| --- | --- | --- |
| ModelPack OCI artifact | `<acr>.azurecr.io/public/microsoft.aks.investigator/saturn:v1-20260202` | `mcr.microsoft.com/microsoft.aks.investigator/saturn:v1-20260202` |
| Model-only container image | `<acr>.azurecr.io/public/microsoft.aks.investigator/saturn:0.1.0` | `mcr.microsoft.com/microsoft.aks.investigator/saturn:0.1.0` |

Both outputs use the same resolved HF commit even though their release tags
and artifact formats differ. They share the **same repository**, not the same
tag: a registry tag can reference only one manifest. The workflow rejects equal
release tags and reserves separate SHA-tag prefixes to prevent format collisions.
MCR drops the source ACR's `public/` prefix. Its publication policy must support
the runnable image as well as the ModelPack artifact; the workflow currently
verifies ACR manifests, **not MCR availability**. Verify the target image/tag
before deploying with the default `saturn.modelSource=image`; these examples do not
assert that it has been published or passed an end-to-end deployment.
