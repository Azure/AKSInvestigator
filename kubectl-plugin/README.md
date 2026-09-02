# kubectl-investigate

A `kubectl` plugin that asks the in-cluster AKS Investigator to investigate a
question about the current cluster. It is adapted from
[`mainred/investigate`](https://github.com/mainred/investigate) for this chart's
default `aks-investigator` release in the `aks-investigator` namespace.

The plugin runs `python holmes_cli.py ask` inside the Holmes deployment with
`kubectl exec`. Questions and additional Holmes CLI flags are forwarded
unchanged.

## Prerequisites

- AKS Investigator installed in the cluster.
- `kubectl` configured for that cluster.
- Permission to get deployments and exec into the Holmes pod.

## Install

```bash
install -m 0755 kubectl-plugin/kubectl-investigate ~/.local/bin/kubectl-investigate

# Ensure ~/.local/bin is on PATH, then verify discovery.
kubectl plugin list | grep investigate
```

Any directory on `PATH` works. A system-wide installation can use
`/usr/local/bin` instead.

## Usage

```bash
kubectl investigate "how many pods are in my cluster"
kubectl investigate "why is pod payment-service-1 crashlooping"
kubectl investigate "list noisy namespaces by log volume" --model saturn-vllm
```

When attached to a terminal, the plugin allocates a TTY so Holmes's rich output
uses the terminal width. A question runs in one-shot mode by default, so the
command exits after printing the answer. Pass `--interactive` or `-i` to remain
in Holmes's conversational prompt. The plugin also works without a TTY,
allowing output to be piped or captured.

## Configuration

The default target is deployment `aks-investigator-holmes` in namespace `aks-investigator`.
If that deployment does not exist, the plugin falls back to finding exactly one
deployment matching `app=holmes`.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `INVESTIGATOR_NAMESPACE` | `aks-investigator` | Namespace containing AKS Investigator. |
| `INVESTIGATOR_RELEASE` | `aks-investigator` | Helm release used to form `<release>-holmes`. |
| `INVESTIGATOR_DEPLOY` | _(auto)_ | Explicit deployment; skips release and label discovery. |
| `INVESTIGATOR_SELECTOR` | `app=holmes` | Fallback deployment selector. |
| `INVESTIGATOR_HOME` | `/tmp` | Writable home directory inside the Holmes pod. |

For a release named `investigator` in namespace `observability`:

```bash
INVESTIGATOR_NAMESPACE=observability INVESTIGATOR_RELEASE=investigator \
  kubectl investigate "are any workloads unhealthy?"
```

`HOME` defaults to `/tmp` because the chart runs Holmes with a read-only root
filesystem while mounting `/tmp` as writable. Holmes uses its home directory
for cached toolset state.
