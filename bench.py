#!/usr/bin/env python3
"""
NUMA Scheduler Benchmark Harness.

One invocation = one run. Everything lands in a single output folder
(--outdir, default results/run_YYYYmmdd_HHMMSS):
  results.csv   one row per case (median/min/max + metadata + time_s)
  runs.csv      one row per repetition (raw values; use this for mean/min/max/std)
  full_run.log  everything printed to the console + raw output of every run
  summary.txt   run info, time taken per benchmark, and the result tables
--bench accepts one name, a comma-separated list, or "all".

Selectable cases (-c / --cases):
  local  : numactl --cpunodebind=MEM --membind=MEM (ideal upper bound)
  remote : numactl --cpunodebind=REM --membind=MEM (interconnect penalty bound)
  cfs    : numactl --membind=MEM (stock Linux CFS)
  migr   : numactl --membind=MEM + numa_migr -i <ms> (for each interval in -i)
  custom : numactl --membind=MEM + each --sched command (repeatable). A command without
           {PID} is treated as a system-wide scheduler (e.g. scx_simple, scx_rusty): it is
           started first and the benchmark begins once sched_ext reports 'enabled'.
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
import threading
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



def read_first(path, default="n/a"):
    try:
        with open(path) as f:
            return f.read().strip() or default
    except OSError:
        return default


def parse_cpulist(txt):
    cpus = []
    for part in txt.strip().split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-")
            cpus.extend(range(int(a), int(b) + 1))
        else:
            cpus.append(int(part))
    return cpus


def cpu_to_node_map():
    m = {}
    for n in discover_nodes():
        for c in parse_cpulist(read_first(f"/sys/devices/system/node/node{n}/cpulist", "")):
            m[c] = n
    return m


def collect_env():
    """Machine/kernel settings that change benchmark results; stored with every run."""
    env = {}
    env["kernel"] = platform.release()
    model = "n/a"
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    env["cpu_model"] = model
    env["online_cpus"] = os.cpu_count()
    sib = read_first("/sys/devices/system/cpu/cpu0/topology/thread_siblings_list", "")
    env["threads_per_core"] = len(parse_cpulist(sib)) if sib else "n/a"
    env["smt_active"] = read_first("/sys/devices/system/cpu/smt/active")
    env["numa_balancing"] = read_first("/proc/sys/kernel/numa_balancing")
    thp = read_first("/sys/kernel/mm/transparent_hugepage/enabled")
    m = re.search(r"\[(\w+)\]", thp)
    env["thp"] = m.group(1) if m else thp
    env["cpu_governor"] = read_first("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    env["sched_ext_state"] = read_first("/sys/kernel/sched_ext/state", "unsupported")
    for n in discover_nodes():
        cpus = parse_cpulist(read_first(f"/sys/devices/system/node/node{n}/cpulist", ""))
        mem = "n/a"
        m = re.search(r"MemTotal:\s+(\d+) kB", read_first(f"/sys/devices/system/node/node{n}/meminfo", ""))
        if m:
            mem = f"{int(m.group(1)) / 1048576:.1f} GiB"
        dist = read_first(f"/sys/devices/system/node/node{n}/distance")
        env[f"node{n}"] = f"{len(cpus)} CPUs, {mem}, distance to nodes: {dist}"
    return env


VMSTAT_KEYS = {
    "hint_faults": "numa_hint_faults",
    "pages_migrated": "numa_pages_migrated",
    "task_migrated": "numa_task_migrated",
}


def read_vmstat():
    out = {}
    try:
        with open("/proc/vmstat") as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k in VMSTAT_KEYS.values():
                    out[k] = int(v)
    except OSError:
        pass
    return out


class PlacementSampler(threading.Thread):
    """Samples which NUMA node the benchmark's runnable threads are on.

    Every `interval` seconds it reads /proc/<pid>/task/*/stat and, for threads in
    state R (running or runnable), looks up the CPU they last ran on.
    """

    def __init__(self, pid, cpu2node, mem_node, interval=0.1):
        super().__init__(daemon=True)
        self.pid, self.cpu2node, self.mem_node, self.interval = pid, cpu2node, mem_node, interval
        self._stop_ev = threading.Event()
        self.samples = self.active = self.on_mem = 0

    def run(self):
        task_dir = f"/proc/{self.pid}/task"
        while not self._stop_ev.is_set():
            try:
                tids = os.listdir(task_dir)
            except OSError:
                break
            for tid in tids:
                try:
                    with open(f"{task_dir}/{tid}/stat") as f:
                        data = f.read()
                    rest = data[data.rindex(")") + 2:].split()
                    if rest[0] != "R":
                        continue
                    node = self.cpu2node.get(int(rest[36]))
                except (OSError, ValueError, IndexError):
                    continue
                self.active += 1
                if node == self.mem_node:
                    self.on_mem += 1
            self.samples += 1
            self._stop_ev.wait(self.interval)

    def stop(self):
        self._stop_ev.set()
        if self.is_alive():
            self.join(timeout=2)

    def result(self):
        if not self.samples or not self.active:
            return {"on_mem_pct": None, "avg_running": None}
        return {"on_mem_pct": 100.0 * self.on_mem / self.active,
                "avg_running": self.active / self.samples}


SCX_STATE = "/sys/kernel/sched_ext/state"


def start_sched(sched_cmd, pid):
    cmd = [arg.replace("{PID}", str(pid)) for arg in sched_cmd]
    print(f"    attaching scheduler: {' '.join(cmd)}", flush=True)
    sp = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        if os.path.exists(SCX_STATE):
            deadline = time.monotonic() + 20.0
            while read_first(SCX_STATE) != "enabled":
                if sp.poll() is not None or time.monotonic() > deadline:
                    break
                time.sleep(0.05)
        else:
            time.sleep(0.5)
        if sp.poll() is not None:
            out, _ = sp.communicate()
            raise RuntimeError(f"Scheduler exited early:\n{out}")
        if os.path.exists(SCX_STATE) and read_first(SCX_STATE) != "enabled":
            raise RuntimeError(f"sched_ext not 'enabled' after 20s (state={read_first(SCX_STATE)})")
    except BaseException:
        stop_sched(sp)
        raise
    return sp


def stop_sched(sp):
    if sp.poll() is None:
        sp.send_signal(signal.SIGINT)
    try:
        out, _ = sp.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        sp.kill()
        out, _ = sp.communicate()
    return out


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


def run_case(name, bench_cmd, sched_cmd=None, wait_ready=False, env=None,
             mem_node=None, cpu2node=None):
    print(f"==> Running [{name}]: {' '.join(bench_cmd)}", flush=True)
    system_wide = bool(sched_cmd) and not any("{PID}" in a for a in sched_cmd)
    proc = sched_proc = sampler = None
    sched_out = ""
    prefix_out = rest_out = ""
    vm0 = vm1 = {}
    try:
        if sched_cmd and system_wide:
            sched_proc = start_sched(sched_cmd, None)
        vm0 = read_vmstat()
        proc = subprocess.Popen(
            bench_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        prefix_out = wait_for_ready(proc) if wait_ready else ""
        if sched_cmd and not system_wide:
            sched_proc = start_sched(sched_cmd, proc.pid)
        if cpu2node and mem_node is not None:
            sampler = PlacementSampler(proc.pid, cpu2node, mem_node)
            sampler.start()
        rest_out, _ = proc.communicate()
        vm1 = read_vmstat()
    finally:
        if sampler:
            sampler.stop()
        if proc is not None and proc.poll() is None:
            proc.kill()
        if sched_proc:
            sched_out = stop_sched(sched_proc)

    bench_out = prefix_out + rest_out
    log_raw(f"[{name}] benchmark output", bench_out)
    migr_stats = None
    if sched_proc:
        log_raw(f"[{name}] scheduler output", sched_out)
        if "runs=" in sched_out:
            migr_stats = parse_migr_stats(sched_out)
    if proc.returncode != 0:
        print(f"WARNING: benchmark exited with {proc.returncode}:\n{bench_out}")

    extra = sampler.result() if sampler else {"on_mem_pct": None, "avg_running": None}
    for key, vkey in VMSTAT_KEYS.items():
        extra[key] = (vm1[vkey] - vm0[vkey]) if (vkey in vm0 and vkey in vm1) else None
    return bench_out, migr_stats, extra


EXTRA_KEYS = ("on_mem_pct", "avg_running", "hint_faults", "pages_migrated", "task_migrated")

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
            used = set()
            for sc in args.sched:
                argv = shlex.split(sc)
                label = base = "custom_" + os.path.basename(argv[0])
                i = 2
                while label in used:
                    label, i = f"{base}_{i}", i + 1
                used.add(label)
                cases.append((label, wrap_numactl(base_cmd, has_numactl, memnode=n0), argv))
    return cases


def run_bench(args, spec, selected, has_numactl, n0, n1, env):
    results, reps = [], []
    local_val = None
    cpu2node = cpu_to_node_map()
    for name, bcmd, scmd in build_cases(args, spec["cmd"], selected, has_numactl, n0, n1):
        vals, units, stats_list, extras = [], [], [], []
        t0 = time.monotonic()
        for rep in range(args.repeat):
            tag = f"{name} ({rep + 1}/{args.repeat})" if args.repeat > 1 else name
            r0 = time.monotonic()
            out, stats, extra = run_case(tag, bcmd, scmd, wait_ready=spec["wait_ready"], env=env,
                                         mem_node=n0, cpu2node=cpu2node)
            extras.append(extra)
            v, u = spec["parser"](out)
            reps.append({
                "case": name, "rep": rep + 1, "value": v, "unit": u or args.unit,
                **{k: (stats[k] if stats else "-") for k in ("runs", "cpu_mig", "node_mig", "forced")},
                **{k: ("" if extra[k] is None else round(extra[k], 1)) for k in EXTRA_KEYS},
                "time_s": round(time.monotonic() - r0, 1),
            })
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
        def med_extra(key, nd):
            xs = [e[key] for e in extras if e.get(key) is not None]
            if not xs:
                return "-"
            m = statistics.median(xs)
            return round(m, nd) if nd else int(round(m))

        results.append({
            "case": name, "value": med,
            "min": min(vals) if vals else None, "max": max(vals) if vals else None,
            "unit": unit, "raw": ";".join(f"{v:.6g}" for v in vals),
            "pct_local": f"{pct:.1f}%" if pct is not None else "-",
            **agg,
            "on_mem_pct": med_extra("on_mem_pct", 1), "avg_running": med_extra("avg_running", 1),
            "hint_faults": med_extra("hint_faults", 0), "pages_migrated": med_extra("pages_migrated", 0),
            "task_migrated": med_extra("task_migrated", 0),
            "time_s": round(elapsed, 1),
        })
    return results, reps


def format_table(label, results, args, n0, n1):
    threads_info = f"threads={args.threads}" if args.threads else "threads=auto"
    lines = ["=" * 110,
             f"Benchmark: {label} ({threads_info}, repeats={args.repeat}, mem_node={n0}, remote_node={n1})"]
    multi = args.repeat > 1
    if multi:
        lines.append(f"{'Case':<14} {'Median':>10} {'Min..Max':>17} {'Unit':<9} {'vs Local':>9} "
                     f"{'Runs':>8} {'CPU_Mig':>8} {'Node_Mig':>9} {'Forced':>7} {'OnMem%':>7} {'Time':>9}")
    else:
        lines.append(f"{'Case':<14} {'Score':>10} {'Unit':<9} {'vs Local':>9} "
                     f"{'Runs':>8} {'CPU_Mig':>8} {'Node_Mig':>9} {'Forced':>7} {'OnMem%':>7} {'Time':>9}")
    lines.append("-" * 110)
    for r in results:
        score = f"{r['value']:.4g}" if r["value"] is not None else "N/A"
        tail = (f"{str(r['runs']):>8} {str(r['cpu_mig']):>8} {str(r['node_mig']):>9} "
                f"{str(r['forced']):>7} {str(r['on_mem_pct']):>7} {fmt_dur(r['time_s']):>9}")
        if multi:
            rng = f"{r['min']:.4g}..{r['max']:.4g}" if r["min"] is not None else "-"
            lines.append(f"{r['case']:<14} {score:>10} {rng:>17} {r['unit']:<9} {r['pct_local']:>9} {tail}")
        else:
            lines.append(f"{r['case']:<14} {score:>10} {r['unit']:<9} {r['pct_local']:>9} {tail}")
    lines.append("=" * 110)
    return "\n".join(lines)


def write_summary(path, info, records, args, n0, n1, finished):
    total = time.monotonic() - info["t0"]
    out = []
    out.append("NUMA benchmark summary")
    out.append("=" * 110)
    out.append(f"run_id     : {info['run_id']}")
    out.append(f"started    : {info['started']}")
    out.append(f"status     : {'complete' if finished else 'INCOMPLETE (still running or aborted)'}")
    out.append(f"total time : {fmt_dur(total)}")
    out.append(f"host/kernel: {info['host']} / {info['kernel']}")
    out.append(f"nodes      : {info['nodes']}  (mem_node={n0}, remote_node={n1})")
    out.append(f"settings   : threads={args.threads or 'auto'} repeats={args.repeat} cases={info['cases']}")
    out.append(f"command    : {info['cmdline']}")
    out.append("")
    out.append("Environment")
    out.append("-" * 60)
    for k, v in info["env"].items():
        out.append(f"{k:<18}: {v}")
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
            w.writerow({k: ("" if v is None else v) for k, v in {**meta, **r}.items()})


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
    ap.add_argument("--sched", action="append", default=None,
                    help="Custom scheduler command; repeat for several. Use {PID} for per-process "
                         "schedulers, e.g. './my_sched -p {PID}'. Without {PID} it is run "
                         "system-wide (e.g. --sched scx_simple --sched scx_rusty)")
    ap.add_argument("--outdir", default=None,
                    help="Output folder for results.csv, runs.csv, full_run.log, summary.txt "
                         "(default: results/run_YYYYmmdd_HHMMSS). Must not already contain files.")
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
    outdir = args.outdir or os.path.join("results", f"run_{run_id}")
    if os.path.isdir(outdir) and os.listdir(outdir):
        sys.exit(f"--outdir '{outdir}' already exists and is not empty; refusing to overwrite")
    os.makedirs(outdir, exist_ok=True)
    csv_path = os.path.join(outdir, "results.csv")
    runs_path = os.path.join(outdir, "runs.csv")
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
    info["env"] = collect_env()
    print(f"Output folder: {outdir}", flush=True)
    print("Environment: " + ", ".join(f"{k}={v}" for k, v in info["env"].items()), flush=True)
    if info["env"]["sched_ext_state"] == "enabled":
        print("[warn] a sched_ext scheduler is ALREADY active; all cases (incl. cfs) are affected",
              flush=True)

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
                rec["results"], reps = run_bench(args, spec, selected, has_numactl, n0, n1, bench_env)
                if all(r["value"] is None for r in rec["results"]):
                    rec["status"] = "no data (parse failed)"
                print("\n" + format_table(rec["label"], rec["results"], args, n0, n1), flush=True)
                meta = {
                    "run_id": run_id, "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                    "bench": rec["label"], "threads": args.threads or "auto", "repeats": args.repeat,
                    "mem_node": n0, "remote_node": n1, "host": info["host"], "kernel": info["kernel"],
                    "numa_balancing": info["env"]["numa_balancing"], "smt": info["env"]["smt_active"],
                }
                append_csv(csv_path, meta, rec["results"])
                append_csv(runs_path, meta, reps)
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
        print(f"\nDone in {fmt_dur(total)}. Files in {outdir}/: results.csv, runs.csv, full_run.log, summary.txt",
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