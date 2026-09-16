#!/usr/bin/env python3
"""Benchmark CPU Saturn and read existing Braintrust experiments."""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
from urllib.parse import quote, unquote, urlsplit

import braintrust


HOSTS = {"www.braintrust.dev", "braintrust.dev"}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
TOKEN_METRICS = ("prompt_tokens", "completion_tokens", "tokens")
MARKERS = {
    b"__AKSI_TIMING_EXEC_REQUEST__": "exec_requested",
    b"__AKSI_TIMING_EXEC_STARTED__": "exec_started",
}
LATENCIES = (
    "time_to_first_token_seconds",
    "inter_token_latency_seconds",
    "request_time_per_output_token_seconds",
    "e2e_request_latency_seconds",
    "request_queue_time_seconds",
    "request_prefill_time_seconds",
    "request_decode_time_seconds",
    "request_inference_time_seconds",
)
COUNTERS = (
    "request_success_total", "prompt_tokens_total", "generation_tokens_total",
    "prefix_cache_hits_total", "prefix_cache_queries_total",
    "num_preemptions_total",
)
GAUGES = (
    "num_requests_running", "num_requests_waiting", "kv_cache_usage_perc",
)


def trace_requested(arguments):
    provider = None
    take_next = False
    for argument in arguments:
        if take_next:
            provider, take_next = argument, False
        elif argument == "--":
            break
        elif argument == "--trace":
            take_next = True
        elif argument.startswith("--trace="):
            provider = argument.split("=", 1)[1]
    return (provider or "").lower() == "braintrust"


def parse_experiment_url(url, expected_org):
    """Extract identifiers, never use an output-supplied host for API calls."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https" or parsed.hostname not in HOSTS
        or parsed.username or parsed.password or parsed.port not in (None, 443)
    ):
        raise ValueError("Expected an HTTPS Braintrust experiment URL.")
    parts = parsed.path.strip("/").split("/")
    if (
        len(parts) != 6 or parts[0] != "app" or parts[2] != "p"
        or parts[4] != "experiments"
    ):
        raise ValueError("Unsupported Braintrust experiment URL path.")
    org, project, experiment = [unquote(parts[i]) for i in (1, 3, 5)]
    if not all((org, project, experiment)) or org != expected_org:
        raise ValueError("Experiment organization does not match local org.")
    if any(ord(c) < 32 for c in org + project + experiment):
        raise ValueError("Invalid control characters in experiment URL.")
    canonical = (
        "https://www.braintrust.dev/app/" + quote(org, safe="")
        + "/p/" + quote(project, safe="")
        + "/experiments/" + quote(experiment, safe="")
    )
    return {"org": org, "project": project, "experiment": experiment,
            "url": canonical}


def extract_experiment(text, expected_org):
    """Use only the last CLI footer, allowing Rich's wrapped URL output."""
    clean = ANSI.sub("", OSC.sub("", text))
    footers = list(re.finditer(r"View trace:\s*", clean))
    if not footers:
        return None
    lines = clean[footers[-1].end():].splitlines()
    if not lines:
        return None
    url = lines[0].strip()
    for line in lines[1:]:
        continuation = line.strip()
        if not continuation or not re.fullmatch(
            r"[A-Za-z0-9%._~:/?#\[\]@!$&'()*+,;=\-]+", continuation
        ):
            break
        url += continuation
    try:
        return parse_experiment_url(url, expected_org)
    except ValueError:
        return None


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def interval(row):
    metrics = row.get("metrics") or {}
    if not isinstance(metrics, dict):
        return None
    start, end = number(metrics.get("start")), number(metrics.get("end"))
    if start is None or end is None or end < start:
        return None
    return start, end


def union_seconds(intervals):
    end = None
    total = 0.0
    for start, stop in sorted(intervals):
        total += max(0, stop - max(start, end if end is not None else start))
        end = max(stop, end if end is not None else stop)
    return total


