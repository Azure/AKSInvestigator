# CPU investigation performance

## Quick start (traced by default)

From the repository root on Linux/macOS, with CPU release `aksi-cpu` ready in
`aks-investigator-cpu` and `BRAINTRUST_API_KEY` / `BRAINTRUST_ORG` exported in
this shell. Holmes inside the pod must also have the Braintrust SDK installed
to produce traces. Replace `<context-name>` with your kubeconfig context:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r scripts/performance-requirements.txt
python scripts/performance_benchmark.py --context "<context-name>" --runs 1 \
  "How many nodes are in the cluster? Use read-only tools." --model saturn-vllm --trace braintrust
```

Open `benchmark-results/cpu-<UTC timestamp>/summary.json` in this repository.
The script prints the actual path; `benchmark-results/` is gitignored.
Use `--runs 3` for repeats.
Credentials are passed as exec arguments; see the security notes below.

## Overview

Measure one-shot `kubectl investigate` commands against an **existing CPU
Saturn deployment**, using [../scripts/performance_benchmark.py](../scripts/performance_benchmark.py).
The benchmark runner supports Linux/macOS, not native Windows. It invokes the
Bash plugin from this checkout through real `kubectl` plugin discovery rather
than relying on a possibly older installed copy.

**The standard benchmark requires Braintrust tracing and SDK collection by
default.** Pass `--trace braintrust` after the question and supply both
`BRAINTRUST_API_KEY` and `BRAINTRUST_ORG` in the inherited local environment.
The explicit `--no-braintrust` override before the question instead collects
timing and vLLM metrics only, with no trace flag or Braintrust credentials.
Incomplete timing or invalid vLLM metrics fails either mode; partial/unavailable
trace analysis also fails traced mode. Successful investigation execution alone
is not enough.

One file provides two modes: benchmarking uses the same flags with **no
subcommand**, while `analyze-braintrust` performs analysis only on an existing
trace selected by `--url` or a captured log selected by `--log`. Analysis-only
mode does not access the cluster or require a kubeconfig; it reads Braintrust
using the inherited local credentials. Top-level `--help` shows benchmark
options and an epilog describing the subcommand; `analyze-braintrust --help`
shows analysis-only options.

The benchmark collector itself does not install or upgrade releases, change
deployment settings, restart pods, or clear caches. It performs discovery and
metrics reads and executes the requested investigation inside Holmes. Use a
read-only question and an otherwise idle CPU deployment; the investigation
itself invokes tools. No Helm changes are needed for the collector. The separate
[CPU resource sweep](#cpu-resource-sweep) **does temporarily upgrade the CPU
release and attempt rollback**, and requires permission for those mutations.
These instructions do not target GPU deployments.

## Local setup and CPU target

Benchmark prerequisites (cluster requirements do not apply to analysis-only mode):

- Python 3 with `venv` and `pip`, Bash, and `kubectl` on the local `PATH`.
- An existing kubeconfig context for the cluster hosting the CPU deployment.
- CPU namespace `aks-investigator-cpu`, release `aksi-cpu` (Holmes deployment
  `aksi-cpu-holmes`), and model `saturn-vllm` already available.
- Permission to read pods/deployments, exec into Holmes, and read the selected
  Saturn pod's port-8000 metrics through the Kubernetes pod proxy.
- The required local dependencies below, including the Braintrust SDK even
  with `--no-braintrust`. Traced runs additionally require local Braintrust
  credentials for an organization authorized to receive the investigation data.

From the repository root, install only the local benchmark dependencies:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r scripts/performance-requirements.txt
```

[../scripts/performance-requirements.txt](../scripts/performance-requirements.txt)
includes the Prometheus parser and pins the Braintrust SDK to `0.40.0`.
The script imports `braintrust` at module load; the SDK is mandatory for
benchmarking (including `--no-braintrust`) and standalone analysis, including
help. Only traced benchmark startup rejects a missing `--trace braintrust` or
either missing local credential before creating output or accessing the
cluster. Untraced mode rejects any `--trace` option and skips credential
preflight and SDK collection, not the SDK import. Both modes call
`parse_metrics("")` to verify the Prometheus parser dependency before those
steps. Installing these dependencies does not access or modify the cluster.

