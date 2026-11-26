#!/usr/bin/env python3
"""
NUMA Scheduler Benchmark Harness.

Selectable cases (-c / --cases):
  local  : numactl --cpunodebind=N0 --membind=N0 (ideal upper bound)
  remote : numactl --cpunodebind=N1 --membind=N0 (interconnect penalty bound)
  cfs    : numactl --membind=N0 (stock Linux CFS)
  migr   : numactl --membind=N0 + numa_migr -i <ms> (for each interval in -i)
  custom : numactl --membind=N0 + --sched command
  all    : local, remote (if >=2 nodes), cfs, migr (if >=2 nodes & -i set), custom (if --sched set)
"""

import argparse
import csv
import glob
import os
import re
import shlex
import shutil
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


def wrap_numactl(cmd, has_numactl, cpunode=None, memnode=None):
    if not has_numactl:
        return list(cmd)
    prefix = ["numactl"]
    if cpunode is not None:
        prefix.append(f"--cpunodebind={cpunode}")
    if memnode is not None:
        prefix.append(f"--membind={memnode}")
    return prefix + list(cmd)


def parse_number(text):
    return float(text.replace(",", ""))


def parse_membench(stdout):
    m = re.search(
        r"SUMMARY\s+mode=(\S+)\s+samples=\d+\s+mean=[\d.]+\s+median=([\d.]+)",
        stdout,
    )
    if not m:
        return None, None
    mode, median = m.group(1), float(m.group(2))
    unit = "Macc/s" if mode == "chase" else "GB/s"
    return median, unit


def parse_stream(stdout):
    m = re.search(r"Triad:\s+([\d.]+)", stdout)
    if not m:
        return None, None
    return float(m.group(1)) / 1000.0, "GB/s"


def parse_gapbs(stdout):
    m = re.search(r"Average Time:\s+([\d.]+)", stdout)
    if not m:
        return None, None
    return float(m.group(1)), "sec"


def parse_xsbench(stdout):
    m = re.search(r"Lookups/s:\s+([\d,]+(?:\.\d+)?)", stdout)
    if not m:
        return None, None
    return parse_number(m.group(1)) / 1e6, "MLookup/s"


def parse_duckdb(stdout):
    times = re.findall(r"Run Time \(s\):\s+real\s+([\d.]+)", stdout)
    if not times:
        return None, None
    return sum(float(t) for t in times), "sec"


def parse_migr_stats(stdout):
    lines = re.findall(
        r"(?:FINAL\s+)?runs=(\d+)\s+cpu_mig=(\d+)\s+node_mig=(\d+)\s+forced=(\d+)",
        stdout,
    )
    if not lines:
        return {"runs": "-", "cpu_mig": "-", "node_mig": "-", "forced": "-"}
    r, cm, nm, fm = map(int, lines[-1])
    return {"runs": r, "cpu_mig": cm, "node_mig": nm, "forced": fm}


