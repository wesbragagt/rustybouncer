#!/usr/bin/env python3
"""Native pgbench concurrency benchmark against an isolated replication cluster.

Requires Docker, Python 3 stdlib, postgres:16 and a compiled Rust router.
Every measured transaction is one SQL query. Connections remain persistent.
"""
import argparse
import concurrent.futures
import datetime
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import failover as fixture

WORKLOADS = {
    "select": "\\set id random(1, 10000)\nSELECT id, payload FROM continuity WHERE id = :id;\n",
    "cte": "\\set id random(1, 10000)\nWITH item AS MATERIALIZED (SELECT id, payload FROM continuity WHERE id = :id) SELECT id, payload FROM item;\n",
}


def run(args, **kwargs):
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)


def percentile(sorted_values, percent):
    if not sorted_values:
        return None
    return sorted_values[max(0, math.ceil(percent / 100 * len(sorted_values)) - 1)] / 1000


def process_usage(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        # Fields start with original field 3 (state).
        cpu = (int(fields[11]) + int(fields[12])) / ticks
        rss = int(fields[21]) * os.sysconf("SC_PAGE_SIZE") / 1024**2
        return {"cpu_seconds": cpu, "rss_mib": rss}
    except (OSError, ValueError, IndexError):
        return {"cpu_seconds": 0, "rss_mib": None}


def pgbench(cluster, directory, workload, concurrency, duration, target, measured=True):
    port = cluster.proxy_port if target == "router" else fixture.port(cluster.nodes[1])
    stem = f"{target}-{workload}-c{concurrency}" + ("" if measured else "-warmup")
    container = "rustybouncer-load-" + uuid.uuid4().hex[:10]
    command = ["docker", "run", "--rm", "--name", container, "--network", "host", "-v", f"{directory}:/bench",
               fixture.IMAGE, "pgbench", "-n", "-M", "simple", "-U", "postgres",
               "-h", "127.0.0.1", "-p", str(port), "-c", str(concurrency),
               "-j", str(min(concurrency, 8)), "-T", str(duration), "-f", f"/bench/{workload}.sql", "postgres"]
    if measured:
        command += ["-l", "--sampling-rate", "0.1", "--log-prefix", f"/bench/{stem}.latency"]
    before = process_usage(cluster.process.pid)
    began = time.monotonic()
    try:
        result = run(command, timeout=duration + 30)
    finally:
        # Killing the Docker CLI alone does not guarantee the container stopped.
        run(["docker", "rm", "-f", container], timeout=10)
    elapsed = time.monotonic() - began
    after = process_usage(cluster.process.pid)
    if not measured:
        if result.returncode:
            raise RuntimeError(f"Warmup failed: {result.stdout}")
        return None
    (directory / f"{stem}.stdout.txt").write_text(result.stdout)
    latencies, sampled_errors = [], 0
    for path in directory.glob(f"{stem}.latency.*"):
        with path.open() as source, gzip.open(str(path) + ".gz", "wt") as compressed:
            for line in source:
                compressed.write(line)
                fields = line.split()
                if len(fields) >= 3:
                    try:
                        latencies.append(float(fields[2]))
                    except ValueError:
                        sampled_errors += 1
        path.unlink()
    latencies.sort()
    def match(pattern, default=None):
        found = re.search(pattern, result.stdout)
        return found.group(1) if found else default
    successful = int(match(r"number of transactions actually processed:\s*(\d+)", "0"))
    failed = int(match(r"number of failed transactions:\s*(\d+)", "0"))
    tps = match(r"tps = ([\d.]+) \(without initial connection time\)")
    error_lines = [line for line in result.stdout.splitlines()
                   if any(token in line.lower() for token in ("error:", "fatal:", "failed:", "aborted"))]
    return {"target": target, "workload": workload, "concurrency": concurrency,
            "duration_requested_s": duration, "wall_elapsed_s": elapsed,
            "successful_transactions": successful, "failed_transactions": failed,
            "successful_tps": float(tps) if tps else None,
            "p50_ms": percentile(latencies, 50), "p95_ms": percentile(latencies, 95),
            "p99_ms": percentile(latencies, 99), "latency_samples": len(latencies),
            "sampled_errors": sampled_errors, "process_exit_code": result.returncode,
            "setup_or_process_error_lines": error_lines,
            "router_cpu_percent_of_one_core": (after["cpu_seconds"] - before["cpu_seconds"]) / elapsed * 100,
            "router_rss_end_mib": after["rss_mib"], "command": command}


def overload(cluster, clients=80):
    def connect(_):
        try:
            return fixture.Pg(cluster.proxy_port), None
        except Exception as exc:
            return None, {"type": type(exc).__name__, "message": str(exc)}
    opened = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=clients) as executor:
            results = list(executor.map(connect, range(clients)))
        opened = [client for client, error in results if client]
        errors = [error for client, error in results if error]
        query_successes, query_errors = 0, []
        for client in opened:
            try:
                assert client.query("SELECT 1") == [["1"]]
                query_successes += 1
            except Exception as exc:
                query_errors.append({"type": type(exc).__name__, "message": str(exc)})
        return {"attempted_connections": clients, "established_connections": len(opened),
                "setup_failures": len(errors), "setup_error_details": errors,
                "established_client_query_successes": query_successes,
                "established_client_query_error_details": query_errors}
    finally:
        for client in opened:
            client.close()