The collector defaults to `--namespace aks-investigator-cpu --release aksi-cpu`.
It requires exactly one `app=saturn-vllm` pod and one `app=holmes` pod in that
namespace. It checks that Saturn is Ready, its `vllm` container image includes
`vllm-openai-cpu`, and it does not request or limit `nvidia.com/gpu`. A failed
preflight does not trigger any automatic cluster changes.

`--context <context-name>` selects the context explicitly. Replace
`<context-name>` in the examples with your target kubeconfig context.
The collector makes
a temporary, mode-`0600` kubeconfig containing only that context, passes it to
the plugin through `KUBECONFIG`, and removes it in cleanup. It does not switch
the user's current context. It sets `INVESTIGATOR_NAMESPACE`,
`INVESTIGATOR_RELEASE`, `INVESTIGATOR_TIMING=1`, and `INVESTIGATOR_HOME=/tmp`
for the child process, and ignores inherited `INVESTIGATOR_DEPLOY` and
`INVESTIGATOR_SELECTOR` overrides.

## Braintrust setup for traced CPU runs

**Tracing uploads prompts, tool results, and other investigation data to
the external Braintrust service.** Use only a Braintrust organization authorized
to receive that cluster's data, and obtain approval before running a traced
benchmark. Local log redaction does not redact what Holmes sends to Braintrust.
Traced benchmarks include tracing/upload overhead; those measurements do not
represent untraced plugin latency. This setup is not needed for an explicitly
untraced benchmark or the CPU resource sweep.

Credentials must come only from the invoking process's inherited local
environment: `BRAINTRUST_API_KEY` and `BRAINTRUST_ORG`. Do not put keys in command
examples, documentation, or committed files. These are local process values,
not Helm values or persistent deployment configuration. The plugin expands them
into remote `env` arguments, which can expose them in process listings and
Kubernetes audit records. Supply them through an approved credential manager,
or use hidden input in **Bash** to avoid typing the literal key in shell history:

```bash
set +x
read -rs -p 'Braintrust API key: ' BRAINTRUST_API_KEY
printf '\n'
read -r -p 'Authorized Braintrust organization: ' BRAINTRUST_ORG
export BRAINTRUST_API_KEY BRAINTRUST_ORG
```

The Bash plugin passes the two local values as separate quoted `NAME=value`
arguments to `kubectl exec ... -- env`, setting them only for the new Holmes
CLI process and its children. Values are not evaluated as remote shell code.
There is no stdin credential preamble or persistent pod configuration change.
Timing mode first emits the remote-shell marker, then executes the same `env`
command. For traced runs, missing local credentials are rejected before cluster
access.

**Expanded API keys are visible in process arguments and potentially Kubernetes
audit records.** Hidden input and local output redaction do not protect those
surfaces. Use this forwarding mode only when that exposure is acceptable; avoid
shell tracing and verbose command logging.

Standalone `kubectl investigate` supports both traced one-shot and interactive
sessions when attached to a terminal. Tracing remains optional for standalone
plugin use; without `--trace braintrust`, the plugin does not forward these local
credentials. The benchmark requires tracing by default, unless explicitly
disabled with its own `--no-braintrust` option, and always uses non-TTY one-shot
mode.

## Run the traced CPU benchmark

After the environment setup above, use the activated virtual environment.
Each invocation automatically creates a timestamped directory under
`benchmark-results/` at the repository root, independent of the terminal's
working directory. This invocation includes the required tracing flag and
explicitly sets the default post-run fetch timeout:

```bash
python scripts/performance_benchmark.py --context "<context-name>" --runs 3 \
  --braintrust-timeout 60 \
  "How many nodes are in the cluster? Use read-only tools." --model saturn-vllm --trace braintrust
```

