#!/usr/bin/env python3
"""Real, sequential CPU-only sweep; one untraced investigation per fresh pod.

Requires an idle release and its matching local chart. No dry-run/test mode.
Only the original Helm revision is rolled back; concurrent changes block it.
"""

import argparse
from copy import deepcopy
import csv
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time


ENV = {
    "VLLM_CPU_OMP_THREADS_BIND", "VLLM_CPU_KVCACHE_SPACE", "OMP_NUM_THREADS",
}
FILES = {
    "cpu.stat": {"usage_usec", "user_usec", "system_usec", "nr_periods",
                 "nr_throttled", "throttled_usec"},
    "cpu.max": None, "cpuset.cpus.effective": None,
    "memory.current": None, "memory.peak": None,
    "memory.events": {
        "low", "high", "max", "oom", "oom_kill", "oom_group_kill",
    },
    "memory.stat": {"inactive_file"},
}
COLUMNS = [
    "cpu", "status", "wall_seconds", "exec_startup_seconds",
    "ttft_mean_seconds",
    "prefill_seconds", "decode_seconds", "requests", "prompt_tokens",
    "generation_tokens", "prefix_ratio", "working_gib", "throttle_ratio",
    "rollout_seconds",
]
NOTES = [
    "One sample/level: variability is unknown. Use an otherwise idle release.",
    "Fresh vLLM/emptyDir caches; OS caches and authentication stay warm.",
    "Each workload stays on its original node, not necessarily the same "
    "physical cores; cpusets are recorded.",
    "No inference warmup. Model startup is excluded from command wall time; "
    "rollout_seconds is separate.",
    "Cgroup timestamps bracket incidental client/preflight/scrape overhead.",
    "throttle_ratio = delta(nr_throttled)/delta(nr_periods), NOT wall-time %.",
    "working_gib estimates max(memory.current - inactive_file, 0)/2**30.",
    "vLLM metrics cover ALL pod traffic, including other callers.",
    "Live workload specs are checked after upgrade; no full-chart pre-render.",
    "Revision guards are not atomic locks. Do not operate the release "
    "concurrently. Uncertain upgrade ownership blocks automatic rollback.",
    "Timeout/interruption may leave remote exec running; the sweep stops.",
    "The existing benchmark redacts literal questions; restricted logs "
    "can still contain sensitive tool responses.",
    "The existing benchmark temporarily copies a private minified kubeconfig "
    "and normally removes it in finally; this wrapper does not copy secrets.",
]


class SweepError(Exception):
    """Messages are fixed diagnostic codes, never command output."""


def require(condition, code):
    if not condition:
        raise SweepError(code)


def now():
    return datetime.now(timezone.utc).isoformat()


def failure(error):
    result = {"error_type": type(error).__name__}
    if isinstance(error, SweepError):
        result["error_code"] = str(error)
    return result


def command(argv, timeout=60, log=None, check=True):
    # Never echo command stderr, argv, Helm values, or subprocess exceptions.
    with subprocess.Popen(argv, stdin=subprocess.DEVNULL, text=True,
                          stdout=log if log else subprocess.PIPE,
                          stderr=log if log else subprocess.PIPE,
                          start_new_session=True) as process:
        try:
            out, _ = process.communicate(timeout=timeout)
        except BaseException:
            # Let the benchmark clean up its separately-sessioned plugin first.
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=45)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=10)
            raise
        if check:
            require(process.returncode == 0, "CommandFailed")
        return process.returncode, out or ""


def sanitized(spec):
    return {kind: [{"name": c["name"], "image": c["image"],
                    "args": c.get("args", []),
                    "resources": c.get("resources", {}),
                    "cpu_env": [e for e in c.get("env", [])
                                if e["name"] in ENV and "value" in e]}
                   for c in spec.get(kind, [])]
            for kind in ("containers", "initContainers")}


