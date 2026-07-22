# AKSInvestigator

AKS-flavored fork of the [HolmesGPT](https://github.com/HolmesGPT/holmesgpt)
Helm chart. Tracks the upstream `helm/holmes` chart verbatim (no patches to
`values.yaml`) and layers AKS-specific add-ons on top via
`helm/templates/saturn/` + `helm/values-saturn.yaml`.

## What's added vs upstream

- **In-cluster Saturn vLLM model** (`helm/templates/saturn/saturn-vllm.yaml`,
  defaults in `helm/values-saturn.yaml`).
  When you `helm install` with `-f helm/values-saturn.yaml` AND `modelList`
  is empty, the chart deploys a vLLM serving `saturn-ms/saturn-0202` with an
  OpenAI-compatible API at `/v1`, so HolmesGPT has a model to talk to out of
  the box. Toggle the backend with:
  - `saturn.gpu=false` (default) — CPU build of vLLM, `--device=cpu`,
    `--dtype=float32`, `--max-model-len=4096`. Requires an AVX-512 Intel
    node pool (the prebuilt CPU image SIGILLs on AMD EPYC).
  - `saturn.gpu=true` — GPU build, `--dtype=auto`, `--max-model-len=65536`,
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
├── helm/                       # synced from holmesgpt/helm/holmes
│   ├── Chart.yaml
│   ├── values.yaml             # byte-identical to upstream — do not patch
│   ├── values-saturn.yaml      # AKSInvestigator-only defaults (saturn.*)
│   ├── crds/
│   └── templates/
│       ├── ... upstream templates ...
│       └── saturn/             # AKS-specific add-ons (preserved by sync.sh)
│           └── saturn-vllm.yaml
└── scripts/sync.sh             # pull latest chart from upstream
```

## Sync from upstream

```bash
# Defaults to ~/llm/holmesgpt
./scripts/sync.sh

# Or point at a different clone
HOLMESGPT_DIR=/path/to/holmesgpt ./scripts/sync.sh
```

`scripts/sync.sh` rsyncs `<HOLMESGPT_DIR>/helm/holmes/` into `./helm/`, then restores
the AKSInvestigator-specific files (`templates/saturn/` and
`values-saturn.yaml`) that the rsync `--delete` would otherwise drop. Because
`values.yaml` is now tracked verbatim from upstream, no manual re-application
is needed after a sync — just review `git diff` to see what changed upstream.

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

## Point HolmesGPT at the in-cluster Saturn model

When you installed with `-f helm/values-saturn.yaml` (and `modelList` is
empty), the Saturn deployment is rendered. Point HolmesGPT at it by adding
to your install command (or a custom values file):

```yaml
additionalEnvVars:
  - name: MODEL
    value: openai/saturn-vllm
  - name: OPENAI_API_BASE
    value: http://saturn-vllm.holmes.svc.cluster.local:8000/v1
  - name: OPENAI_API_KEY
    value: dummy   # vLLM doesn't enforce auth; LiteLLM needs a non-empty value
```

(replace `holmes` with whatever namespace you installed into).
