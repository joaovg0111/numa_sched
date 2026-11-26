#!/usr/bin/env python3
"""
NUMA Scheduler Benchmark Harness.

One invocation = one run. Everything lands in a single output folder
(--outdir, default results_YYYYmmdd_HHMMSS):
  results.csv   all benchmarks / cases (one row per case, with metadata + time_s)
  full_run.log  everything printed to the console + raw output of every run
  summary.txt   run info, time taken per benchmark, and the result tables
--bench accepts one name, a comma-separated list, or "all".

Selectable cases (-c / --cases):
  local  : numactl --cpunodebind=MEM --membind=MEM (ideal upper bound)
  remote : numactl --cpunodebind=REM --membind=MEM (interconnect penalty bound)
  cfs    : numactl --membind=MEM (stock Linux CFS)
  migr   : numactl --membind=MEM + numa_migr -i <ms> (for each interval in -i)
  custom : numactl --membind=MEM + --sched command
  all    : local, remote (if >=2 nodes), cfs, migr (if >=2 nodes & -i set), custom (if --sched set)
"""

import argparse
import csv
import socket
import platform
import datetime
import glob
import os
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import traceback


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
        return None
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


LOG_FH = None


def log_raw(title, text):
    """Write text to the log file only (not the console)."""
    if LOG_FH and text:
        LOG_FH.write(f"\n----- {title} -----\n{text.rstrip()}\n----- end -----\n")
        LOG_FH.flush()


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, s):
        for st in self.streams:
            st.write(s)

    def flush(self):
        for st in self.streams:
            st.flush()


