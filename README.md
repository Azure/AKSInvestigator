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
  is empty, the chart deploys a vLLM serving `saturn-ms/saturn-0202` with an
  OpenAI-compatible API at `/v1` and automatically registers it in HolmesGPT's
  model list. Toggle the backend with:
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
│   ├── values.yaml             # byte-identical to upstream — do not patch
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

```bash
# Defaults to ~/llm/holmesgpt
./scripts/sync.sh

# Or point at a different clone
HOLMESGPT_DIR=/path/to/holmesgpt ./scripts/sync.sh
```

`scripts/sync.sh` builds the new chart in a temporary directory, restores the
AKSInvestigator-only files listed in `KEEP_FILES`, and applies
`patches/saturn-integration.patch` to the upstream Holmes templates. It updates
`./helm/` only after the patch passes `git apply --check`, so an incompatible
upstream change cannot leave a half-synced chart.

For a new HolmesGPT release, update the upstream clone and run the script. If
the patch still applies, review the resulting `git diff` and run the Helm
validation commands. If it fails, port the rejected Saturn hunks to the new
upstream templates, refresh `patches/saturn-integration.patch`, and rerun the
script. Do not preserve or copy entire modified upstream templates; doing so
would hide new upstream changes.

## Install

```bash
# CPU (default) — installs anywhere with AVX-512 (Standard_D*s_v5)
helm install aksi ./helm -n holmes --create-namespace \
  -f ./helm/values-saturn.yaml

# GPU — requires an NVIDIA-equipped node pool
helm install aksi ./helm -n holmes --create-namespace \
  -f ./helm/values-saturn.yaml \
  --set saturn.gpu=true \
  --set saturn.nodeSelector.agentpool=gpu

# Bring your own model (no Saturn deployment — omit -f values-saturn.yaml)
helm install aksi ./helm -n holmes --create-namespace \
  --set-json 'modelList={"gpt-5":{"model":"openai/gpt-5","api_key":"sk-..."}}'
```

## Install the kubectl plugin

```bash
install -m 0755 kubectl-plugin/kubectl-investigate ~/.local/bin/kubectl-investigate
kubectl investigate "are any workloads unhealthy?"
```

The plugin defaults to the `aksi` release in namespace `holmes`. See the
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