def summarize_spans(events):
    # Fetch pagination can return different versions of the same event.
    unique = {}
    unidentified = 0
    for row in events:
        key = row.get("id") or row.get("span_id")
        if not key:
            unidentified += 1
            continue
        old = unique.get(key)

        def version(event):
            try:
                return int(event.get("_xact_id", 0))
            except (ValueError, TypeError):
                return 0
        if old is None or version(row) >= version(old):
            unique[key] = row
    rows = list(unique.values())
    roots = [r for r in rows if r.get("span_id") and
             r.get("span_id") == r.get("root_span_id")]
    root_intervals = [interval(r) for r in roots if interval(r) is not None]
    known_ids = {r.get("span_id") for r in rows if r.get("span_id")}
    hierarchy_incomplete = sum(
        not r.get("span_id") or not r.get("root_span_id")
        or r.get("root_span_id") not in known_ids
        for r in rows
    )
    missing_parents = sum(
        any(parent not in known_ids for parent in r.get("span_parents") or [])
        for r in rows
    )
    groups = {}
    for kind in ("llm", "tool", "task", "other"):
        selected = [r for r in rows if (
            (r.get("span_attributes") or {}).get("type")
            if (r.get("span_attributes") or {}).get("type")
            in ("llm", "tool", "task") else "other"
        ) == kind]
        spans = [interval(r) for r in selected if interval(r) is not None]
        durations = [end - start for start, end in spans]
        groups[kind] = {
            "span_count": len(selected), "timed_span_count": len(spans),
            "duration_sum_seconds": sum(durations) if durations else None,
            "duration_mean_seconds": (
                sum(durations) / len(durations) if durations else None
            ),
            "duration_max_seconds": max(durations) if durations else None,
            "interval_union_seconds": union_seconds(spans) if spans else None,
            "error_span_count": sum(bool(r.get("error")) for r in selected),
        }
    # Only leaf LLM spans contribute tokens; task/root metrics can roll up
    # their children's usage and must never be counted a second time.
    by_span = {r.get("span_id"): r for r in rows if r.get("span_id")}
    llm_rows = [r for r in rows if
                (r.get("span_attributes") or {}).get("type") == "llm"]
    llm_ancestors = set()
    for row in llm_rows:
        pending = list(row.get("span_parents") or [])
        visited = set()
        while pending:
            parent = pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            ancestor = by_span.get(parent)
            if ancestor:
                attributes = ancestor.get("span_attributes") or {}
                if attributes.get("type") == "llm":
                    llm_ancestors.add(parent)
                pending.extend(ancestor.get("span_parents") or [])
    leaves = [r for r in llm_rows if r.get("span_id")
              and r.get("root_span_id")
              and r.get("span_id") not in llm_ancestors]
    tokens = {}
    for metric in TOKEN_METRICS:
        values = [
            number(r["metrics"].get(metric))
            if isinstance(r.get("metrics"), dict) else None
            for r in leaves
        ]
        known = [v for v in values if v is not None]
        tokens[metric] = {"sum": sum(known) if known else None,
                          "reporting_spans": len(known)}
    complete = bool(roots) and all(interval(r) is not None for r in rows)
    complete = (complete and not missing_parents and not unidentified
                and not hierarchy_incomplete)
    return {
        "status": "available" if complete else "partial",
        "span_count": len(rows), "root_count": len(roots),
        "unidentified_rows": unidentified,
        "hierarchy_incomplete_rows": hierarchy_incomplete,
        "spans_with_missing_parents": missing_parents,
        "root_durations_seconds": [b - a for a, b in root_intervals],
        "root_interval_union_seconds": (
            union_seconds(root_intervals) if root_intervals else None
        ),
        "by_type": groups, "leaf_llm_tokens": tokens,
        "error_span_count": sum(bool(r.get("error")) for r in rows),
        "notes": [
            "Trace timing excludes local dispatch, exec startup "
            "and CLI setup.",
            "Nested/parallel span duration sums are not command wall time.",
            "Token totals use leaf LLM spans only; missing values stay null.",
            "Available means fetched spans are closed, not proof "
            "all uploads arrived.",
            "Input, output, span names, metadata and error text "
            "are not exported.",
        ],
    }