def fmt_dur(sec):
    if sec < 60:
        return f"{sec:.1f}s"
    m, s = divmod(int(round(sec)), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def run_case(name, bench_cmd, sched_cmd=None, wait_ready=False, env=None):
    print(f"==> Running [{name}]: {' '.join(bench_cmd)}", flush=True)
    proc = subprocess.Popen(
        bench_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
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
    log_raw(f"[{name}] benchmark output", bench_out)

    migr_stats = None
    if sched_proc:
        if sched_proc.poll() is None:
            sched_proc.send_signal(signal.SIGINT)
        try:
            sched_out, _ = sched_proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            sched_proc.kill()
            sched_out, _ = sched_proc.communicate()
        log_raw(f"[{name}] scheduler output", sched_out)
        if "runs=" in sched_out:
            migr_stats = parse_migr_stats(sched_out)

    if proc.returncode != 0:
        print(f"WARNING: benchmark exited with {proc.returncode}:\n{bench_out}")

    return bench_out, migr_stats


BENCHES = [
    "chase", "stream", "stream_st", "stream_omp",
    "gapbs_pr", "gapbs_bfs", "gapbs_cc", "gapbs_sssp",
    "xsbench", "duckdb_tpch",
]
GAPBS_N = {"gapbs_pr": ("pr", "15"), "gapbs_bfs": ("bfs", "30"),
           "gapbs_cc": ("cc", "15"), "gapbs_sssp": ("sssp", "15")}


def build_bench(args, bench, tmp_files):
    """Return dict(label, cmd, parser, wait_ready, lower)."""
    if bench == "__cmd__":
        base_cmd = shlex.split(args.cmd)
        rx = re.compile(args.regex)

        def parser(stdout):
            m = rx.search(stdout)
            return (parse_number(m.group(1)), args.unit) if m else (None, None)

        return dict(label=base_cmd[0], cmd=base_cmd, parser=parser,
                    wait_ready=args.wait_ready or ("membench" in os.path.basename(base_cmd[0])),
                    lower=args.lower_is_better)
    if bench in ("chase", "stream"):
        cmd = ["./benchmark/membench", "-m", bench, "-s", str(args.size),
               "-t", str(args.duration), "-w", str(args.warmup)]
        return dict(label=bench, cmd=cmd, parser=parse_membench, wait_ready=True, lower=False)
    if bench == "stream_omp":
        return dict(label=bench, cmd=["./benchmark/stream_omp"], parser=parse_stream,
                    wait_ready=False, lower=False)
    if bench == "stream_st":
        return dict(label=bench, cmd=["./benchmark/stream"], parser=parse_stream,
                    wait_ready=False, lower=False)
    if bench in GAPBS_N:
        exe, n = GAPBS_N[bench]
        return dict(label=bench, cmd=[f"./benchmark/ext/gapbs/{exe}", "-g", "22", "-n", n],
                    parser=parse_gapbs, wait_ready=False, lower=True)
    if bench == "duckdb_tpch":
        sql_file = "./benchmark/tpch.sql"
        if args.threads is not None:
            with open(sql_file) as f:
                body = f.read()
            tf = tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False)
            tf.write(f"SET threads = {args.threads};\n" + body)
            tf.close()
            tmp_files.append(tf.name)
            sql_file = tf.name
        return dict(label=bench, cmd=["./benchmark/ext/duckdb/duckdb", "-f", sql_file],
                    parser=parse_duckdb, wait_ready=False, lower=True)
    if bench == "xsbench":
        return dict(label=bench,
                    cmd=["./benchmark/ext/XSBench/openmp-threading/XSBench", "-s", "large"],
                    parser=parse_xsbench, wait_ready=False, lower=False)
    raise ValueError(f"unknown bench {bench}")


def build_cases(args, base_cmd, selected, has_numactl, n0, n1):
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
                    cases.append((f"migr_{ms}ms", wrap_numactl(base_cmd, has_numactl, memnode=n0),
                                  ["./migr/numa_migr", "-p", "{PID}", "-i", ms]))
        elif c == "custom":
            cases.append(("custom", wrap_numactl(base_cmd, has_numactl, memnode=n0),
                          shlex.split(args.sched)))
    return cases


def run_bench(args, spec, selected, has_numactl, n0, n1, env):
    results = []
    local_val = None
    for name, bcmd, scmd in build_cases(args, spec["cmd"], selected, has_numactl, n0, n1):
        vals, units, stats_list = [], [], []
        t0 = time.monotonic()
        for rep in range(args.repeat):
            tag = f"{name} ({rep + 1}/{args.repeat})" if args.repeat > 1 else name
            out, stats = run_case(tag, bcmd, scmd, wait_ready=spec["wait_ready"], env=env)
            v, u = spec["parser"](out)
            if v is not None:
                vals.append(v)
                units.append(u)
            if stats is not None:
                stats_list.append(stats)
        elapsed = time.monotonic() - t0

        med = statistics.median(vals) if vals else None
        unit = units[0] if units else args.unit
        if name == "local":
            local_val = med
        pct = None
        if med is not None and local_val:
            pct = (local_val / med * 100.0) if spec["lower"] and med > 0 else (
                None if spec["lower"] else med / local_val * 100.0)

        agg = {k: (int(statistics.median(s[k] for s in stats_list)) if stats_list else "-")
               for k in ("runs", "cpu_mig", "node_mig", "forced")}
        results.append({
            "case": name, "value": med,
            "min": min(vals) if vals else None, "max": max(vals) if vals else None,
            "unit": unit, "raw": ";".join(f"{v:.6g}" for v in vals),
            "pct_local": f"{pct:.1f}%" if pct is not None else "-",
            **agg, "time_s": round(elapsed, 1),
        })
    return results


def format_table(label, results, args, n0, n1):
    threads_info = f"threads={args.threads}" if args.threads else "threads=auto"
    lines = ["=" * 102,
             f"Benchmark: {label} ({threads_info}, repeats={args.repeat}, mem_node={n0}, remote_node={n1})"]
    multi = args.repeat > 1
    if multi:
        lines.append(f"{'Case':<14} {'Median':>10} {'Min..Max':>17} {'Unit':<9} {'vs Local':>9} "
                     f"{'Runs':>8} {'CPU_Mig':>8} {'Node_Mig':>9} {'Forced':>7} {'Time':>9}")
    else:
        lines.append(f"{'Case':<14} {'Score':>10} {'Unit':<9} {'vs Local':>9} "
                     f"{'Runs':>8} {'CPU_Mig':>8} {'Node_Mig':>9} {'Forced':>7} {'Time':>9}")
    lines.append("-" * 102)
    for r in results:
        score = f"{r['value']:.3f}" if r["value"] is not None else "N/A"
        tail = (f"{str(r['runs']):>8} {str(r['cpu_mig']):>8} {str(r['node_mig']):>9} "
                f"{str(r['forced']):>7} {fmt_dur(r['time_s']):>9}")
        if multi:
            rng = f"{r['min']:.2f}..{r['max']:.2f}" if r["min"] is not None else "-"
            lines.append(f"{r['case']:<14} {score:>10} {rng:>17} {r['unit']:<9} {r['pct_local']:>9} {tail}")
        else:
            lines.append(f"{r['case']:<14} {score:>10} {r['unit']:<9} {r['pct_local']:>9} {tail}")
    lines.append("=" * 102)
    return "\n".join(lines)


def write_summary(path, info, records, args, n0, n1, finished):
    total = time.monotonic() - info["t0"]
    out = []
    out.append("NUMA benchmark summary")
    out.append("=" * 102)
    out.append(f"run_id     : {info['run_id']}")
    out.append(f"started    : {info['started']}")
    out.append(f"status     : {'complete' if finished else 'INCOMPLETE (still running or aborted)'}")
    out.append(f"total time : {fmt_dur(total)}")
    out.append(f"host/kernel: {info['host']} / {info['kernel']}")
    out.append(f"nodes      : {info['nodes']}  (mem_node={n0}, remote_node={n1})")
    out.append(f"settings   : threads={args.threads or 'auto'} repeats={args.repeat} cases={info['cases']}")
    out.append(f"command    : {info['cmdline']}")
    out.append("")
    out.append("Time per benchmark")
    out.append("-" * 60)
    out.append(f"{'Benchmark':<16} {'Status':<22} {'Time':>10}")
    for r in records:
        out.append(f"{r['label']:<16} {r['status']:<22} {fmt_dur(r['elapsed']):>10}")
    out.append(f"{'TOTAL':<16} {'':<22} {fmt_dur(total):>10}")
    out.append("")
    for r in records:
        if r["results"]:
            out.append(format_table(r["label"], r["results"], args, n0, n1))
            out.append("")
    with open(path, "w") as f:
        f.write("\n".join(out) + "\n")


def append_csv(path, meta, results):
    fieldnames = list(meta) + list(results[0].keys())
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if new:
            w.writeheader()
        for r in results:
            row = {**meta, **r}
            for col in ("value", "min", "max"):
                if row[col] is None:
                    row[col] = ""
            w.writerow(row)


def main():
    global LOG_FH
    ap = argparse.ArgumentParser(description="NUMA scheduler benchmark suite")
    ap.add_argument("--bench", default="gapbs_pr",
                    help="Workload name, comma-separated list, or 'all'. Valid: "
                         + ", ".join(BENCHES) + " (ignored if --cmd is set; default: gapbs_pr)")
    ap.add_argument("--cmd", default=None,
                    help="External benchmark command, e.g. './benchmark/ext/gapbs/cc -g 23 -n 10'")
    ap.add_argument("--regex",
                    default=r"SUMMARY\s+mode=\S+\s+samples=\d+\s+mean=[\d.]+\s+median=([\d.]+)",
                    help="Regex with 1 numeric capture group to parse --cmd output")
    ap.add_argument("--unit", default="score", help="Unit label for --cmd output")
    ap.add_argument("--lower-is-better", action="store_true",
                    help="For --cmd: compute 'vs Local' as local/score instead of score/local")
    ap.add_argument("--wait-ready", action="store_true",
                    help="Wait for stdout line containing 'ready' before attaching scheduler")
    ap.add_argument("-c", "--cases", default="all",
                    help="Comma-separated cases: local,remote,cfs,migr,custom, or all (default: all)")
    ap.add_argument("--threads", type=int, default=None,
                    help="Worker threads (sets OMP_NUM_THREADS and DuckDB threads; default: all CPUs)")
    ap.add_argument("--repeat", type=int, default=1,
                    help="Repetitions per case; reports median (default: 1)")
    ap.add_argument("--mem-node", type=int, default=None,
                    help="NUMA node to bind memory & local CPU to (default: first online node)")
    ap.add_argument("--remote-node", type=int, default=None,
                    help="NUMA node for the 'remote' baseline CPU binding (default: next online node)")
    ap.add_argument("-s", "--size", type=int, default=1024, help="membench size MB")
    ap.add_argument("-t", "--duration", type=int, default=15, help="membench duration s")
    ap.add_argument("-w", "--warmup", type=int, default=2, help="membench warmup s")
    ap.add_argument("-i", "--intervals", default="0,100,20,5,1",
                    help="Comma-separated numa_migr intervals in ms (default: 0,100,20,5,1)")
    ap.add_argument("--sched", default=None,
                    help="Custom scheduler command template using {PID}, e.g. './my_sched -p {PID}'")
    ap.add_argument("--outdir", default=None,
                    help="Output folder for results.csv, full_run.log, summary.txt "
                         "(default: results_YYYYmmdd_HHMMSS). Must not already contain files.")
    args = ap.parse_args()

    if args.threads is not None and args.threads < 1:
        sys.exit("--threads must be >= 1")
    if args.repeat < 1:
        sys.exit("--repeat must be >= 1")

    if args.cmd:
        bench_list = ["__cmd__"]
    elif args.bench.strip() == "all":
        bench_list = list(BENCHES)
    else:
        bench_list = [b.strip() for b in args.bench.split(",") if b.strip()]
        for b in bench_list:
            if b not in BENCHES:
                sys.exit(f"Unknown bench '{b}'. Valid: {', '.join(BENCHES)}, all")
        if not bench_list:
            sys.exit("No benchmark selected")

    nodes = discover_nodes()
    if not nodes:
        sys.exit("No NUMA nodes found in /sys/devices/system/node")

    if args.mem_node is not None:
        if args.mem_node not in nodes:
            sys.exit(f"--mem-node {args.mem_node} not in online nodes: {nodes}")
        n0 = args.mem_node
    else:
        n0 = nodes[0]
    if args.remote_node is not None:
        if args.remote_node not in nodes:
            sys.exit(f"--remote-node {args.remote_node} not in online nodes: {nodes}")
        n1 = args.remote_node
    else:
        others = [n for n in nodes if n != n0]
        n1 = others[0] if others else n0

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

    has_numactl = shutil.which("numactl") is not None
    if not has_numactl:
        if len(nodes) >= 2:
            sys.exit("numactl not found (install numactl for multi-node pinning)")
        print("[warn] numactl not found; running without explicit NUMA binding on 1-node host.")

    if (("migr" in selected) or ("custom" in selected)) and os.geteuid() != 0:
        sys.exit("Run with sudo when running 'migr' or 'custom' cases")

    # ---- output folder, log, summary ----
    now = datetime.datetime.now()
    run_id = now.strftime("%Y%m%d_%H%M%S")
    outdir = args.outdir or f"results_{run_id}"
    if os.path.isdir(outdir) and os.listdir(outdir):
        sys.exit(f"--outdir '{outdir}' already exists and is not empty; refusing to overwrite")
    os.makedirs(outdir, exist_ok=True)
    csv_path = os.path.join(outdir, "results.csv")
    summary_path = os.path.join(outdir, "summary.txt")
    LOG_FH = open(os.path.join(outdir, "full_run.log"), "w")
    real_stdout = sys.stdout
    sys.stdout = Tee(real_stdout, LOG_FH)

    info = {
        "run_id": run_id, "started": now.isoformat(timespec="seconds"),
        "host": socket.gethostname(), "kernel": platform.release(),
        "nodes": nodes, "cases": ",".join(selected),
        "cmdline": " ".join(shlex.quote(a) for a in sys.argv), "t0": time.monotonic(),
    }
    print(f"Output folder: {outdir}", flush=True)

    bench_env = os.environ.copy()
    bench_env["OMP_PROC_BIND"] = "false"
    if args.threads is not None:
        bench_env["OMP_NUM_THREADS"] = str(args.threads)

    records, tmp_files, finished = [], [], False
    try:
        for bench in bench_list:
            rec = {"label": "cmd" if bench == "__cmd__" else bench,
                   "status": "ok", "results": [], "elapsed": 0.0}
            t0 = time.monotonic()
            records.append(rec)
            print(f"\n##### {rec['label']} #####", flush=True)
            try:
                spec = build_bench(args, bench, tmp_files)
                rec["label"] = spec["label"]
                rec["results"] = run_bench(args, spec, selected, has_numactl, n0, n1, bench_env)
                if all(r["value"] is None for r in rec["results"]):
                    rec["status"] = "no data (parse failed)"
                print("\n" + format_table(rec["label"], rec["results"], args, n0, n1), flush=True)
                meta = {
                    "run_id": run_id, "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                    "bench": rec["label"], "threads": args.threads or "auto", "repeats": args.repeat,
                    "mem_node": n0, "remote_node": n1, "host": info["host"], "kernel": info["kernel"],
                }
                append_csv(csv_path, meta, rec["results"])
            except KeyboardInterrupt:
                rec["status"] = "interrupted"
                rec["elapsed"] = time.monotonic() - t0
                raise
            except Exception:
                rec["status"] = "FAILED"
                print(f"ERROR in {rec['label']}:\n{traceback.format_exc()}", flush=True)
            rec["elapsed"] = time.monotonic() - t0
            print(f"[{rec['label']}] took {fmt_dur(rec['elapsed'])} ({rec['status']})", flush=True)
            write_summary(summary_path, info, records, args, n0, n1, finished=False)
        finished = True
    except KeyboardInterrupt:
        print("\nInterrupted.", flush=True)
    finally:
        for p in tmp_files:
            if os.path.exists(p):
                os.unlink(p)
        write_summary(summary_path, info, records, args, n0, n1, finished=finished)
        total = time.monotonic() - info["t0"]
        print(f"\nDone in {fmt_dur(total)}. Files in {outdir}/: results.csv, full_run.log, summary.txt",
              flush=True)
        sys.stdout = real_stdout
        LOG_FH.close()
        LOG_FH = None
        # when run via sudo, hand the folder back to the invoking user
        if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
            uid, gid = int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", os.environ["SUDO_UID"]))
            for root, dirs, files in os.walk(outdir):
                for n in dirs + files:
                    os.chown(os.path.join(root, n), uid, gid)
            os.chown(outdir, uid, gid)


if __name__ == "__main__":
    main()