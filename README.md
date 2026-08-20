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
plugin provides a direct CLI for asking the in-cluster agent questions.

## What's added vs upstream

- **In-cluster Saturn vLLM model** (`helm/templates/saturn/saturn-vllm.yaml`,
  defaults in `helm/values-saturn.yaml`).
  When you `helm install` with `-f helm/values-saturn.yaml` AND `modelList`
  is empty, an init container pulls the `saturn-ms/saturn-0202` ModelPack
  artifact from MCR into pod-local storage, then vLLM serves that local model
  through an OpenAI-compatible API at `/v1`. The chart automatically registers
  it in HolmesGPT's model list. Toggle the backend with:
  - `saturn.gpu=false` (default) — CPU build of vLLM,
    `--dtype=float32`, `--max-model-len=65536`. Requires an AVX-512 Intel
    node pool (the prebuilt CPU image SIGILLs on AMD EPYC).
  - `saturn.gpu=true` — GPU build, `--dtype=auto`, `--max-model-len=262144`,
    requests `nvidia.com/gpu: 1`. Set `saturn.nodeSelector` to land on
    your GPU pool.

  As soon as `modelList` is non-empty, the Saturn deployment is fully
  suppressed (HolmesGPT uses your external models instead). Without
  `-f helm/values-saturn.yaml`, the chart behaves exactly like upstream
  HolmesGPT.

  See the original standalone manifests this template is derived from at
  `~/llm/holmesgpt/deploy/models/` for the rationale behind each flag.

## Layout

```
AKSInvestigator/
├── kubectl-plugin/              # kubectl investigate client
│   ├── kubectl-investigate
│   └── README.md
├── helm/                       # synced from holmesgpt/helm/holmes
│   ├── Chart.yaml
│   ├── values.yaml             # synced upstream values; image tag pinned by sync
│   ├── values-saturn.yaml      # AKSInvestigator-only defaults (saturn.*)
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

```bash
# CPU (default) — installs anywhere with AVX-512 (Standard_D*s_v5)
helm install aksi ./helm -n aks-investigator --create-namespace \
  -f ./helm/values-saturn.yaml

# GPU — requires an NVIDIA-equipped node pool
helm install aksi ./helm -n aks-investigator --create-namespace \
  -f ./helm/values-saturn.yaml \
  --set saturn.gpu=true \
  --set saturn.nodeSelector.agentpool=gpu

# Bring your own model (no Saturn deployment — omit -f values-saturn.yaml)
helm install aksi ./helm -n aks-investigator --create-namespace \
  --set-json 'modelList={"gpt-5":{"model":"openai/gpt-5","api_key":"sk-..."}}'
```

## Install the kubectl plugin

```bash
install -m 0755 kubectl-plugin/kubectl-investigate ~/.local/bin/kubectl-investigate
kubectl investigate "are any workloads unhealthy?"
```

The plugin defaults to the `aksi` release in namespace `aks-investigator`. See the
[plugin documentation](kubectl-plugin/README.md) for custom release and
namespace settings.

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

Every image used by the Saturn pod, including the ModelPack OCI artifact, has
independent `repository` and `tag` values under `saturn`:

```yaml
saturn:
  modelArtifact:
    repository: mcr.microsoft.com/microsoft.aks.investigator/saturn
    tag: "v1-20260202"
  modelPullImage:
    repository: mcr.microsoft.com/oss/v2/oras-project/oras
    tag: v1.2.3
  gpuImage:
    repository: mcr.microsoft.com/microsoft.aks.investigator/vllm-openai
    tag: v0.26.0
  cpuImage:
    repository: mcr.microsoft.com/microsoft.aks.investigator/vllm-openai-cpu
    tag: v0.26.0
```

## Publish the Saturn model

Run the `Publish Saturn model to MCR source ACR` workflow manually with a
Hugging Face revision and a Saturn release tag (default `v1-20260202`). Use a different
tag for each Saturn release. The workflow resolves
the revision to an immutable commit, packages `saturn-ms/saturn-0202` as a CNCF
ModelPack OCI artifact with AIKit, and publishes both `sha-<commit>` and the
optional friendly tag through the ACR that feeds MCR:

```text
mcr.microsoft.com/microsoft.aks.investigator/saturn:<tag>
```