def fetch_analysis(url):
    org = os.environ.get("BRAINTRUST_ORG", "")
    key = os.environ.get("BRAINTRUST_API_KEY", "")
    if not key or not org:
        return {
            "status": "unavailable", "reason": "Local credentials missing.",
        }
    target = parse_experiment_url(url, org)
    # Reopen on each attempt: fetch() caches its first snapshot. Never use
    # update=True or logging APIs; open=True fails if the experiment is absent.
    summary = {"status": "partial", "span_count": 0}
    for attempt in range(1, 4):
        experiment = braintrust.init(
            project=target["project"], experiment=target["experiment"],
            org_name=org, api_key=key, app_url="https://www.braintrust.dev",
            open=True, set_current=False,
        )
        summary = summarize_spans(experiment.fetch())
        summary.update(target, fetch_attempts=attempt)
        if summary["status"] == "available":
            break
    return summary


def analysis_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="performance_benchmark.py analyze-braintrust",
        description=(
            "Read an existing Holmes Braintrust experiment; "
            "never create or update it."
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url")
    source.add_argument("--log", type=Path, help="Captured CLI output log.")
    options = parser.parse_args(argv)
    try:
        url = options.url
        if options.log:
            target = extract_experiment(
                options.log.read_text(), os.environ.get("BRAINTRUST_ORG", "")
            )
            if not target:
                raise ValueError("No matching experiment footer")
            url = target["url"]
        result = fetch_analysis(url)
    except Exception as error:
        # SDK exceptions can include HTTP bodies/credentials; report only type.
        result = {
            "status": "unavailable", "error_type": type(error).__name__,
            "reason": "Unable to read experiment; check access "
            "and upload completion.",
        }
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "available" else 1


def parse_metrics(text):
    """Keep label sets distinct so resets cannot be hidden by aggregation."""
    from prometheus_client.parser import text_string_to_metric_families

    return {
        (sample.name, tuple(sorted(sample.labels.items()))): float(
            sample.value
        )
        for family in text_string_to_metric_families(text)
        for sample in family.samples
    }


def metric_delta(before, after, name):
    old = {
        labels: value for (metric, labels), value in before.items()
        if metric == name
    }
    new = {
        labels: value for (metric, labels), value in after.items()
        if metric == name
    }
    if not new or old.keys() - new.keys():
        return None
    changes = [value - old.get(labels, 0.0) for labels, value in new.items()]
    if any(not math.isfinite(value) or value < 0 for value in changes):
        return None
    total = sum(changes)
    return total if math.isfinite(total) else None


def metric_sum(snapshot, name):
    values = [
        value for (metric, _), value in snapshot.items() if metric == name
    ]
    return sum(values) if values and all(map(math.isfinite, values)) else None


def summarize_metrics(before_text, after_text, wall_seconds):
    before, after = parse_metrics(before_text), parse_metrics(after_text)
    # Only existing series can restart. Newly created label sets are normal
    # on the first request and must not invalidate the report.
    restarted = any(
        after.get(key) != value
        for key, value in before.items()
        if (
            key[0] == "process_start_time_seconds"
            or key[0].endswith("_created")
        )
    )
    result = {
        "valid": not restarted,
        "scope": "all traffic on the selected Saturn pod",
    }
    if restarted:
        result["warning"] = (
            "Metric process/series restarted; deltas are invalid."
        )
        return result
    present = {metric for metric, _ in before.keys() | after.keys()}
    invalid_metrics = []

    def delta(name):
        value = metric_delta(before, after, name)
        if value is None and name in present:
            invalid_metrics.append(name)
        return value

    result["counters"] = {
        name: delta("vllm:" + name) for name in COUNTERS
    }
    result["latencies"] = {}
    for name in LATENCIES:
        total = delta("vllm:" + name + "_sum")
        count = delta("vllm:" + name + "_count")
        result["latencies"][name] = {
            "sum_seconds": total,
            "observations": count,
            "mean_seconds": (
                total / count if total is not None and count else None
            ),
        }
    if invalid_metrics:
        result["valid"] = False
        result["invalid_metrics"] = invalid_metrics
        result["warning"] = (
            "Measured counter series reset, disappeared or became "
            "nonfinite; affected deltas are invalid."
        )
    result["gauges"] = {
        name: {
            "before": metric_sum(before, "vllm:" + name),
            "after": metric_sum(after, "vllm:" + name),
        }
        for name in GAUGES
    }
    generated = result["counters"]["generation_tokens_total"]
    hits = result["counters"]["prefix_cache_hits_total"]
    queries = result["counters"]["prefix_cache_queries_total"]
    result["generated_tokens_per_command_second"] = (
        generated / wall_seconds
        if generated is not None and wall_seconds > 0 else None
    )
    result["prefix_cache_hit_ratio"] = (
        hits / queries if hits is not None and queries else None
    )
    return result


def pod_identity(pod, container_name):
    """Return allowlisted restart metadata without inspecting container env."""
    container = next(
        c for c in pod["spec"]["containers"] if c["name"] == container_name
    )
    status = next(
        (c for c in pod.get("status", {}).get("containerStatuses", [])
         if c["name"] == container_name),
        {},
    )
    running = status.get("state", {}).get("running")
    return {
        "pod": pod["metadata"]["name"],
        "uid": pod["metadata"]["uid"],
        "node": pod["spec"].get("nodeName"),
        "container": container_name,
        "image": container["image"],
        "image_id": status.get("imageID"),
        "container_id": status.get("containerID"),
        "restart_count": status.get("restartCount"),
        "started_at": (
            running.get("startedAt") if running is not None else None
        ),
        "running": running is not None,
    }


def measure_command(command, env, output_dir, timeout):
    """Time received exec markers locally; never subtract remote clocks."""
    times = {}
    # Redact each literal question line, including multiline questions.
    # Do not persist argv or the environment; output can still be sensitive.
    question_lines = (
        command[2].encode("utf-8").splitlines()
        if len(command) > 2 and command[1] == "investigate" else []
    )
    secret = env.get("BRAINTRUST_API_KEY", "").encode("utf-8")
    started = time.perf_counter()
    process = subprocess.Popen(
        command, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=(os.name != "nt"),
    )

    def drain(stream, log_path, capture_markers):
        with log_path.open("wb") as log:
            for line in iter(stream.readline, b""):
                elapsed = time.perf_counter() - started
                marker = MARKERS.get(line.strip()) if capture_markers else None
                if marker:
                    times.setdefault(marker, elapsed)
                else:
                    if secret:
                        line = line.replace(secret, b"[REDACTED KEY]")
                    for question_line in question_lines:
                        if question_line:
                            line = line.replace(
                                question_line, b"[REDACTED QUESTION]"
                            )
                    log.write(line)
                    log.flush()
        stream.close()

    threads = [
        threading.Thread(
            target=drain,
            args=(process.stdout, output_dir / "stdout.log", False),
        ),
        threading.Thread(
            target=drain,
            args=(process.stderr, output_dir / "stderr.log", True),
        ),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        exit_code = process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        timed_out = True
        if os.name != "nt":
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        process.wait()
        exit_code = 124
    wall = time.perf_counter() - started
    for thread in threads:
        thread.join()
    requested, remote = times.get("exec_requested"), times.get("exec_started")
    markers_complete = requested is not None and remote is not None
    return {
        "exit_code": exit_code, "timed_out": timed_out, "wall_seconds": wall,
        "client_before_exec_seconds": requested,
        "exec_startup_observed_seconds": (
            remote - requested if markers_complete else None
        ),
        "remote_to_exit_seconds": (
            wall - remote if remote is not None and not timed_out else None
        ),
        "timing_markers_complete": markers_complete,
    }


def analyze_trace(run_dir, env, timeout):
    target = None
    for name in ("stdout.log", "stderr.log"):
        with (run_dir / name).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 65536))
            tail = stream.read().decode("utf-8", errors="replace")
        target = extract_experiment(tail, env.get("BRAINTRUST_ORG", ""))
        if target:
            break
    if not target:
        return {
            "status": "unavailable",
            "reason": "No valid Braintrust experiment footer in CLI output.",
        }
    started = time.perf_counter()
    try:
        worker = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()),
             "analyze-braintrust", "--url", target["url"]],
            env=env, capture_output=True, text=True, timeout=timeout,
        )
        # Do not echo SDK stderr: HTTP errors may contain sensitive payloads.
        result = json.loads(worker.stdout)
        if not isinstance(result, dict) or result.get("status") not in (
            "available", "partial", "unavailable",
        ):
            raise ValueError("Invalid analysis result")
        if result["status"] == "available" and (
            worker.returncode != 0 or not result.get("span_count")
        ):
            raise ValueError("SDK analysis did not complete successfully")
    except subprocess.TimeoutExpired:
        result = {"status": "unavailable", "reason": "SDK fetch timed out."}
    except (OSError, ValueError):
        result = {"status": "unavailable", "reason": "SDK analysis failed."}
    result["url"] = target["url"]
    result["fetch_analysis_seconds"] = time.perf_counter() - started
    return result


