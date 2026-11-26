#!/usr/bin/env python3
"""
NUMA Scheduler Benchmark Harness.

Runs a chosen benchmark across:
  1. local      : numactl --cpunodebind=N0 --membind=N0 (ideal upper bound)
  2. remote     : numactl --cpunodebind=N1 --membind=N0 (interconnect penalty bound)
  3. cfs        : numactl --membind=N0 (stock Linux CFS)
  4. migr_<ms>  : numactl --membind=N0 + numa_migr -i <ms> (NUMA-oblivious stress)
  5. custom     : (optional) --sched /path/to/your_sched -p <pid>
"""

import argparse
import csv
import glob
import os
import re
import signal
import subprocess
import sys
import time


def discover_nodes():
    nodes = []
    for path in sorted(glob.glob("/sys/devices/system/node/node[0-9]*/cpulist")):
        nid = int(re.search(r"node(\d+)", path).group(1))
        with open(path) as f:
            cpulist = f.read().strip()
        if cpulist:
            nodes.append(nid)
    return sorted(nodes)


def parse_membench(stdout):
    # SUMMARY mode=chase samples=13 mean=12.3456 median=12.3000 min=... max=...
    m = re.search(
        r"SUMMARY\s+mode=(\S+)\s+samples=\d+\s+mean=([\d.]+)\s+median=([\d.]+)",
        stdout,
    )
    if not m:
        return None, None
    mode, mean, median = m.group(1), float(m.group(2)), float(m.group(3))
    unit = "Macc/s" if mode == "chase" else "GB/s"
    return median, unit


def parse_stream(stdout):
    # Triad:       12345.6     0.077...
    m = re.search(r"Triad:\s+([\d.]+)", stdout)
    if not m:
        return None, None
    gb_s = float(m.group(1)) / 1000.0
    return gb_s, "GB/s"


def parse_migr_stats(stdout):
    # Check FINAL line first, else last periodic line
    lines = re.findall(
        r"(?:FINAL\s+)?runs=(\d+)\s+cpu_mig=(\d+)\s+node_mig=(\d+)\s+forced=(\d+)",
        stdout,
    )
    if not lines:
        return {"runs": 0, "cpu_mig": 0, "node_mig": 0, "forced": 0}
    r, cm, nm, fm = map(int, lines[-1])
    return {"runs": r, "cpu_mig": cm, "node_mig": nm, "forced": fm}


def wait_for_ready(proc, timeout=30.0):
    """Consume lines until membench prints 'ready' (init/first-touch complete)."""
    buf = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if not line:
            break
        buf.append(line)
        if "ready" in line:
            return "".join(buf)
    return "".join(buf)


