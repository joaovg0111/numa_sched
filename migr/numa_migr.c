/*
 * numa_migr: sched_ext scheduler that forces one process to hop between
 * NUMA nodes every N ms, to measure the cost of a NUMA-oblivious scheduler.
 *
 * Self-contained: needs only vmlinux.h (from the running kernel) and libbpf's
 * bpf_helpers.h. No scx repo headers.
 */
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>

char _license[] SEC("license") = "GPL";

#define BPF_STRUCT_OPS(name, args...) \
	SEC("struct_ops/"#name) BPF_PROG(name, ##args)

/* Hide a value from the compiler so a verifier-required check isn't removed. */
#define barrier_var(var) asm volatile("" : "+r"(var))

#define MAX_NODES 8
#define MAX_CPUS  256

/* sched_ext constants (ABI-stable values from include/linux/sched/ext.h) */
#define NM_DSQ_BUILTIN	(1ULL << 63)
#define NM_DSQ_GLOBAL	(NM_DSQ_BUILTIN | 1)
#define NM_DSQ_LOCAL_ON	(NM_DSQ_BUILTIN | (1ULL << 62))
#define NM_SLICE_DFL	(20ULL * 1000 * 1000)	/* 20 ms */

/*
 * kfuncs. If your kernel names/signatures differ, check with:
 *   bpftool btf dump file /sys/kernel/btf/vmlinux | grep -E 'scx_bpf_(dsq_insert|select_cpu|task_cpu)'
 * and copy the declarations from your working minimal scheduler.
 */
s32  scx_bpf_select_cpu_dfl(struct task_struct *p, s32 prev_cpu,
			    u64 wake_flags, bool *is_idle) __ksym;
void scx_bpf_dsq_insert(struct task_struct *p, u64 dsq_id, u64 slice,
			u64 enq_flags) __ksym;
s32  scx_bpf_task_cpu(const struct task_struct *p) __ksym;
bool bpf_cpumask_test_cpu(u32 cpu, const struct cpumask *cpumask) __ksym;

/* ---- configuration, filled by userspace before load ---- */
const volatile u32 target_tgid;
const volatile u64 migrate_interval_ns;
const volatile u32 nr_nodes = 1;
const volatile u16 cpu_node[MAX_CPUS];
const volatile u16 node_ncpus[MAX_NODES];
const volatile u16 node_cpus[MAX_NODES][MAX_CPUS];

/* ---- stats / exit info, read by userspace ---- */
u64 stat_runs;
u64 stat_cpu_migrations;
u64 stat_node_migrations;
u64 stat_forced_moves;

u64 exited;
s64 exit_kind;
s64 exit_code;
char exit_reason[128];
char exit_msg[256];

struct task_ctx {
	u64 last_move_ns;
	s32 last_cpu;
	u32 last_node;
	bool seen;
};

struct {
	__uint(type, BPF_MAP_TYPE_TASK_STORAGE);
	__uint(map_flags, BPF_F_NO_PREALLOC);
	__type(key, int);
	__type(value, struct task_ctx);
} task_ctxs SEC(".maps");

static __always_inline bool is_target(struct task_struct *p)
{
	return target_tgid && p->tgid == target_tgid;
}

static __always_inline u32 node_of(s32 cpu)
{
	if (cpu < 0 || cpu >= MAX_CPUS)
		return 0;
	return cpu_node[cpu];
}

/* Random allowed CPU inside @node, or -1 if none. */
static s32 pick_cpu_in_node(struct task_struct *p, u32 node)
{
	u32 n, i, j, start;
	s32 cpu;

	if (node >= MAX_NODES)
		return -1;
	n = node_ncpus[node];
	if (!n || n > MAX_CPUS)
		return -1;

	start = bpf_get_prandom_u32() % n;
	for (i = 0; i < MAX_CPUS; i++) {
		if (i >= n)
			break;
		j = (start + i) % n;
		barrier_var(j);	/* keep the bounds check below from being optimized out */
		if (j >= MAX_CPUS)
			break;
		cpu = node_cpus[node][j];
		if (bpf_cpumask_test_cpu(cpu, p->cpus_ptr))
			return cpu;
	}
	return -1;
}

s32 BPF_STRUCT_OPS(numa_select_cpu, struct task_struct *p, s32 prev_cpu,
		   u64 wake_flags)
{
	bool idle = false;
	s32 cpu;

	/* Target: keep prev_cpu, real placement happens in enqueue. */
	if (is_target(p))
		return prev_cpu;

	cpu = scx_bpf_select_cpu_dfl(p, prev_cpu, wake_flags, &idle);
	if (idle)
		scx_bpf_dsq_insert(p, NM_DSQ_LOCAL_ON | cpu, NM_SLICE_DFL, 0);
	return cpu;
}

void BPF_STRUCT_OPS(numa_enqueue, struct task_struct *p, u64 enq_flags)
{
	struct task_ctx *tc;
	u64 slice = NM_SLICE_DFL;
	s32 cpu;
	u32 cur, k;

	if (!is_target(p)) {
		scx_bpf_dsq_insert(p, NM_DSQ_GLOBAL, NM_SLICE_DFL, enq_flags);
		return;
	}

	cpu = scx_bpf_task_cpu(p);
	tc = bpf_task_storage_get(&task_ctxs, p, 0,
				  BPF_LOCAL_STORAGE_GET_F_CREATE);

	if (tc && migrate_interval_ns) {
		u64 now = bpf_ktime_get_ns();

		slice = migrate_interval_ns;	/* make it come back to us */

		if (now - tc->last_move_ns >= migrate_interval_ns) {
			cur = node_of(cpu);
			/* next non-empty node, round-robin */
			for (k = 1; k < MAX_NODES; k++) {
				u32 nxt;
				s32 c;

				if (k >= nr_nodes)
					break;
				nxt = (cur + k) % nr_nodes;
				c = pick_cpu_in_node(p, nxt);
				if (c >= 0) {
					cpu = c;
					tc->last_move_ns = now;
					__sync_fetch_and_add(&stat_forced_moves, 1);
					break;
				}
			}
		}
	}

	scx_bpf_dsq_insert(p, NM_DSQ_LOCAL_ON | cpu, slice, enq_flags);
}

/* Measure where the task actually runs. */
void BPF_STRUCT_OPS(numa_running, struct task_struct *p)
{
	struct task_ctx *tc;
	s32 cpu;
	u32 node;

	if (!is_target(p))
		return;
	tc = bpf_task_storage_get(&task_ctxs, p, 0,
				  BPF_LOCAL_STORAGE_GET_F_CREATE);
	if (!tc)
		return;

	cpu = bpf_get_smp_processor_id();
	node = node_of(cpu);

	__sync_fetch_and_add(&stat_runs, 1);
	if (tc->seen) {
		if (cpu != tc->last_cpu)
			__sync_fetch_and_add(&stat_cpu_migrations, 1);
		if (node != tc->last_node)
			__sync_fetch_and_add(&stat_node_migrations, 1);
	}
	tc->seen = true;
	tc->last_cpu = cpu;
	tc->last_node = node;
}

void BPF_STRUCT_OPS(numa_exit, struct scx_exit_info *ei)
{
	exit_kind = BPF_CORE_READ(ei, kind);
	exit_code = BPF_CORE_READ(ei, exit_code);
	bpf_probe_read_kernel_str(exit_reason, sizeof(exit_reason),
				  BPF_CORE_READ(ei, reason));
	bpf_probe_read_kernel_str(exit_msg, sizeof(exit_msg),
				  BPF_CORE_READ(ei, msg));
	exited = 1;
}

SEC(".struct_ops.link")
struct sched_ext_ops numa_ops = {
	.select_cpu = (void *)numa_select_cpu,
	.enqueue    = (void *)numa_enqueue,
	.running    = (void *)numa_running,
	.exit       = (void *)numa_exit,
	.name       = "numa_migr",
};