def benchmark_main(argv=None):
    parser = argparse.ArgumentParser(
        prog="performance_benchmark.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Measure the installed kubectl investigate plugin "
            "against CPU Saturn with Braintrust collection by default."
        ),
        epilog=(
            "Required Holmes option after the question: --trace braintrust\n"
            "Required local environment: "
            "BRAINTRUST_API_KEY, BRAINTRUST_ORG\n\n"
            "Use --no-braintrust before the question for timing/vLLM only.\n\n"
            "Standalone read-only Braintrust analysis:\n"
            "  %(prog)s analyze-braintrust (--url URL | --log LOG)"
        ),
    )
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="aks-investigator-cpu")
    parser.add_argument("--release", default="aksi-cpu")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--braintrust-timeout", type=float, default=60)
    parser.add_argument(
        "--no-braintrust", action="store_true",
        help="Explicitly disable trace collection for timing/vLLM-only runs.",
    )
    parser.add_argument(
        "--output", type=Path,
        help="New output directory (must not exist). Defaults to "
        "benchmark-results/cpu-<UTC timestamp>/ at the repository root.",
    )
    parser.add_argument("question")
    parser.add_argument("holmes_args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    if (
        options.runs < 1 or options.timeout <= 0
        or options.braintrust_timeout <= 0
        or not math.isfinite(options.timeout)
        or not math.isfinite(options.braintrust_timeout)
    ):
        parser.error("--runs and --timeout must be positive")
    if os.name == "nt":
        parser.error(
            "This benchmark runner currently requires Linux/macOS."
        )
    if any(arg in ("-i", "--interactive") for arg in options.holmes_args):
        parser.error("Benchmarks require one-shot mode, not --interactive")
    if options.no_braintrust and any(
        arg == "--trace" or arg.startswith("--trace=")
        for arg in options.holmes_args
    ):
        parser.error("--no-braintrust cannot be combined with --trace")
    traced = not options.no_braintrust
    if traced and not trace_requested(options.holmes_args):
        parser.error(
            "Braintrust SDK collection is required: pass --trace braintrust "
            "after the question."
        )
    if traced and not all(os.environ.get(name) for name in (
        "BRAINTRUST_API_KEY", "BRAINTRUST_ORG",
    )):
        parser.error(
            "Benchmark requires local BRAINTRUST_API_KEY and BRAINTRUST_ORG."
        )
    # Validate the metrics dependency before any expensive investigation.
    parse_metrics("")
    kubectl = shutil.which("kubectl")
    if not kubectl:
        parser.error("kubectl must be on PATH")
    if options.output is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        options.output = (
            Path(__file__).resolve().parents[1]
            / "benchmark-results" / f"cpu-{timestamp}"
        )
    options.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    print(f"Benchmark output: {options.output.resolve()}", flush=True)
    base = [
        kubectl, "--context", options.context, "--request-timeout=30s",
        "-n", options.namespace,
    ]

    def read(*args):
        return subprocess.check_output(
            base + list(args), text=True, timeout=40
        )

    def single_pod(selector, label):
        pods = json.loads(
            read("get", "pods", "-l", selector, "-o", "json")
        )["items"]
        if len(pods) != 1:
            raise RuntimeError(
                f"Expected exactly one {label} pod for attributable metrics."
            )
        return pods[0]

    pod = single_pod("app=saturn-vllm", "Saturn")
    holmes_pod = single_pod("app=holmes", "Holmes")
    container = next(
        c for c in pod["spec"]["containers"] if c["name"] == "vllm"
    )
    if "vllm-openai-cpu" not in container["image"] or any(
        "nvidia.com/gpu" in container.get("resources", {}).get(key, {})
        for key in ("requests", "limits")
    ):
        raise RuntimeError(
            "Refusing to benchmark a non-CPU Saturn deployment."
        )
    if not any(
        c["type"] == "Ready" and c["status"] == "True"
        for c in pod["status"].get("conditions", [])
    ):
        raise RuntimeError("CPU Saturn pod is not Ready.")
    pod_name = pod["metadata"]["name"]
    metrics_path = (
        f"/api/v1/namespaces/{options.namespace}/pods/"
        f"{pod_name}:8000/proxy/metrics"
    )

    def identities():
        return {
            "saturn": pod_identity(
                single_pod("app=saturn-vllm", "Saturn"), "vllm"
            ),
            "holmes": pod_identity(
                single_pod("app=holmes", "Holmes"), "holmes"
            ),
        }

    def scrape(destination):
        text = read("get", "--raw", metrics_path)
        destination.write_text(text)
        return text

    env = os.environ.copy()
    if not traced:
        env.pop("BRAINTRUST_API_KEY", None)
        env.pop("BRAINTRUST_ORG", None)
    env.update(
        INVESTIGATOR_NAMESPACE=options.namespace,
        INVESTIGATOR_RELEASE=options.release,
        INVESTIGATOR_TIMING="1",
        INVESTIGATOR_HOME="/tmp",
    )
    # Ensure the measured plugin is the instrumented checkout, not an old
    # installed copy. kubectl still performs actual plugin dispatch.
    plugin_dir = Path(__file__).resolve().parents[1] / "kubectl-plugin"
    env["PATH"] = str(plugin_dir) + os.pathsep + env.get("PATH", "")
    # Ignore inherited targeting and remote HOME overrides.
    env.pop("INVESTIGATOR_DEPLOY", None)
    env.pop("INVESTIGATOR_SELECTOR", None)
    # Use a private kubeconfig with only the selected context. Plugins don't
    # receive kubectl's --context flag; don't change the user's context.
    kubeconfig = options.output / ".benchmark-kubeconfig"
    fd = os.open(kubeconfig, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            subprocess.run(
                [
                    kubectl, "--context", options.context, "config", "view",
                    "--minify", "--flatten", "--raw",
                ],
                stdout=stream, check=True, timeout=30,
            )
        env["KUBECONFIG"] = str(kubeconfig.resolve())
        command = [
            kubectl, "investigate", options.question,
        ] + options.holmes_args
        report = {
            "context": options.context,
            "namespace": options.namespace,
            "release": options.release,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "saturn_pod": pod_name, "saturn_uid": pod["metadata"]["uid"],
            "saturn_node": pod["spec"].get("nodeName"),
            "image": container["image"],
            "saturn": pod_identity(pod, "vllm"),
            "holmes": pod_identity(holmes_pod, "holmes"),
            "remote_home": "/tmp",
            "resources": container.get("resources", {}), "runs": [],
            "braintrust_enabled": traced,
            "braintrust_required": traced,
            "status": "running",
            "notes": [
                "First observed run is not guaranteed cold: pods/caches "
                "are not restarted or cleared.",
                "Preflight and metric scrapes warm client authentication; "
                "not a cold kubeconfig test.",
                "Exec startup is remote-shell marker receipt minus local "
                "exec-request marker receipt (includes network/stdio delay).",
                "Remote-to-exit includes Python imports, toolset init, "
                "tracing, agent work and stream teardown.",
                "vLLM deltas include ALL traffic on this pod; use an "
                "otherwise idle deployment.",
                "Non-TTY one-shot measurements; literal question lines are "
                "redacted, but logs may still contain sensitive data, "
                "including reformatted or paraphrased questions.",
                "Local timeout/interruption does not guarantee remote exec "
                "stops; its metrics are not attributable and runs stop.",
                "Braintrust analysis runs after command timing and vLLM "
                "scrapes; its fetch duration is reported separately.",
                "When tracing is required, missing or partial Braintrust "
                "data fails collection; other measurements are saved.",
            ],
        }
        for index in range(1, options.runs + 1):
            run_dir = options.output / f"run-{index:02d}"
            run_dir.mkdir(mode=0o700)
            pods_before = identities()
            for label, identity in pods_before.items():
                if (
                    identity["uid"] != report[label]["uid"]
                    or identity["image"] != report[label]["image"]
                    or not identity["running"]
                    or identity["restart_count"] is None
                ):
                    raise RuntimeError(
                        f"{label} pod changed or main container "
                        "is not running."
                    )
            before = scrape(run_dir / "before.prom")
            print(
                f"CPU investigation {index}/{options.runs} started",
                flush=True,
            )
            timing = measure_command(command, env, run_dir, options.timeout)
            entry = {
                "run": index,
                "label": "first_observed" if index == 1 else "subsequent",
                "timing": timing,
                "pods_before": pods_before,
            }
            if timing["timed_out"]:
                entry["vllm"] = {
                    "valid": False,
                    "warning": (
                        "Local timeout/interruption; remote exec may still "
                        "be running. Metrics are not attributable; stopping "
                        "further runs. Verify remote completion before retry."
                    ),
                }
            else:
                try:
                    try:
                        after = scrape(run_dir / "after.prom")
                    finally:
                        entry["pods_after"] = identities()
                    for label, identity in entry["pods_after"].items():
                        if (
                            identity != pods_before[label]
                            or not identity["running"]
                        ):
                            raise RuntimeError(
                                f"{label} pod/main container changed, "
                                "restarted or stopped during measurement."
                            )
                    if timing["exit_code"] != 0:
                        raise RuntimeError(
                            "Investigation command failed; "
                            "metrics are invalid."
                        )
                    entry["vllm"] = summarize_metrics(
                        before, after, timing["wall_seconds"]
                    )
                except (
                    RuntimeError, subprocess.SubprocessError, ValueError,
                ) as error:
                    entry["vllm"] = {"valid": False, "warning": str(error)}
            entry["braintrust"] = (
                {"status": "disabled", "reason": "Explicit --no-braintrust"}
                if not traced else
                {"status": "unavailable", "reason": "Command timed out."}
                if timing["timed_out"] else analyze_trace(
                    run_dir, env, options.braintrust_timeout
                )
            )
            entry["collection_complete"] = (
                timing["exit_code"] == 0 and not timing["timed_out"]
                and timing["timing_markers_complete"]
                and entry["vllm"]["valid"]
                and (
                    not traced or
                    entry["braintrust"]["status"] == "available"
                )
            )
            report["runs"].append(entry)
            report["status"] = (
                "failed" if not entry["collection_complete"] else
                "complete" if index == options.runs else "running"
            )
            successful = [
                r["timing"]["wall_seconds"] for r in report["runs"][1:]
                if r["collection_complete"]
            ]
            report["subsequent_wall_median_seconds"] = (
                statistics.median(successful) if successful else None
            )
            summary_path = options.output / "summary.json"
            summary_path.write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(f"Summary saved: {summary_path.resolve()}", flush=True)
            print(json.dumps(entry, indent=2), flush=True)
            if timing["timed_out"]:
                return 124
            if timing["exit_code"] != 0:
                return timing["exit_code"]
            if not entry["collection_complete"]:
                print(
                    "Benchmark collection incomplete; see summary.json "
                    "for timing, vLLM and Braintrust status.",
                    file=sys.stderr,
                )
                return 1
        return 0
    finally:
        kubeconfig.unlink(missing_ok=True)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "analyze-braintrust":
        return analysis_main(argv[1:])
    return benchmark_main(argv)


if __name__ == "__main__":
    sys.exit(main())
