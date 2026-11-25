#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <unistd.h>
#include <stdint.h>
#include <stdbool.h>
#include <bpf/libbpf.h>

/* The generated skeleton uses kernel-style type names from vmlinux.h. */
typedef uint8_t  u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int8_t   s8;
typedef int16_t  s16;
typedef int32_t  s32;
typedef int64_t  s64;

#include "numa_migr.bpf.skel.h"

static volatile sig_atomic_t exit_req;
static void sigint_handler(int sig) { exit_req = 1; }

static int libbpf_print_fn(enum libbpf_print_level l, const char *f, va_list a)
{
	return l == LIBBPF_DEBUG ? 0 : vfprintf(stderr, f, a);
}

/*
 * Read the NUMA topology from sysfs (no libnuma needed):
 *   /sys/devices/system/node/nodeN/cpulist  ->  "0-7,16-23"
 * Returns the number of nodes (highest node id + 1), or 0 on failure.
 */
static int load_topology(struct numa_migr *skel)
{
	int max_cpus  = sizeof(skel->rodata->cpu_node) / sizeof(skel->rodata->cpu_node[0]);
	int max_nodes = sizeof(skel->rodata->node_ncpus) / sizeof(skel->rodata->node_ncpus[0]);
	int nr_nodes = 0;

	for (int n = 0; n < max_nodes; n++) {
		char path[128], buf[4096];
		FILE *f;

		snprintf(path, sizeof(path),
			 "/sys/devices/system/node/node%d/cpulist", n);
		f = fopen(path, "r");
		if (!f)
			continue;
		if (!fgets(buf, sizeof(buf), f)) {
			fclose(f);
			continue;
		}
		fclose(f);
		nr_nodes = n + 1;

		for (char *tok = strtok(buf, ",\n"); tok; tok = strtok(NULL, ",\n")) {
			int lo, hi;

			if (sscanf(tok, "%d-%d", &lo, &hi) != 2) {
				if (sscanf(tok, "%d", &lo) != 1)
					continue;
				hi = lo;
			}
			for (int c = lo; c <= hi; c++) {
				if (c < 0 || c >= max_cpus) {
					fprintf(stderr, "warning: CPU %d >= MAX_CPUS, ignored\n", c);
					continue;
				}
				skel->rodata->cpu_node[c] = n;
				skel->rodata->node_cpus[n][skel->rodata->node_ncpus[n]++] = c;
			}
		}
	}
	return nr_nodes;
}

int main(int argc, char **argv)
{
	struct numa_migr *skel;
	struct bpf_link *link;
	unsigned long pid = 0, interval_ms = 0, req_nodes = 0;
	int opt, err;

	while ((opt = getopt(argc, argv, "p:i:n:h")) != -1) {
		switch (opt) {
		case 'p': pid = strtoul(optarg, NULL, 0); break;
		case 'i': interval_ms = strtoul(optarg, NULL, 0); break;
		case 'n': req_nodes = strtoul(optarg, NULL, 0); break;
		default:
			fprintf(stderr,
				"usage: %s -p <tgid> [-i <migrate_ms, 0=never>] [-n <nodes to hop among, default all>]\n",
				argv[0]);
			return opt == 'h' ? 0 : 1;
		}
	}
	if (!pid) { fprintf(stderr, "need -p <tgid>\n"); return 1; }

	signal(SIGINT, sigint_handler);
	signal(SIGTERM, sigint_handler);
	libbpf_set_print(libbpf_print_fn);

	skel = numa_migr__open();
	if (!skel) { fprintf(stderr, "failed to open skeleton\n"); return 1; }

	/* Topology -> rodata */
	int nr_nodes = load_topology(skel);

	if (nr_nodes <= 0) {
		fprintf(stderr, "no NUMA nodes found in /sys/devices/system/node\n");
		numa_migr__destroy(skel);
		return 1;
	}
	if (req_nodes) {
		if (req_nodes > (unsigned long)nr_nodes) {
			fprintf(stderr, "-n %lu requested but machine has only %d nodes\n",
				req_nodes, nr_nodes);
			numa_migr__destroy(skel);
			return 1;
		}
		nr_nodes = req_nodes;	/* hop only among nodes 0..req_nodes-1 */
	}
	skel->rodata->nr_nodes = nr_nodes;
	skel->rodata->target_tgid = pid;
	skel->rodata->migrate_interval_ns = (unsigned long long)interval_ms * 1000000ULL;

	err = numa_migr__load(skel);
	if (err) {
		fprintf(stderr, "failed to load: %d\n", err);
		numa_migr__destroy(skel);
		return 1;
	}

	link = bpf_map__attach_struct_ops(skel->maps.numa_ops);
	if (!link) {
		fprintf(stderr, "failed to attach struct_ops (is sched_ext enabled? root?)\n");
		numa_migr__destroy(skel);
		return 1;
	}

	printf("target=%lu interval=%lums nodes=%u\n", pid, interval_ms,
	       skel->rodata->nr_nodes);
	while (!exit_req && !skel->bss->exited) {
		printf("runs=%llu cpu_mig=%llu node_mig=%llu forced=%llu\n",
		       skel->bss->stat_runs, skel->bss->stat_cpu_migrations,
		       skel->bss->stat_node_migrations, skel->bss->stat_forced_moves);
		fflush(stdout);
		sleep(1);
	}

	bpf_link__destroy(link);
	if (skel->bss->exited)
		fprintf(stderr, "scheduler exited: kind=%lld code=%lld reason=%s msg=%s\n",
			(long long)skel->bss->exit_kind,
			(long long)skel->bss->exit_code,
			skel->bss->exit_reason, skel->bss->exit_msg);
	numa_migr__destroy(skel);
	return 0;
}