def wait_for_ready(proc, timeout=30.0):
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

    prefix_out = wait_for_ready(proc) if wait_ready else ""

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
        choices=[
            "chase", "stream", "stream_omp", "stream_st",
            "gapbs_pr", "gapbs_bfs", "gapbs_cc", "gapbs_sssp",
            "xsbench", "duckdb_tpch",
        ],
        default="gapbs_pr",
        help="Built-in workload (ignored if --cmd is set; default: gapbs_pr)",
    )
    ap.add_argument(
        "--cmd",
        default=None,
        help="External benchmark command, e.g. './benchmark/ext/gapbs/cc -g 23 -n 10'",
    )
    ap.add_argument(
        "--regex",
        default=r"SUMMARY\s+mode=\S+\s+samples=\d+\s+mean=[\d.]+\s+median=([\d.]+)",
        help="Regex with 1 numeric capture group to parse --cmd output",
    )
    ap.add_argument("--unit", default="score", help="Unit label for --cmd output")
    ap.add_argument(
        "--lower-is-better",
        action="store_true",
        help="Compute 'vs Local' as local/score instead of score/local (for time/latency)",
    )
    ap.add_argument(
        "--wait-ready",
        action="store_true",
        help="Wait for stdout line containing 'ready' before attaching scheduler",
    )
    ap.add_argument(
        "-c", "--cases",
        default="all",
        help="Comma-separated cases: local,remote,cfs,migr,custom, or all (default: all)",
    )
    ap.add_argument("-s", "--size", type=int, default=1024, help="membench size MB")
    ap.add_argument("-t", "--duration", type=int, default=15, help="membench duration s")
    ap.add_argument("-w", "--warmup", type=int, default=2, help="membench warmup s")
    ap.add_argument(
        "-i", "--intervals",
        default="0,100,20,5,1",
        help="Comma-separated numa_migr intervals in ms (default: 0,100,20,5,1)",
    )
    ap.add_argument(
        "--sched",
        default=None,
        help="Custom scheduler command template using {PID}, e.g. './my_sched -p {PID}'",
    )
    ap.add_argument("--csv", default=None, help="Optional output CSV path")
    args = ap.parse_args()

    nodes = discover_nodes()
    if not nodes:
        sys.exit("No NUMA nodes found in /sys/devices/system/node")

    valid_cases = {"local", "remote", "cfs", "migr", "custom"}
    raw_cases = [c.strip() for c in args.cases.split(",") if c.strip()]
    if not raw_cases:
        sys.exit("No cases selected")

    if "all" in raw_cases:
        selected = ["local"]
        if len(nodes) >= 2:
            selected.append("remote")
        selected.append("cfs")
        if len(nodes) >= 2 and args.intervals.strip():
            selected.append("migr")
        if args.sched:
            selected.append("custom")
        if len(nodes) < 2:
            print("[info] 1 NUMA node detected; skipping 'remote' and 'migr' cases.")
    else:
        for c in raw_cases:
            if c not in valid_cases:
                sys.exit(f"Unknown case '{c}'. Valid: local, remote, cfs, migr, custom, all")
        selected = raw_cases

    if "custom" in selected and not args.sched:
        sys.exit("Case 'custom' requires --sched \"...\"")
    if "migr" in selected and not args.intervals.strip():
        sys.exit("Case 'migr' requires non-empty -i/--intervals")
    if ("remote" in selected or "migr" in selected) and len(nodes) < 2:
        sys.exit(f"Cases 'remote'/'migr' require >= 2 NUMA nodes, found: {nodes}")

    n0 = nodes[0]
    n1 = nodes[1] if len(nodes) > 1 else nodes[0]

    has_numactl = shutil.which("numactl") is not None
    if not has_numactl:
        if len(nodes) >= 2:
            sys.exit("numactl not found (install numactl for multi-node pinning)")
        print("[warn] numactl not found; running without explicit NUMA binding on 1-node host.")

    needs_root = ("migr" in selected) or ("custom" in selected)
    if needs_root and os.geteuid() != 0:
        sys.exit("Run with sudo when running 'migr' or 'custom' cases")

    lower_is_better = args.lower_is_better
    if args.cmd:
        base_cmd = shlex.split(args.cmd)
        rx = re.compile(args.regex)

        def parser(stdout):
            m = rx.search(stdout)
            return (parse_number(m.group(1)), args.unit) if m else (None, None)

        wait_ready = args.wait_ready or ("membench" in os.path.basename(base_cmd[0]))
        bench_label = base_cmd[0]
    elif args.bench in ("chase", "stream"):
        base_cmd = [
            "./benchmark/membench",
            "-m", args.bench,
            "-s", str(args.size),
            "-t", str(args.duration),
            "-w", str(args.warmup),
        ]
        parser = parse_membench
        wait_ready = True
        bench_label = args.bench
    elif args.bench == "stream_omp":
        base_cmd = ["./benchmark/stream_omp"]
        parser = parse_stream
        wait_ready = False
        bench_label = "stream_omp"
    elif args.bench == "stream_st":
        base_cmd = ["./benchmark/stream"]
        parser = parse_stream
        wait_ready = False
        bench_label = "stream_st"
    elif args.bench == "gapbs_pr":
        base_cmd = ["./benchmark/ext/gapbs/pr", "-g", "22", "-n", "15"]
        parser = parse_gapbs
        wait_ready = False
        lower_is_better = True
        bench_label = "gapbs_pr"
    elif args.bench == "gapbs_bfs":
        base_cmd = ["./benchmark/ext/gapbs/bfs", "-g", "22", "-n", "30"]
        parser = parse_gapbs
        wait_ready = False
        lower_is_better = True
        bench_label = "gapbs_bfs"
    elif args.bench == "gapbs_cc":
        base_cmd = ["./benchmark/ext/gapbs/cc", "-g", "22", "-n", "15"]
        parser = parse_gapbs
        wait_ready = False
        lower_is_better = True
        bench_label = "gapbs_cc"
    elif args.bench == "gapbs_sssp":
        base_cmd = ["./benchmark/ext/gapbs/sssp", "-g", "22", "-n", "15"]
        parser = parse_gapbs
        wait_ready = False
        lower_is_better = True
        bench_label = "gapbs_sssp"
    elif args.bench == "duckdb_tpch":
        base_cmd = ["./benchmark/ext/duckdb/duckdb", "-f", "./benchmark/tpch.sql"]
        parser = parse_duckdb
        wait_ready = False
        lower_is_better = True
        bench_label = "duckdb_tpch"
    else:  # xsbench
        base_cmd = ["./benchmark/ext/XSBench/openmp-threading/XSBench", "-s", "large", "-p", "2000000"]
        parser = parse_xsbench
        wait_ready = False
        bench_label = "xsbench"

    cases = []
    for c in selected:
        if c == "local":
            cases.append(("local", wrap_numactl(base_cmd, has_numactl, cpunode=n0, memnode=n0), None))
        elif c == "remote":
            cases.append(("remote", wrap_numactl(base_cmd, has_numactl, cpunode=n1, memnode=n0), None))
        elif c == "cfs":
            cases.append(("cfs", wrap_numactl(base_cmd, has_numactl, memnode=n0), None))
        elif c == "migr":
            for ms in args.intervals.split(","):
                ms = ms.strip()
                if ms:
                    cases.append((
                        f"migr_{ms}ms",
                        wrap_numactl(base_cmd, has_numactl, memnode=n0),
                        ["./migr/numa_migr", "-p", "{PID}", "-i", ms],
                    ))
        elif c == "custom":
            cases.append((
                "custom",
                wrap_numactl(base_cmd, has_numactl, memnode=n0),
                shlex.split(args.sched),
            ))

    results = []
    local_val = None

    for name, bcmd, scmd in cases:
        out, stats = run_case(name, bcmd, scmd, wait_ready=wait_ready)
        val, unit = parser(out)
        if name == "local":
            local_val = val

        if val is not None and local_val:
            if lower_is_better:
                pct_num = (local_val / val * 100.0) if val > 0 else None
            else:
                pct_num = val / local_val * 100.0
        else:
            pct_num = None

        results.append({
            "case": name,
            "value": val,
            "unit": unit or args.unit,
            "pct_local": f"{pct_num:.1f}%" if pct_num is not None else "-",
            "runs": stats["runs"],
            "cpu_mig": stats["cpu_mig"],
            "node_mig": stats["node_mig"],
            "forced": stats["forced"],
        })

    print("\n" + "=" * 80)
    print(f"Benchmark: {bench_label} (mem_node={n0}, remote_node={n1})")
    print(f"{'Case':<14} {'Score':>10} {'Unit':<9} {'vs Local':>9} {'Runs':>8} {'CPU_Mig':>8} {'Node_Mig':>9} {'Forced':>7}")
    print("-" * 80)
    for r in results:
        score_str = f"{r['value']:.3f}" if r["value"] is not None else "N/A"
        print(
            f"{r['case']:<14} {score_str:>10} {r['unit']:<9} {r['pct_local']:>9} "
            f"{str(r['runs']):>8} {str(r['cpu_mig']):>8} {str(r['node_mig']):>9} {str(r['forced']):>7}"
        )
    print("=" * 80)

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=results[0].keys())
            w.writeheader()
            for r in results:
                row = dict(r)
                if row["value"] is None:
                    row["value"] = ""
                w.writerow(row)
        print(f"Saved results to {args.csv}")


if __name__ == "__main__":
    main()