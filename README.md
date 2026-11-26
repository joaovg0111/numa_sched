# numa_sched

A Linux `sched_ext` (eBPF) NUMA-aware CPU scheduler project.

## Project status

The project is in **Phase 1 (benchmarking and baseline groundwork)**. Before the NUMA-aware scheduler itself is implemented, this repository provides:

1. **`numa_migr`** (`migr/`): a deliberately NUMA-oblivious `sched_ext` scheduler that forces a target process (`tgid`) to hop round-robin across NUMA nodes every *N* ms. It is a stressor that simulates worst-case cross-node migration cost and records CPU/node migration telemetry. It is **not** a candidate scheduler.
2. **Benchmark suite** (`benchmark/`): microbenchmarks and standard multi-threaded workloads covering memory latency, memory bandwidth, irregular graph traversal, random DRAM lookups and analytical SQL.
3. **Evaluation harness** (`bench.py`): runs any workload under hardware bounds (`local`, `remote`), the stock Linux scheduler (`cfs`), controlled migration (`migr`) and pluggable custom schedulers (`custom`), and writes everything to one results folder.

### Roadmap

- [x] `numa_migr` eBPF `sched_ext` stressor and sysfs topology loader
- [x] Microbenchmarks (`membench` latency/bandwidth, McCalpin `stream` / `stream_omp`)
- [x] Standard workloads (GAPBS, XSBench, DuckDB TPC-H)
- [x] `bench.py` harness: selectable cases, per-run results folder (CSV, log, summary), `--sched` hook
- [ ] Baseline NUMA sensitivity characterization on multi-socket hardware
- [ ] NUMA-aware `sched_ext` scheduler (per-node DSQs, locality-preserving placement, load balancing)
- [ ] Phase 2 workloads (multi-process / client-server, e.g. Memcached + `memtier_benchmark`)

---

## Repository structure

```text
.
├── Makefile              # Top-level build (migr + benchmarks)
├── bench.py              # Benchmark harness
├── run.sh                # Quick manual demo: membench chase, optionally under numa_migr
├── migr/
│   ├── Makefile
│   ├── numa_migr.bpf.c   # sched_ext BPF scheduler (round-robin NUMA hopping)
│   └── numa_migr.c       # Userspace loader, topology parser, stats reporter
├── benchmark/
│   ├── Makefile          # Builds local benchmarks, fetches external suites
│   ├── membench.c        # Pointer-chasing latency / streaming bandwidth microbenchmark
│   ├── stream.c          # McCalpin STREAM (single-threaded and OpenMP builds)
│   ├── tpch.sql          # DuckDB TPC-H workload (SF=3)
│   └── ext/              # External suites (GAPBS, XSBench, DuckDB CLI), created by make
└── results/              # Output of bench.py (git-ignored)
```

## Requirements

- Kernel with `sched_ext` (`CONFIG_SCHED_CLASS_EXT=y`) and BTF (`CONFIG_DEBUG_INFO_BTF=y`, `/sys/kernel/btf/vmlinux` readable).
- A machine with **at least 2 NUMA nodes** for the `remote` and `migr` cases (single-node hosts can only run `local`/`cfs`).
- Build and runtime tools (Debian/Ubuntu):

```bash
sudo apt install clang llvm libbpf-dev libelf-dev zlib1g-dev \
                 gcc g++ make python3 numactl curl \
                 linux-tools-common linux-tools-$(uname -r)
```

## Building

```bash
git clone --recurse-submodules <repo-url>
cd numa_sched
make
```

`make` compiles `numa_migr`, `membench`, `stream` and `stream_omp`, builds GAPBS and XSBench, and downloads the DuckDB CLI with its `tpch` extension.

Offline servers: run `git clone --recurse-submodules` and `make fetch` on a connected machine, copy the directory to the target server, then run `make`.

---

## Benchmarks

Select one with `--bench <key>`, several with `--bench a,b,c`, or everything with `--bench all`.

| `--bench` key | Workload | What it stresses | Metric (better) |
| :-- | :-- | :-- | :-- |
| `chase` | `membench` pointer chase, 1 thread, 1 GB buffer | Every load depends on the previous one in a random cycle, so it measures raw DRAM / interconnect **latency** | Macc/s (higher) |
| `stream` | `membench` streaming, 1 thread | Single-core memory **bandwidth** | GB/s (higher) |
| `stream_st` | McCalpin STREAM, 1 thread (40M-element arrays, 80 iterations) | Single-core memory bandwidth, reports Triad | GB/s (higher) |
| `stream_omp` | McCalpin STREAM, OpenMP | **Aggregate** bandwidth of all threads; threads wait at a barrier after each kernel, so the slowest thread sets the pace | GB/s (higher) |
| `gapbs_pr` | GAPBS PageRank (`-g 22 -n 15`) | Iterative sweeps over a graph; mostly streaming reads plus irregular gathers | sec (lower) |
| `gapbs_bfs` | GAPBS BFS (`-g 22 -n 30`) | Irregular, latency-sensitive traversal; very short trials (about 20 ms) | sec (lower) |
| `gapbs_cc` | GAPBS Connected Components (`-g 22 -n 15`) | Irregular access plus cross-thread atomic updates | sec (lower) |
| `gapbs_sssp` | GAPBS Shortest Paths (`-g 22 -n 15`) | Irregular access with dynamic OpenMP work distribution | sec (lower) |
| `xsbench` | XSBench, OpenMP (`-s large`) | Monte Carlo neutron-transport proxy: random lookups into large tables | MLookup/s (higher) |
| `duckdb_tpch` | DuckDB, in-memory TPC-H SF=3, Q1/3/5/9/18 run twice | Vectorized OLAP scans, joins and hash aggregation; reports the sum of query times | sec (lower) |