To choose a different destination, pass `--output <path>` before the question.
A custom directory **must not already exist**. Relative custom paths are
relative to the terminal's working directory. Only the repository's
`benchmark-results/` directory is covered by the added Git ignore rule.

Collector options go **before the question**. Arguments after the question,
including `--model` and the required `--trace braintrust`, are forwarded to
Holmes. Each invocation is non-TTY and one-shot; the collector rejects
`--interactive`/`-i`.

Keep the credentials available for any later read-only analysis, then remove
them from the local session when finished:

```bash
unset BRAINTRUST_API_KEY BRAINTRUST_ORG
```

### Automatic, read-only post-run analysis

In traced mode, after each non-timeout command has been timed and post-run vLLM
collection attempted, the collector always attempts trace analysis, even if
the command failed or metrics are invalid. It parses the final `View trace:`
footer in the last 64 KiB of captured stdout, falling back to stderr. The shared parser and
analysis helpers live in [../scripts/performance_benchmark.py](../scripts/performance_benchmark.py).
The parser handles ANSI/OSC formatting and wrapped URLs. It accepts only HTTPS Braintrust
experiment URLs whose organization matches local `BRAINTRUST_ORG`; it does
not treat arbitrary narrative URLs as trace footers.

`fetch_analysis(url)` directly calls the mandatory local Braintrust `0.40.0`
SDK's `braintrust.init(..., open=True, set_current=False)` to open the existing
experiment, then reads `experiment.fetch()`.
This analysis does not create/update experiments or log new events. It reopens
the experiment for up to three fetch attempts when the snapshot is partial,
because an experiment handle caches its first fetched snapshot.

The per-run report is under `runs[].braintrust` in the root JSON summary shown
below, separate from `runs[].timing` and `runs[].vllm`.
`fetch_analysis_seconds` measures the post-run fetch/analysis process; it is
not included in `wall_seconds` or the vLLM scrape window. In contrast, tracing
and uploads performed by the investigation itself are inside command timing.
`--braintrust-timeout 60` bounds this post-run worker to 60 seconds per run;
it is separate from `--timeout`, the command timeout (default 900 seconds).
The worker remains a separate subprocess, invoking the same file with the
`analyze-braintrust` subcommand.

- `available`: fetched spans have roots, valid closed timing intervals, and
  no detected missing parents/unidentified rows. This is **not proof that all
  uploads have arrived**, nor proof that the investigation succeeded.
- `partial`: empty or incomplete trace snapshots retain known aggregates;
  unknown values remain `null`, not invented zeros or successful timings.
- `unavailable`: no valid footer, missing credentials/access, SDK failure, or
  fetch timeout. A command timeout also skips fetching and reports unavailable.

The traced benchmark requires Braintrust `status: available` as part of
`collection_complete`. A `partial` or `unavailable` result fails collection,
stops further runs, and returns a nonzero exit even if the command succeeded.
Known aggregates and other collected measurements remain in the saved summary.
See the full completion and exit rules below.

The analyzer summarizes root durations, interval unions, per-type
`llm`/`tool`/`task`/`other` counts and durations, error counts, and leaf-LLM token
totals. `root_durations_seconds` describes each traced root;
`root_interval_union_seconds` merges overlapping root intervals. Per-type
`duration_sum_seconds` adds span durations, including nested or parallel work,
and can exceed elapsed time. Do not add those sums to estimate command wall
time. Even per-type interval unions can overlap with other types. Trace timing
does not cover local dispatch, exec startup, or uninstrumented CLI setup.
Leaf-LLM token totals avoid counting parent/root rollups again; missing token
fields stay `null`.

**No raw trace payload is persisted by the analyzer.** Fetched inputs, outputs,
span names, metadata, and error text are omitted from the aggregate report.
The report retains experiment identifiers/URL and timing/count statistics;
the separately captured CLI output can still contain sensitive data.

### Retry analysis later without rerunning the investigation