def invariant(spec, saturn):
    # Full spec/env comparisons stay in memory, never in reports.
    spec = deepcopy(spec)
    selectors = spec.setdefault("nodeSelector", {})
    selectors.pop("kubernetes.io/hostname", None)
    if not selectors:
        spec.pop("nodeSelector")
    if saturn:
        vllm = next(c for c in spec["containers"] if c["name"] == "vllm")
        for field in ("requests", "limits"):
            vllm["resources"][field].pop("cpu", None)
    return spec


def publish(root, report):
    report["updated_at"] = now()
    (root / "summary.json").write_text(json.dumps(report, indent=2) + "\n")
    restore = report["restoration"]
    extra = {"restoration_success": restore.get("success"),
             "restoration_revision": restore.get("revision"),
             "restoration_error_type": restore.get("error_type")}
    with (root / "comparison.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=COLUMNS + ["error_type"] + list(extra),
            extrasaction="ignore")
        writer.writeheader()
        writer.writerows({**row, **extra} for row in report["levels"])
    lines = ["# CPU sweep", "", f"Status: {report['status']}", "",
             "| " + " | ".join(COLUMNS) + " |",
             "| " + " | ".join(["---"] * len(COLUMNS)) + " |"]
    for row in report["levels"]:
        values = [row.get(k) if row["status"] == "complete" or
                  k in ("cpu", "status") else None for k in COLUMNS]
        cells = ["—" if v is None else f"{v:.4f}" if isinstance(v, float)
                 else str(v) for v in values]
        lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "Failure: " + json.dumps({
        k: report[k] for k in ("error_type", "error_code") if k in report})]
    lines += ["", "## Restoration", "", json.dumps(report["restoration"]), "",
              "## Caveats", "", *["- " + note for note in NOTES]]
    (root / "comparison.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", default="aks-investigator-cpu")
    parser.add_argument("--release", default="aksi-cpu")
    parser.add_argument("--levels", nargs="+", type=int,
                        default=[6, 7, 8, 9, 10, 12])
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--rollout-timeout", type=int, default=1200)
    parser.add_argument("question", nargs="?", default=(
        "How many nodes are in the cluster? "
        "Use read-only tools and answer in one sentence."))
    args = parser.parse_args()
    if (args.timeout <= 0 or args.rollout_timeout <= 0 or
            any(n not in (6, 7, 8, 9, 10, 12) for n in args.levels) or
            len(set(args.levels)) != len(args.levels)):
        parser.error("Positive timeouts; distinct levels from 6,7,8,9,10,12")
    os.umask(0o077)
    repo = Path(__file__).resolve().parents[1]
    tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root = repo / "benchmark-results" / ("cpu-sweep-" + tag)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    print(f"CPU sweep output: {root}", flush=True)
    report = {
        "started_at": now(), "context": args.context,
        "namespace": args.namespace, "release": args.release,
        "requested_levels": args.levels, "status": "running",
        "levels": [], "notes": NOTES,
        "restoration": {"attempted": False, "success": None},
    }
    kube = ["kubectl", "--context", args.context, "-n", args.namespace]
    helm = ["--kube-context", args.context, "-n", args.namespace]
    names = {"saturn": "saturn-vllm", "holmes": args.release + "-holmes"}
    expected, pending, external, attempted = None, None, False, False
    original, row = {}, None

    def get(*parts):
        return json.loads(command(kube + [
            "--request-timeout=30s", "get", *parts, "-o", "json"])[1])

    def helm_status():
        return json.loads(command([
            "helm", "status", args.release, *helm, "-o", "json"])[1])

    def guard():
        nonlocal expected, pending, external
        require(not external, "ConcurrentHelmRevision")
        state = helm_status()
        revision = int(state["version"])
        if pending and revision == expected + 1:
            # Helm can replace our description on failure; do not guess.
            require(state["info"].get("description") == pending,
                    "UpgradeOwnershipUncertain")
            expected, pending = revision, None
        elif revision != expected:
            external = True
            raise SweepError("ConcurrentHelmRevision")
        return state

    def snapshot():
        deployments, identities = {}, {}
        for label, name in names.items():
            deployment = get("deployment", name)
            meta, spec = deployment["metadata"], deployment["spec"]
            status = deployment.get("status", {})
            annotations = meta.get("annotations", {})
            require(
                annotations.get("meta.helm.sh/release-name") == args.release
                and annotations.get("meta.helm.sh/release-namespace")
                == args.namespace, "ReleaseOwnershipMismatch")
            require(spec.get("replicas") == 1 and
                    status.get("observedGeneration", 0) >= meta["generation"]
                    and all(status.get(k) == 1 for k in (
                        "replicas", "updatedReplicas", "readyReplicas",
                        "availableReplicas")),
                    "DeploymentNotSettledSingleReplica")
            app = "saturn-vllm" if label == "saturn" else "holmes"
            pods = get("pods", "-l", "app=" + app)["items"]
            require(len(pods) == 1, "ExpectedOnePodIncludingTerminating")
            pod = pods[0]
            ready = any(c["type"] == "Ready" and c["status"] == "True"
                        for c in pod.get("status", {}).get("conditions", []))
            require(ready and pod["status"].get("phase") == "Running" and
                    not pod["metadata"].get("deletionTimestamp"),
                    "PodNotRunningReady")
            owner = next(
                o for o in pod["metadata"]["ownerReferences"]
                if o.get("controller"))
            require(owner["kind"] == "ReplicaSet", "UnexpectedPodOwner")
            rs = get("replicaset", owner["name"])
            require(rs["metadata"]["uid"] == owner["uid"] and any(
                o.get("controller") and o["uid"] == meta["uid"]
                for o in rs["metadata"].get("ownerReferences", [])),
                "PodDeploymentMismatch")
            deployments[label] = deployment
            identities[label] = {
                "pod": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                "node": pod["spec"]["nodeName"], "containers": [
                    {k: c.get(k) for k in (
                        "name", "containerID", "imageID", "restartCount")}
                    for c in pod["status"].get("containerStatuses", [])],
            }
        return deployments, identities

    def cgroup(identity, directory, label, cpu):
        started = now()
        shell = ("set -eu; for f in " + " ".join(FILES) + "; do "
                 "if [ -r /sys/fs/cgroup/$f ]; then printf '[%s]\\n' \"$f\"; "
                 "if [ \"$f\" = memory.stat ]; then "
                 "grep '^inactive_file ' /sys/fs/cgroup/$f; "
                 "else cat /sys/fs/cgroup/$f; fi; fi; done")
        raw = command(kube + [
            "--request-timeout=45s", "exec", "pod/" + identity["pod"],
            "-c", "vllm", "--", "sh", "-c", shell], timeout=60)[1]
        values, current = {}, None
        for line in raw.splitlines():
            if line.startswith("[") and line.endswith("]"):
                current = line[1:-1]
                require(current in FILES, "UnexpectedCgroupFile")
                values[current] = {} if FILES[current] else ""
            elif current and FILES[current]:
                key, value = line.split()
                if key in FILES[current]:
                    values[current][key] = int(value)
            else:
                require(
                    current is not None and
                    bool(re.fullmatch(r"[0-9,\- max]+", line)),
                    "InvalidCgroupValue")
                values[current] = line
        quota, period = map(int, values["cpu.max"].split())
        require(period > 0 and quota == cpu * period, "CgroupCPUQuotaMismatch")
        (directory / (label + "-cgroup.txt")).write_text(raw)
        return {"started_at": started, "finished_at": now(), "values": values}

    try:
        python = repo / ".venv/bin/python"
        require(python.is_file(), "MissingConfiguredPython")
        initial = helm_status()
        expected = int(initial["version"])
        report["original_revision"] = expected
        report["original_helm_status"] = initial["info"]["status"]
        require(
            initial["info"]["status"] == "deployed",
            "InitialHelmStatusNotDeployed")
        # Helm status includes config: inspect in memory, never serialize it.
        config = initial.get("config") or {}
        saturn = config.get("saturn") or {}
        require(
            not saturn.get("gpu", False) and
            saturn.get("enabled", True) and not config.get("modelList"),
            "HelmConfigNotCPUSaturn")
        del initial, config, saturn
        original, previous = snapshot()
        baseline = {
            k: d["spec"]["template"]["spec"] for k, d in original.items()}
        vllm = next(
            c for c in baseline["saturn"]["containers"]
            if c["name"] == "vllm")
        dtype = re.findall(
            r"--dtype(?:=|\s+)(\S+)", " ".join(vllm.get("args", [])))
        require(
            "vllm-openai-cpu" in vllm["image"] and len(dtype) == 1 and
            dtype[0] in ("auto", "bfloat16", "float32"),
            "UnsupportedCPUImageOrDtype")
        require(not any(
            "nvidia.com/gpu" in c.get("resources", {}).get(r, {})
            for s in baseline.values()
            for kind in ("containers", "initContainers")
            for c in s.get(kind, [])
            for r in ("requests", "limits")), "GPUResourcePresent")
        nodes = {k: p["node"] for k, p in previous.items()}
        report["baseline"] = {k: {"deployment": names[k], "replicas": 1,
                                  "pod": previous[k], **sanitized(s)}
                              for k, s in baseline.items()}
        publish(root, report)
        for cpu in args.levels:
            row = {
                "cpu": cpu, "status": "running", "comparable": False,
                "started_at": now(), "measurement": f"cpu-{cpu}/measurement",
            }
            report["levels"].append(row)
            directory = root / f"cpu-{cpu}"
            directory.mkdir(mode=0o700)
            print(f"Starting CPU {cpu}", flush=True)
            require(
                guard()["info"]["status"] == "deployed",
                "HelmStatusNotDeployed")
            pending = f"cpu-sweep-{tag}-{cpu}"
            prior = expected
            attempted = True
            started = time.monotonic()
            try:
                hostname = "nodeSelector.kubernetes\\.io/hostname="
                command(["helm", "upgrade", args.release, str(repo / "helm"),
                         *helm, "--reuse-values",
                         "--description", pending,
                         "--set-string",
                         f"saturn.resources.cpu.requests.cpu={cpu}",
                         "--set-string",
                         f"saturn.resources.cpu.limits.cpu={cpu}",
                         "--set-string",
                         "saturn." + hostname + nodes["saturn"],
                         "--set-string", hostname + nodes["holmes"]],
                        timeout=args.rollout_timeout + 60)
            finally:
                state = guard()
                row["helm_revision"] = expected
                row["upgrade_reconciliation"] = (
                    "owned_revision" if expected != prior
                    else "no_revision_advance_uncertain")
            require(expected == prior + 1 and
                    state["info"]["status"] == "deployed",
                    "UpgradeNotConfirmed")
            for label, name in names.items():
                # Delete-watch only, never DELETE: avoid stale Available=True.
                command(kube + ["wait", "--for=delete",
                                "pod/" + previous[label]["pod"],
                                f"--timeout={args.rollout_timeout}s"],
                        timeout=args.rollout_timeout + 30)
                generation = get("deployment", name)["metadata"]["generation"]
                for condition in (
                    "jsonpath={.status.observedGeneration}=" + str(generation),
                    "condition=Available",
                ):
                    command(kube + ["wait", "--for=" + condition,
                                    "deployment/" + name,
                                    f"--timeout={args.rollout_timeout}s"],
                            timeout=args.rollout_timeout + 30)
            command(
                kube + ["rollout", "status", "deployment/" + names["holmes"],
                        f"--timeout={args.rollout_timeout}s"],
                timeout=args.rollout_timeout + 30)
            live, identities = snapshot()
            row["rollout_seconds"] = time.monotonic() - started
            row["pods_before"] = identities
            for label, deployment in live.items():
                spec = deployment["spec"]["template"]["spec"]
                require(
                    invariant(spec, label == "saturn") ==
                    invariant(baseline[label], label == "saturn"),
                    "UnexpectedWorkloadSpecChange")
                require(identities[label]["node"] == nodes[label],
                        "NodeChanged")
                require(
                    identities[label]["uid"] != previous[label]["uid"],
                    "PodNotFresh")
            saturn_spec = live["saturn"]["spec"]["template"]["spec"]
            current = next(
                c for c in saturn_spec["containers"] if c["name"] == "vllm")
            require(all(
                str(current["resources"][r]["cpu"]) in
                (str(cpu), f"{cpu * 1000}m")
                for r in ("requests", "limits")), "CPUResourcesMismatch")
            row["resources"] = current["resources"]
            try:
                logs = command(kube + [
                    "logs", "pod/" + identities["saturn"]["pod"], "-c", "vllm",
                    "--tail=2000", "--limit-bytes=1048576"])[1]
                # Safe fragments, never whole engine configs/general env.
                fragments = re.findall(
                    r"(?:dtype[=:]\s*['\"]?(?:torch\.)?"
                    r"(?:auto|bfloat16|float32|float16)\b|"
                    r"VLLM_CPU_OMP_THREADS_BIND[=:]\s*['\"]?[0-9,;:\-]+|"
                    r"(?:CPU binding|CPU affinity)[=:]\s*[0-9,;:\-]+|"
                    r"(?:Model loading|Loading model weights|Loading weights)"
                    r" took [0-9.]+ (?:GiB|GB|seconds|s)\b)",
                    logs, re.IGNORECASE)[:64]
                (directory / "startup.log").write_text(
                    "\n".join(fragments) + "\n")
                row["startup_log_fragments"] = len(fragments)
            except Exception as error:
                row["startup_log_error"] = failure(error)
            row["cgroup_before"] = cgroup(
                identities["saturn"], directory, "before", cpu)
            with (directory / "benchmark.log").open("w") as log:
                code, _ = command([
                    str(python),
                    str(repo / "scripts/performance_benchmark.py"),
                    "--context", args.context, "--namespace", args.namespace,
                    "--release", args.release, "--runs", "1",
                    "--timeout", str(args.timeout), "--no-braintrust",
                    "--output", str(directory / "measurement"), args.question,
                    "--model", "saturn-vllm"],
                    timeout=args.timeout + 600, log=log, check=False)
            row["benchmark_exit_code"] = code
            row["cgroup_after"] = cgroup(
                identities["saturn"], directory, "after", cpu)
            _, row["pods_after"] = snapshot()
            require(row["pods_after"] == identities,
                    "PodChangedDuringMeasurement")
            before, after = (
                row["cgroup_" + p]["values"] for p in ("before", "after"))
            delta = {k: after["cpu.stat"][k] - before["cpu.stat"][k]
                     for k in FILES["cpu.stat"]}
            require(all(v >= 0 for v in delta.values()), "CgroupCounterReset")
            memory_events = {k: after["memory.events"][k] - v
                             for k, v in before["memory.events"].items()}
            require(all(v >= 0 for v in memory_events.values()),
                    "MemoryCounterReset")
            row.update(
                cpu_usage_seconds_delta=delta["usage_usec"] / 1e6,
                throttled_seconds_delta=delta["throttled_usec"] / 1e6,
                nr_periods_delta=delta["nr_periods"],
                nr_throttled_delta=delta["nr_throttled"],
                throttle_ratio=(delta["nr_throttled"] / delta["nr_periods"]
                                if delta["nr_periods"] else None),
                memory_before_gib=int(before["memory.current"]) / 2**30,
                memory_after_gib=int(after["memory.current"]) / 2**30,
                working_gib=max(0, int(after["memory.current"]) -
                                after["memory.stat"]["inactive_file"]) / 2**30,
                memory_events_delta=memory_events)
            measurement = json.loads(
                (directory / "measurement/summary.json").read_text())
            require(
                len(measurement["runs"]) == 1, "UnexpectedBenchmarkRunCount")
            result = measurement["runs"][0]
            row["collection_complete"] = bool(
                result.get("collection_complete"))
            timing, metrics = result["timing"], result["vllm"]
            # Failed timings remain diagnostic, never comparable.
            row.update(
                wall_seconds=timing["wall_seconds"],
                exec_startup_seconds=timing["exec_startup_observed_seconds"])
            require(code == 0 and row["collection_complete"] and
                    metrics.get("valid") and
                    not measurement.get("braintrust_enabled"),
                    "BenchmarkCollectionIncomplete")
            counters, latencies = metrics["counters"], metrics["latencies"]
            for key, metric, statistic in (
                ("ttft_mean_seconds", "time_to_first_token_seconds", "mean"),
                ("prefill_seconds", "request_prefill_time_seconds", "sum"),
                ("decode_seconds", "request_decode_time_seconds", "sum"),
            ):
                row[key] = latencies[metric][statistic + "_seconds"]
            row.update(
                requests=counters["request_success_total"],
                prompt_tokens=counters["prompt_tokens_total"],
                generation_tokens=counters["generation_tokens_total"],
                prefix_ratio=metrics["prefix_cache_hit_ratio"])
            guard()
            row.update(status="complete", comparable=True, finished_at=now())
            previous = identities
            publish(root, report)
            print(
                f"Finished CPU {cpu} wall {row['wall_seconds']:.3f}s "
                f"TTFT {row['ttft_mean_seconds']}s", flush=True)
        report["status"] = "complete"
    except (Exception, KeyboardInterrupt) as error:
        report.update(
            status="interrupted" if isinstance(error, KeyboardInterrupt)
            else "failed", **failure(error))
        if row is not None:
            row.update(status=report["status"], comparable=False,
                       finished_at=now(), **failure(error))
    finally:
        if attempted:
            restoration = report["restoration"] = {
                "attempted": False, "success": False, "revision": None,
                "target_revision": report["original_revision"],
            }
            handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                guard()  # Never overwrite changed/uncertain release ownership.
                rollback_from = expected
                restoration["attempted"] = True
                rollback_error = None
                try:
                    command([
                        "helm", "rollback", args.release,
                        str(report["original_revision"]), *helm,
                        "--wait", "--timeout", "20m"], timeout=1260)
                except Exception as error:
                    rollback_error = error
                    restoration["command_error"] = failure(error)
                # Reconcile even a failed/timed-out rollback; don't repeat it.
                restored_status = helm_status()
                restoration["revision"] = int(restored_status["version"])
                restored = {k: get("deployment", n) for k, n in names.items()}
                restoration["resources"] = sanitized(
                    restored["saturn"]["spec"]["template"]["spec"])
                if rollback_error is not None:
                    raise rollback_error
                require(restoration["revision"] == rollback_from + 1 and
                        restored_status["info"]["status"] == "deployed",
                        "RollbackStatusMismatch")
                require(all(restored[k]["spec"]["template"] ==
                            original[k]["spec"]["template"] for k in names),
                        "RestoredTemplateMismatch")
                snapshot()  # Settled replicas/readiness, no old pods.
                restoration.update(success=True, error_type=None)
            except (Exception, KeyboardInterrupt) as error:
                restoration.update(blocked=not restoration["attempted"],
                                   needs_manual=True, **failure(error))
                if report["status"] == "complete":
                    report["status"] = "restoration_failed"
            finally:
                signal.signal(signal.SIGINT, handler)
        publish(root, report)
    restore = report["restoration"]
    ok = report["status"] == "complete" and restore.get("success") is True
    print(
        f"Sweep {report['status']}; restored={restore.get('success')} "
        f"revision={restore.get('revision')}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:
        print(json.dumps(failure(error)), flush=True)
        raise SystemExit(1) from None