def main():
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "target/release/rustybouncer")
    parser.add_argument("--levels", default="1,4,8,16,32,64")
    parser.add_argument("--seconds", type=int, default=4)
    parser.add_argument("--warmup-seconds", type=int, default=1)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    levels = [int(value) for value in args.levels.split(",")]
    if any(level < 1 or level > 64 for level in levels) or args.seconds < 1 or args.warmup_seconds < 1:
        parser.error("levels must be 1..64 and durations positive")
    binary = args.binary.resolve()
    if not binary.is_file():
        parser.error(f"binary does not exist: {binary}; run cargo build --release --locked")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = (args.output or ROOT / "benchmarks/runs" / stamp).resolve()
    if directory.exists() and any(directory.iterdir()):
        parser.error(f"output directory is not empty: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    fixture.BINARY = binary
    cluster = fixture.Cluster()
    results = {"started_at_utc": stamp, "binary": str(binary),
               "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
               "platform": platform.platform(), "cpu_count": os.cpu_count(),
               "cpu_info": run(["lscpu"]).stdout, "memory_info": Path("/proc/meminfo").read_text(),
               "pgbench_version": run(["docker", "run", "--rm", fixture.IMAGE, "pgbench", "--version"]).stdout.strip(),
               "levels": levels, "seconds": args.seconds, "warmup_seconds": args.warmup_seconds,
               "sampling_rate": 0.1, "workloads": WORKLOADS, "measurements": []}
    try:
        cluster.start()
        shutil.copy(Path(cluster.directory.name) / "config.toml", directory / "router-config.toml")
        fixture.sql(cluster.nodes[0], "INSERT INTO continuity SELECT i, repeat('x', 64) FROM generate_series(1,10000) i")
        for replica in cluster.nodes[1:]:
            fixture.eventually(lambda replica=replica: fixture.sql(replica, "SELECT count(*) FROM continuity") == "10000")
        for name, script in WORKLOADS.items():
            (directory / f"{name}.sql").write_text(script)
        for workload in WORKLOADS:
            for concurrency in levels:
                for target in ("direct_replica", "router"):
                    pgbench(cluster, directory, workload, concurrency, args.warmup_seconds, target, False)
                    measured = pgbench(cluster, directory, workload, concurrency, args.seconds, target)
                    results["measurements"].append(measured)
                    (directory / "results.json").write_text(json.dumps(results, indent=2))
                    print(json.dumps({key: measured[key] for key in ("target", "workload", "concurrency", "successful_tps", "p95_ms", "failed_transactions", "process_exit_code")}), flush=True)
                    if measured["process_exit_code"]:
                        raise RuntimeError(f"pgbench failed; inspect {directory}")
        # Leave server tasks time to close prior benchmark connections.
        time.sleep(0.5)
        results["overload"] = overload(cluster)
        (directory / "results.json").write_text(json.dumps(results, indent=2))
        print("OVERLOAD " + json.dumps(results["overload"]), flush=True)
    finally:
        if cluster.log_path.exists():
            shutil.copy(cluster.log_path, directory / "router.log")
        cluster.close()
    import csv
    fields = ["target", "workload", "concurrency", "successful_tps", "p50_ms", "p95_ms", "p99_ms", "successful_transactions", "failed_transactions", "latency_samples", "process_exit_code", "router_cpu_percent_of_one_core", "router_rss_end_mib"]
    with (directory / "results.csv").open("w", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results["measurements"])
    print(f"Results: {directory}", flush=True)


if __name__ == "__main__":
    main()