If uploads are delayed or access is temporarily unavailable, retry the
`analyze-braintrust` mode in the activated virtual environment with the same
inherited local credentials. Replace the
illustrative log path with a captured run's stdout log (or stderr if that
contains the footer):

```bash
python scripts/performance_benchmark.py analyze-braintrust \
  --log "benchmark-results/cpu-<UTC timestamp>/run-01/stdout.log"
```

Alternatively, copy the actual experiment URL from the `View trace:` footer.
The following URL is a placeholder, not an existing experiment; replace all
three identifiers, keeping the organization equal to `BRAINTRUST_ORG`:

```bash
python scripts/performance_benchmark.py analyze-braintrust \
  --url 'https://www.braintrust.dev/app/<authorized-org>/p/<project>/experiments/<experiment>'
```

Analysis-only mode prints aggregate JSON and returns zero only for
`available`, otherwise one. It does not rewrite the saved benchmark summary
or rerun the investigation, and does not access the cluster.
`--braintrust-timeout` is a collector option, not an `analyze-braintrust` option.

## Explicit untraced CPU benchmark

For an approved timing/vLLM-only measurement, use the same existing local
dependencies and CPU target, but put `--no-braintrust` **before the question**:

```bash
python scripts/performance_benchmark.py --context '<context-name>' --runs 1 \
  --no-braintrust \
  "How many nodes are in the cluster? Use read-only tools." --model saturn-vllm
```

Do not include any `--trace` option; combining it with `--no-braintrust` is an
error. No Braintrust credentials are needed, and the collector removes the two
local Braintrust credential variables from the child environment. It does not
fetch or analyze traces. The top-level SDK import still requires the already
installed local SDK. This is a collector-only override, not a plugin flag.

Results set `braintrust_enabled: false`, `braintrust_required: false`, and
`runs[].braintrust.status: disabled`. Collection still requires successful,
non-timeout execution, both timing markers, and valid vLLM metrics; there is no
trace-availability check. Do not compare these timings with traced runs as if
their overhead were identical.

## CPU resource sweep

[../scripts/sweep_cpu.py](../scripts/sweep_cpu.py) runs the explicitly approved
untraced, timing/vLLM-only sweep at **6, 7, 8, 9, 10, and 12 CPU**, sequentially,
with Saturn's CPU **request equal to its limit** at each level. Defaults are
namespace `aks-investigator-cpu` and release `aksi-cpu`; no GPU release is
targeted. Unlike the collector, this wrapper **mutates the CPU Helm release**:
it upgrades using the matching local chart and reused values, temporarily pins
Saturn and Holmes to their respective original nodes, and attempts restoration.
Obtain permission for upgrades, pod replacement, and rollback before running.

Use an idle, deployed, single-replica CPU release, a matching local chart,
`helm` and `kubectl` on `PATH`, and the existing repository virtual environment
with the benchmark dependencies above. Additional access is needed to inspect
ReplicaSets, read Saturn startup logs/cgroup files via exec, and manage the
Helm release. The sweep performs **no SDK installation or Braintrust tracing,
upload, or collection**; its child collector still imports the installed SDK.
From the repository root:

```bash
. .venv/bin/activate
python scripts/sweep_cpu.py --context '<context-name>'
```

Optionally append `--levels 6 7 8 9 10 12` to state the defaults explicitly;
only distinct levels from that set are accepted. `--namespace` and `--release`
can select another approved CPU release. The default question asks for the
cluster's node count using read-only tools and a one-sentence answer.

- **Sampling:** one investigation per level, measuring the first inference
  after fresh Saturn and Holmes pods are verified on their original nodes,
  with no inference warmup. vLLM/pod-local
  `emptyDir` caches reset, not host OS caches or client authentication. Startup
  is recorded separately as `rollout_seconds`, outside command wall time.
- **Controls and limits:** model, images, memory budgets, context length, and
  dtype are unchanged; live workload specs are checked after upgrade, not by a
  full-chart pre-render. The chart uses `--dtype=auto`; BF16 is expected for the
  current model, but confirm the resolved dtype from startup evidence before
  interpreting results. CPU quota and effective cpusets are recorded, not proof
  of dedicated cores. For example, 12 vCPUs on an eight-physical-core node do
  not provide 12 dedicated physical cores.
