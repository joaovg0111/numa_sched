Here is a clean README.md reflecting the current state of the repository,
components, usage, and roadmap:

# numa_sched

A Linux `sched_ext` (eBPF) NUMA-aware CPU scheduler project.

## Project Status

The project is in **Phase 1 (Benchmarking & Baseline Groundwork)**. Before implementing the NUMA-aware scheduler itself, this repository provides:

1. **`numa_migr` (`migr/`)**: A deliberately NUMA-oblivious `sched_ext` scheduler that forces a target process (`tgid`) to hop round-robin across NUMA nodes every $N$ milliseconds, simulating worst-case cross-node migration overhead and recording CPU/node migration telemetry.
2. **Benchmark Suite (`benchmark/`)**: A mix of microbenchmarks and standard multi-threaded workloads covering memory latency, memory bandwidth, irregular graph traversal, random DRAM lookups, and analytical SQL queries.
3. **Evaluation Harness (`bench.py`)**: A unified runner that compares any workload across hardware bounds (`local`, `remote`), stock Linux (`cfs`), controlled migration degradation (`migr`), and pluggable custom schedulers (`custom`).

### Roadmap

- [x] `numa_migr` eBPF `sched_ext` stressor & sysfs topology loader
- [x] Microbenchmarks (`membench` latency/bandwidth, McCalpin `stream` / `stream_omp`)
- [x] Standard workloads (`GAPBS`, `XSBench`, `DuckDB TPC-H`)
- [x] Automated `bench.py` harness with selectable cases, CSV export, and `--sched` hook
- [ ] Baseline NUMA sensitivity characterization on multi-socket hardware
- [ ] NUMA-aware `sched_ext` scheduler implementation (per-node DSQs & locality-preserving placement)
- [ ] Phase 2 workloads (multi-process / client–server tests such as Memcached + `memtier_benchmark`)

---

## Repository Structure

```text
.
├── Makefile              # Top-level build (builds migr + benchmarks)
├── bench.py              # Automated NUMA benchmark harness
├── migr/
│   ├── Makefile
│   ├── numa_migr.bpf.c   # sched_ext BPF scheduler (round-robin NUMA hopping)
│   └── numa_migr.c       # Userspace loader, topology parser, stats reporter
└── benchmark/
    ├── Makefile          # Builds local benchmarks + fetches external suites
    ├── membench.c        # Pointer-chasing latency & streaming bandwidth microbenchmark
    ├── stream.c          # McCalpin STREAM (single-threaded & OpenMP)
    ├── tpch.sql          # In-memory DuckDB TPC-H query workload (SF=3)
    └── ext/              # External benchmarks (GAPBS, XSBench, DuckDB CLI)

Requirements

  - Kernel: Linux with sched_ext (CONFIG_SCHED_CLASS_EXT=y) and BTF
    (CONFIG_DEBUG_INFO_BTF=y, /sys/kernel/btf/vmlinux readable).
  - Build & Runtime Tools (Debian/Ubuntu):
    sudo apt install clang llvm libbpf-dev libelf-dev zlib1g-dev \
                     gcc g++ make python3 numactl curl \
                     linux-tools-common linux-tools-$(uname -r)

Building

Clone with submodules and run make (this compiles migr/numa_migr, membench,
stream, stream_omp, fetches/builds GAPBS and XSBench, and downloads DuckDB CLI +
installs the tpch extension):

git clone --recurse-submodules <repo-url>
cd numa_sched
make

Offline servers: Run git clone --recurse-submodules and make fetch on an
internet-connected machine first, then copy the directory to the target server
and run make.

Workloads

| `--bench` key | Workload                           | Primary Bottleneck                | Metric      |
| :------------ | :--------------------------------- | :-------------------------------- | :---------- |
| `chase`       | `membench` pointer chase (1T)      | DRAM / interconnect latency       | `Macc/s`    |
| `stream`      | `membench` triad (1T)              | Single-core memory bandwidth      | `GB/s`      |
| `stream_st`   | McCalpin STREAM (1T)               | Single-core memory bandwidth      | `GB/s`      |
| `stream_omp`  | McCalpin STREAM (OpenMP)           | Multi-core aggregate bandwidth    | `GB/s`      |
| `gapbs_pr`    | GAPBS PageRank (`-g 22 -n 15`)     | Irregular graph memory access     | `sec`       |
| `gapbs_bfs`   | GAPBS BFS (`-g 22 -n 30`)          | Latency-sensitive traversal       | `sec`       |
| `gapbs_cc`    | GAPBS Connected Components         | Cross-core atomic updates         | `sec`       |
| `gapbs_sssp`  | GAPBS Shortest Paths               | Dynamic OpenMP + irregular access | `sec`       |
| `xsbench`     | XSBench (`-s large -p 2000000`)    | Random DRAM lookup latency        | `MLookup/s` |
| `duckdb_tpch` | DuckDB TPC-H (`sf=3`, Q1/3/5/9/18) | Vectorized OLAP scan/join/hash    | `sec`       |

Running Benchmarks (bench.py)

bench.py discovers NUMA nodes from /sys/devices/system/node and runs the
selected scheduling cases while keeping memory pinned to Node 0 (numactl
--membind=0):

  - local: --cpunodebind=N0 --membind=N0 (ideal upper bound)
  - remote: --cpunodebind=N1 --membind=N0 (cross-socket lower bound)
  - cfs: --membind=N0 under stock Linux CFS
  - migr: --membind=N0 under numa_migr across -i intervals (ms)
  - custom: --membind=N0 under your --sched binary

Note: numa_migr and custom intentionally use --membind=N0 without --cpunodebind,
so memory stays allocated on Node 0 while p->cpus_ptr remains open for the eBPF
scheduler to place threads across nodes.

Examples

# Run full suite (local, remote, cfs, numa_migr sweep) on GAPBS PageRank
sudo ./bench.py --bench gapbs_pr --csv pr_results.csv

# Run only baselines without sudo
./bench.py --bench xsbench -c local,remote,cfs

# Sweep specific migration intervals on pointer-chasing latency
sudo ./bench.py --bench chase -s 1024 -t 15 -c local,remote,migr -i 0,100,20,5,1

# Test a custom NUMA-aware scheduler (with optional local/cfs comparison)
sudo ./bench.py --bench duckdb_tpch -c local,cfs,custom --sched "./my_sched -p {PID}"

# Run an arbitrary external benchmark command
sudo ./bench.py --cmd "./benchmark/ext/XSBench/openmp-threading/XSBench -s XL -p 5000000" \
  --regex "Lookups/s:\s+([\d,]+)" --unit "Lookup/s" -c local,cfs,custom --sched "./my_sched -p {PID}"