def run_case(name, bench_cmd, sched_cmd=None, wait_ready=False):
    print(f"==> Running [{name}]: {' '.join(bench_cmd)}", flush=True)
    proc = subprocess.Popen(
        bench_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    prefix_out = ""
    if wait_ready:
        prefix_out = wait_for_ready(proc)

    sched_proc = None
    if sched_cmd:
        cmd = [arg.replace("{PID}", str(proc.pid)) for arg in sched_cmd]
        print(f"    attaching scheduler: {' '.join(cmd)}", flush=True)
        sched_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        time.sleep(0.2)
        if sched_proc.poll() is not None:
            out, _ = sched_proc.communicate()
            proc.terminate()
            raise RuntimeError(f"Scheduler exited early:\n{out}")

    rest_out, _ = proc.communicate()
    bench_out = prefix_out + rest_out

    migr_stats = {"runs": "-", "cpu_mig": "-", "node_mig": "-", "forced": "-"}
    if sched_proc:
        if sched_proc.poll() is None:
            sched_proc.send_signal(signal.SIGINT)
        try:
            sched_out, _ = sched_proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            sched_proc.kill()
            sched_out, _ = sched_proc.communicate()
        if "runs=" in sched_out:
            migr_stats = parse_migr_stats(sched_out)

    if proc.returncode != 0:
        print(f"WARNING: benchmark exited with {proc.returncode}:\n{bench_out}")

    return bench_out, migr_stats


def main():
    ap = argparse.ArgumentParser(description="NUMA scheduler benchmark suite")
    ap.add_argument(
        "--bench",
        choices=["chase", "stream", "stream_omp", "stream_st"],
        default="chase",
        help="Workload to run (default: chase)",
    )
    ap.add_argument("-s", "--size", type=int, default=1024, help="membench size MB")
    ap.add_argument("-t", "--duration", type=int, default=15, help="membench duration s")
    ap.add_argument("-w", "--warmup", type=int, default=2, help="membench warmup s")
    ap.add_argument(
        "-i",
        "--intervals",
        default="0,100,20,5,1",
        help="Comma-separated numa_migr intervals in ms (empty to skip)",
    )
    ap.add_argument(
        "--sched",
        default=None,
        help="Custom scheduler command template using {PID}, e.g. './my_sched -p {PID}'",
    )
    ap.add_argument("--csv", default=None, help="Optional output CSV path")
    args = ap.parse_args()

    nodes = discover_nodes()
    if len(nodes) < 2:
        sys.exit(f"Need at least 2 NUMA nodes with CPUs, found: {nodes}")
    n0, n1 = nodes[0], nodes[1]

    needs_root = bool(args.intervals.strip() or args.sched)
    if needs_root and os.geteuid() != 0:
        sys.exit("Run with sudo when testing sched_ext schedulers (e.g. sudo ./bench.py ...)")

    if args.bench in ("chase", "stream"):
        base_cmd = [
            "./benchmark/membench",
            "-m", args.bench,
            "-s", str(args.size),
            "-t", str(args.duration),
            "-w", str(args.warmup),
        ]
        parser = parse_membench
        wait_ready = True
    elif args.bench == "stream_omp":
        base_cmd = ["./benchmark/stream_omp"]
        parser = parse_stream
        wait_ready = False
    else:
        base_cmd = ["./benchmark/stream"]
        parser = parse_stream
        wait_ready = False

    cases = [
        ("local", ["numactl", f"--cpunodebind={n0}", f"--membind={n0}"] + base_cmd, None),
        ("remote", ["numactl", f"--cpunodebind={n1}", f"--membind={n0}"] + base_cmd, None),
        ("cfs", ["numactl", f"--membind={n0}"] + base_cmd, None),
    ]

    if args.intervals.strip():
        for ms in args.intervals.split(","):
            ms = ms.strip()
            cases.append((
                f"migr_{ms}ms",
                ["numactl", f"--membind={n0}"] + base_cmd,
                ["./migr/numa_migr", "-p", "{PID}", "-i", ms],
            ))

    if args.sched:
        cases.append((
            "custom_sched",
            ["numactl", f"--membind={n0}"] + base_cmd,
            args.sched.split(),
        ))

    results = []
    local_val = None

    for name, bcmd, scmd in cases:
        out, stats = run_case(name, bcmd, scmd, wait_ready=wait_ready)
        val, unit = parser(out)
        if name == "local":
            local_val = val
        pct = (val / local_val * 100.0) if (val is not None and local_val) else 0.0
        results.append({
            "case": name,
            "value": val if val is not None else 0.0,
            "unit": unit or "?",
            "pct_local": pct,
            "runs": stats["runs"],
            "cpu_mig": stats["cpu_mig"],
            "node_mig": stats["node_mig"],
            "forced": stats["forced"],
        })

    print("\n" + "=" * 78)
    print(f"Benchmark: {args.bench} (mem_node={n0}, remote_node={n1})")
    print(f"{'Case':<14} {'Score':>10} {'Unit':<7} {'vs Local':>9} {'Runs':>8} {'CPU_Mig':>8} {'Node_Mig':>9} {'Forced':>7}")
    print("-" * 78)
    for r in results:
        print(
            f"{r['case']:<14} {r['value']:>10.3f} {r['unit']:<7} {r['pct_local']:>8.1f}% "
            f"{str(r['runs']):>8} {str(r['cpu_mig']):>8} {str(r['node_mig']):>9} {str(r['forced']):>7}"
        )
    print("=" * 78)

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=results[0].keys())
            w.writeheader()
            w.writerows(results)
        print(f"Saved results to {args.csv}")


if __name__ == "__main__":
    main()