- **Variability:** one sample cannot establish variance or statistical scaling.
  A stable question can produce different answers, tool calls, and output-token
  counts; compare request/prompt/generation counts alongside timing. vLLM deltas
  cover all pod traffic, so keep other callers idle.
- **Cgroups:** before/after `cpu.stat` captures usage, throttled time, and period
  deltas. `throttle_ratio` is `delta(nr_throttled) / delta(nr_periods)`, **not a
  wall-time percentage**. `working_gib` estimates post-run
  `max(memory.current - inactive_file, 0) / 2**30`, not peak working memory.
  Snapshots may include `memory.peak`, a container-lifetime high-water mark,
  not an investigation-only peak. Cgroup windows also include surrounding
  preflight/scrape overhead.
- **Runtime and recovery:** model pulls, rollout, and execution can take
  20 minutes or more per level. Defaults are `--rollout-timeout 1200` for
  individual rollout waits and `--timeout 900` for investigation execution,
  not a single per-level deadline. A timeout or failure stops further levels;
  after an upgrade attempt, `finally` attempts rollback to the **original
  revision**, without `--force`. Rollback creates a new Helm revision and is
  verified against the original workload templates and readiness. Restoration
  is not guaranteed: concurrent revision changes or uncertain upgrade ownership
  block automatic rollback rather than overwrite another change. Guards are
  not atomic locks; do not edit the release concurrently. Pending/failed Helm
  states or rollback errors need inspection; consult `restoration.success`,
  `blocked`, `needs_manual`, and error fields. A completed measurement sweep can
  finish as `restoration_failed`. Timeout/interruption may leave remote exec
  running; verify completion before retrying. Forced termination can bypass
  cleanup.

Output is under the repository's gitignored
`benchmark-results/cpu-sweep-<UTC timestamp>/`: root `summary.json`,
`comparison.md`, and `comparison.csv`, plus per-level `cpu-6/`, `cpu-7/`, etc.
Each level retains cgroup snapshots, allowlisted startup fragments, a benchmark
log, and `measurement/summary.json` with per-run logs/vLLM snapshots. Only
complete, valid collections are marked comparable; failed measurements remain
diagnostic. Reports include restoration outcome, and exit zero requires both
all requested measurements and successful restoration. The sensitive-data
precautions below apply even without tracing.

## Output and sensitive data

Results remain in this repository so they can be opened directly in the editor.
The root `/benchmark-results/` rule in `.gitignore` excludes summaries, logs,
metrics snapshots and any leftover temporary kubeconfigs from normal Git adds.
Do not force-add these artifacts: Git ignore is not a security boundary.

A typical output directory contains:

```text
benchmark-results/cpu-<UTC timestamp>/
  summary.json
  run-01/
    stdout.log
    stderr.log
    before.prom
    after.prom
  run-02/...
  run-03/...
```

The collector's root summary records context, CPU image/resources,
pod/container identities, each run's timing and metric validity, and Braintrust
status. `braintrust_enabled` and `braintrust_required` are both `true` by
default, or both `false` with `--no-braintrust` (analysis status `disabled`).

`runs[].collection_complete` is `true` only when **all** of these hold:

- The command's `exit_code` is `0` and `timed_out` is `false`.
- Both timing markers were received (`timing_markers_complete: true`).
- vLLM metrics have `valid: true`.
- In traced mode only, Braintrust analysis has `status: available`.

The root report `status` starts as `running`, becomes `complete` only when all
requested runs meet those requirements, and becomes `failed` on an incomplete
run. The summary is saved after each measured run, before stopping for a
collection failure, preserving measurements from earlier runs and available
data from the failed run. Missing timing markers, invalid metrics, or (in
traced mode) partial/unavailable trace analysis returns exit `1` when the
command itself succeeded. A command failure propagates its nonzero exit code;
a local timeout/interruption returns `124`. All of these stop further runs; there is
no successful partial-collection mode. Failures before a measured run is
recorded may leave no summary for that attempt.

