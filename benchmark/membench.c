/*
 * membench: single-threaded, memory-bound benchmark that prints its work rate
 * every second, so you can watch it drop when a scheduler starts moving the
 * task between NUMA nodes.
 *
 *   chase : dependent loads over a random cyclic permutation (one cache line
 *           per element). Latency-bound; reports Maccess/s and ns/access.
 *   stream: a[i] = b[i] + s*c[i] over three arrays. Bandwidth-bound; GB/s.
 *
 * Memory is allocated and first-touched at startup, so run it under
 *   numactl --cpunodebind=0 --membind=0 ./membench ...
 * to put all pages on node 0.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <time.h>
#include <signal.h>
#include <unistd.h>

static volatile sig_atomic_t stop;
static void on_sig(int s) { (void)s; stop = 1; }

static volatile uint64_t sink_u;
static volatile double sink_d;

static double now(void)
{
	struct timespec ts;

	clock_gettime(CLOCK_MONOTONIC, &ts);
	return ts.tv_sec + ts.tv_nsec * 1e-9;
}

static uint64_t rng_state = 88172645463325252ULL;
static uint64_t rng(void)	/* xorshift64 */
{
	uint64_t x = rng_state;

	x ^= x << 13;
	x ^= x >> 7;
	x ^= x << 17;
	return rng_state = x;
}

struct line {
	uint64_t next;
	char pad[56];
};

/* ---- sample bookkeeping ---- */
static double *samples;
static size_t nsamples, cap_samples = 1 << 16;
static int csv;
static double warmup = 2.0;

static void emit(const char *mode, double t, double rate)
{
	int is_chase = !strcmp(mode, "chase");

	if (csv) {
		printf("%.1f,%.4f\n", t, rate);
	} else if (is_chase) {
		printf("[%6.1fs] chase  %8.3f Macc/s  %7.1f ns/access\n",
		       t, rate, 1e3 / rate);
	} else {
		printf("[%6.1fs] stream %8.3f GB/s\n", t, rate);
	}
	fflush(stdout);

	if (t >= warmup && nsamples < cap_samples)
		samples[nsamples++] = rate;
}

static int cmp_d(const void *a, const void *b)
{
	double x = *(const double *)a, y = *(const double *)b;

	return (x > y) - (x < y);
}

static void summary(const char *mode)
{
	double sum = 0;

	if (!nsamples) {
		fprintf(stderr, "no samples after %.0fs warmup\n", warmup);
		return;
	}
	for (size_t i = 0; i < nsamples; i++)
		sum += samples[i];
	qsort(samples, nsamples, sizeof(double), cmp_d);
	printf("SUMMARY mode=%s samples=%zu mean=%.4f median=%.4f min=%.4f max=%.4f\n",
	       mode, nsamples, sum / nsamples, samples[nsamples / 2],
	       samples[0], samples[nsamples - 1]);
}

/* ---- chase ---- */
static void run_chase(size_t bytes, double duration)
{
	size_t n = bytes / sizeof(struct line);
	struct line *arr = aligned_alloc(64, n * sizeof(struct line));
	uint64_t p = 0, cnt = 0;
	const uint64_t batch = 1 << 16;

	if (!arr) { perror("alloc"); exit(1); }
	/* touch every page in order (first-touch placement), identity chain */
	for (size_t i = 0; i < n; i++) {
		arr[i].next = i;
		arr[i].pad[0] = 0;
	}
	/* Sattolo: yields one single cycle covering all n elements */
	for (size_t i = n - 1; i > 0; i--) {
		size_t j = rng() % i;
		uint64_t t = arr[i].next;

		arr[i].next = arr[j].next;
		arr[j].next = t;
	}

	printf("pid=%d mode=chase size=%zuMB ready\n", getpid(), bytes >> 20);
	fflush(stdout);

	double t0 = now(), tlast = t0;

	while (!stop) {
		for (uint64_t i = 0; i < batch; i++)
			p = arr[p].next;
		cnt += batch;
		double t = now();

		if (t - tlast >= 1.0) {
			emit("chase", t - t0, cnt / (t - tlast) / 1e6);
			cnt = 0;
			tlast = t;
			if (duration > 0 && t - t0 >= duration)
				break;
		}
	}
	sink_u = p;
	free(arr);
}

/* ---- stream ---- */
static void run_stream(size_t bytes, double duration)
{
	size_t m = bytes / 3 / sizeof(double);
	double *a = aligned_alloc(64, m * sizeof(double));
	double *b = aligned_alloc(64, m * sizeof(double));
	double *c = aligned_alloc(64, m * sizeof(double));
	double s = 3.0;
	double moved = 0;

	if (!a || !b || !c) { perror("alloc"); exit(1); }
	for (size_t i = 0; i < m; i++) {
		a[i] = 0.0;
		b[i] = 1.0;
		c[i] = 2.0;
	}

	printf("pid=%d mode=stream size=%zuMB ready\n", getpid(), bytes >> 20);
	fflush(stdout);

	double t0 = now(), tlast = t0;

	while (!stop) {
		s += 1e-9;
		for (size_t i = 0; i < m; i++)
			a[i] = b[i] + s * c[i];
		moved += 3.0 * m * sizeof(double);
		double t = now();

		if (t - tlast >= 1.0) {
			emit("stream", t - t0, moved / (t - tlast) / 1e9);
			moved = 0;
			tlast = t;
			if (duration > 0 && t - t0 >= duration)
				break;
		}
	}
	sink_d = a[m / 2];
	free(a); free(b); free(c);
}

int main(int argc, char **argv)
{
	const char *mode = "chase";
	size_t mb = 1024;
	double duration = 0;
	int opt;

	while ((opt = getopt(argc, argv, "m:s:t:w:ch")) != -1) {
		switch (opt) {
		case 'm': mode = optarg; break;
		case 's': mb = strtoul(optarg, NULL, 0); break;
		case 't': duration = atof(optarg); break;
		case 'w': warmup = atof(optarg); break;
		case 'c': csv = 1; break;
		default:
			fprintf(stderr,
				"usage: %s [-m chase|stream] [-s size_MB=1024] [-t seconds, 0=forever]\n"
				"          [-w warmup_s=2 excluded from summary] [-c csv output]\n",
				argv[0]);
			return opt == 'h' ? 0 : 1;
		}
	}
	if (strcmp(mode, "chase") && strcmp(mode, "stream")) {
		fprintf(stderr, "mode must be chase or stream\n");
		return 1;
	}
	samples = malloc(cap_samples * sizeof(double));
	if (!samples) return 1;

	signal(SIGINT, on_sig);
	signal(SIGTERM, on_sig);

	if (!strcmp(mode, "chase"))
		run_chase(mb << 20, duration);
	else
		run_stream(mb << 20, duration);

	summary(mode);
	return 0;
}