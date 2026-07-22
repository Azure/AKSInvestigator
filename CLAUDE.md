# AKSInvestigator

AKS-flavored fork of the [HolmesGPT](https://github.com/HolmesGPT/holmesgpt)
Helm chart. This repo is **Helm chart only** — there is no application source
code here. HolmesGPT itself lives upstream; this repo packages and deploys it
for AKS with AKS-specific add-ons layered on top.

## Core model: sync + overlay

`helm/` is synced **verbatim** from the upstream `holmesgpt/helm/holmes` chart.
On top of that, a small set of AKSInvestigator-only files are preserved across
syncs. Understanding this split is essential before editing anything:

- **Upstream-tracked (do NOT hand-patch):** everything under `helm/` that comes
  from upstream, most importantly `helm/values.yaml` (kept byte-identical to
  upstream). Changes here should come from an upstream sync, not manual edits.
- **AKSInvestigator-only (safe to edit, preserved by `sync.sh`):**
  - `helm/templates/saturn/` — the Saturn vLLM add-on
  - `helm/values-saturn.yaml` — Saturn defaults (`saturn.*`)

`scripts/sync.sh` rsyncs `<HOLMESGPT_DIR>/helm/holmes/` into `./helm/` with `--delete`,
stashing and restoring the files listed in its `KEEP_FILES` array. If you add a
new AKSInvestigator-only file that must survive syncs, **add it to `KEEP_FILES`
in `scripts/sync.sh`**, or the next sync will delete it.

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

- `-f helm/values-saturn.yaml` is passed (otherwise `.Values.saturn` is
  undefined and the template renders nothing → chart behaves like upstream), AND
- `.Values.saturn.enabled` is true (default in `values-saturn.yaml`), AND
- `.Values.modelList` is **empty** — as soon as `modelList` is non-empty, Saturn
  is fully suppressed and HolmesGPT uses the external models instead.

CPU vs GPU is selected by `saturn.gpu` (default `false`):
- `saturn.gpu=false` — CPU vLLM, `--device=cpu`, `--dtype=float32`,
  `maxModelLen=4096`. Requires an **AVX-512 Intel** node pool (Standard_D*s_v5);
  the prebuilt CPU image SIGILLs on AMD EPYC (AVX2-only).
- `saturn.gpu=true` — GPU vLLM, `--dtype=auto`, `maxModelLen=65536`, requests
  `nvidia.com/gpu: 1`. Set `saturn.nodeSelector` to land on the GPU pool.

## Layout

```
AKSInvestigator/
├── helm/                      # synced from holmesgpt/helm/holmes
│   ├── Chart.yaml             # name: holmes; version placeholder 0.0.1
│   ├── values.yaml            # byte-identical to upstream — do not patch
│   ├── values-saturn.yaml     # AKSInvestigator-only (saturn.*)
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
helm install aksi ./helm -n holmes --create-namespace -f ./helm/values-saturn.yaml

# GPU — requires an NVIDIA node pool
helm install aksi ./helm -n holmes --create-namespace -f ./helm/values-saturn.yaml \
  --set saturn.gpu=true --set saturn.nodeSelector.agentpool=gpu

# Bring your own model (no Saturn — omit -f values-saturn.yaml)
helm install aksi ./helm -n holmes --create-namespace \
  --set-json 'modelList={"gpt-5":{"model":"openai/gpt-5","api_key":"sk-..."}}'
```

To point HolmesGPT at the in-cluster Saturn model, set `additionalEnvVars`
(`MODEL=openai/saturn-vllm`, `OPENAI_API_BASE=http://saturn-vllm.<ns>.svc.cluster.local:8000/v1`,
`OPENAI_API_KEY=dummy`). See `README.md` for the full snippet.

## Working conventions

- **Prefer editing the upstream chart upstream**, then `./scripts/sync.sh`. Hand-editing
  upstream-tracked files (`values.yaml` especially) drifts from upstream and is
  reverted on the next sync.
- New AKS-only files must be added to `KEEP_FILES` in `scripts/sync.sh`.
- Validate template changes with `helm template ./helm -f ./helm/values-saturn.yaml`
  and `helm lint ./helm` before committing.
- Commits use semantic PR title format: `<type>(<scope>): <description>`
  (e.g. `feat(helm): ...`).
</content>
</invoke>