Per-run logs and Prometheus snapshots support inspection; an interrupted or
failed run may not have every artifact. The root and per-run directories are
created with mode `0700`. The temporary kubeconfig contains credentials and is
mode `0600`; if the collector is forcibly killed, check for and securely remove
any leftover private kubeconfig.

The collector does not persist argv or the environment. It removes timing
markers from logs and redacts literal question lines and, in traced mode, the
exact local Braintrust key, but **logs remain sensitive**: answers, tool results, reformatted
or paraphrased questions, and other secrets may still appear. Metrics, resource
identities, and trace URLs can also reveal cluster information. Protect the
whole directory, review/redact before sharing, and follow data-retention policy.

## Interpret the measurements

### Local command timing

All marker timing uses receipt times on the local monotonic clock; it does
not subtract clocks from different machines.

| Field under `runs[].timing` | Meaning and limits |
| --- | --- |
| `wall_seconds` | Local command launch-to-exit duration: an upper bound on time to the result for a successful command, not a separately detected first-answer or first-token latency. Excludes collector preflights, metrics scrapes, and post-run SDK fetching. |
| `client_before_exec_seconds` | Launch to receipt of the local exec-request marker. Includes `kubectl` plugin discovery/dispatch, plugin startup, and deployment discovery before exec. |
| `exec_startup_observed_seconds` | Local exec-request marker receipt to remote-shell marker receipt. Includes local scheduling, authentication, Kubernetes API/exec handshake, remote shell startup, and return network/stdio I/O. This is **not image boot or pod startup time**. |
| `remote_to_exit_seconds` | Remote-shell marker receipt to local exit. Includes Python startup/imports, tool initialization, agent/model/tool work, tracing when enabled, and I/O/stream teardown. It does **not isolate imports** or pure model execution. |
| `timing_markers_complete` | Whether both markers were observed. Missing boundaries remain `null`; do not infer a missing phase from other measurements. |

For the collector alone, the first run is labeled `first_observed`, **not true
cold**. Existing pod state, vLLM prefix-cache warmth, and Holmes/tool caches are
untouched. The sweep separately requires fresh pods, with the cache limits
described above. Repeating the question on unchanged pods can benefit from
prefix reuse. Preflights and metrics reads also warm client authentication
before measurement, so this is not a cold-auth test.
The `subsequent_wall_median_seconds` aggregate excludes the first run and uses
only subsequent runs with `collection_complete: true`, including complete
timing markers, valid vLLM metrics, and (in traced mode) available Braintrust
analysis. It is `null` if none qualify, including each single-run sweep level.
It is not a controlled cold-versus-warm experiment.

### vLLM metrics

- Latency histogram means use **delta of `_sum` divided by delta of `_count`**,
  weighted across label sets. These are means, not p50/p95/p99 quantiles.
  The report retains the duration sum and observation count. Request-level
  timings are not the same as an entire multi-call investigation's wall time.
- Counter deltas include requests, tokens, prefix-cache hits/queries, and
  preemptions. Missing families stay `null`.
- Every delta covers **all traffic on the selected Saturn pod** between
  scrapes, including other clients and the surrounding measurement overhead.
  Selecting one pod does not provide exact attribution to this investigation.
  `generated_tokens_per_command_second` uses the token delta divided by local
  command wall time; it is not isolated model decode throughput.
- Running/waiting request and KV-cache gauges are before/after snapshots
  (summed across labels), **not peaks** or continuous utilization measurements.
- Pod/container changes or restarts invalidate measurement; detected process
  or series restarts, counter resets/disappearances, and nonfinite deltas are
  also invalid. Inspect `valid` and warnings before using results.
- A local timeout/interruption does not guarantee that remote exec stopped.
  Its vLLM metrics are invalid and further runs stop. Verify remote completion
  before retrying, rather than attributing lingering traffic to a new run.