GAPBS uses a synthetic Kronecker graph with 2^22 vertices (`-g 22`). `chase` and the two single-thread STREAM variants ignore `--threads`.

### Command used for the full run

```bash
sudo ./bench.py --bench all --threads 16 --repeat 3
```

This runs every benchmark above with 16 threads and 3 repetitions per case. On our 2-node test machine it took about 1 h 10 min. For long runs, detach it; all output goes to the results folder anyway:

```bash
sudo nohup ./bench.py --bench all --threads 16 --repeat 3 > /dev/null 2>&1 < /dev/null &
```

---

## Cases

`bench.py` discovers NUMA nodes from `/sys/devices/system/node` and keeps memory bound to node 0 (`numactl --membind=0`) in every case:

| Case | Setup | Role |
| :-- | :-- | :-- |
| `local` | `--cpunodebind=0 --membind=0` | All threads on the memory node. Best-case **locality**, but see the note below: it is not always the fastest possible result |
| `remote` | `--cpunodebind=1 --membind=0` | All threads on the other node. Worst-case locality |
| `cfs` | `--membind=0`, no CPU pinning | **Stock Linux scheduler: the baseline a NUMA-aware scheduler has to beat.** (On kernels ≥ 6.6 the fair class is EEVDF; the label `cfs` is historical) |
| `migr` | `--membind=0` under `numa_migr`, swept over `-i` intervals (ms) | Stressor: forced cross-node hopping every *N* ms. Expected to be slower than `cfs` |
| `custom` | `--membind=0` under your `--sched` command | Your scheduler under test |

`migr` and `custom` use `--membind=0` without `--cpunodebind`, so memory stays on node 0 while the CPU affinity mask stays open for the eBPF scheduler to place threads on any node.

### Reading the results

- **`vs Local`** is relative to the `local` median (for time-based metrics it is inverted, so higher is always better). Values above 100% are possible: `local` confines all threads to one node's CPUs, while `cfs` can also use the other node's cores and cache.
- **`migr_0ms`** (interval 0) does *not* mean "CFS without forced migration". `numa_migr` keeps each target thread on the CPU it already had and never rebalances, so this case mostly measures where threads happened to be placed. Treat it as a "sched_ext with no load balancing" control.
- **Medians can hide bimodal results.** For single-threaded benchmarks the default scheduler sometimes lands on the remote node; check `Min..Max` or `runs.csv`.

---

## Running benchmarks

`bench.py` creates **one folder per invocation**: `--outdir`, or `results/run_YYYYmmdd_HHMMSS` by default. It refuses to reuse a folder that already contains files.

| File | Content |
| :-- | :-- |
| `summary.txt` | Run info, **time taken per benchmark**, and the result tables |
| `results.csv` | One row per case: median, min, max, `vs Local`, migration stats, time, plus metadata (`run_id`, `bench`, `threads`, `kernel`, ...) |
| `runs.csv` | One row per repetition (raw value, migration stats, time). Use this for mean/min/max/std |
| `full_run.log` | Everything printed to the console plus the raw output of every benchmark and scheduler run |

The CSV and summary are updated after each benchmark, so an interrupted run still leaves usable files (the summary is marked `INCOMPLETE`). A benchmark that fails is marked `FAILED` and the run continues. When started with `sudo`, the folder is handed back to the invoking user.

Mean/min/max over repetitions:

```python
import pandas as pd

r = pd.read_csv("results/run_YYYYmmdd_HHMMSS/runs.csv")
print(r.groupby(["bench", "case"])["value"].agg(["mean", "min", "max", "std"]))
```

### Examples

```bash
# Full suite (local, remote, cfs, numa_migr sweep) on GAPBS PageRank
sudo ./bench.py --bench gapbs_pr

# Several benchmarks into a named folder
sudo ./bench.py --bench chase,stream_omp --repeat 3 --outdir results/quick

# Baselines only, without sudo
./bench.py --bench xsbench -c local,remote,cfs

# Sweep specific migration intervals on pointer-chasing latency
sudo ./bench.py --bench chase -s 1024 -t 15 -c local,remote,migr -i 0,100,20,5,1

# Test a custom NUMA-aware scheduler against local and cfs
sudo ./bench.py --bench duckdb_tpch -c local,cfs,custom --sched "./my_sched -p {PID}"

# Arbitrary external benchmark command
sudo ./bench.py --cmd "./benchmark/ext/XSBench/openmp-threading/XSBench -s XL -p 5000000" \
  --regex "Lookups/s:\s+([\d,]+)" --unit "Lookup/s" -c local,cfs,custom --sched "./my_sched -p {PID}"
```
