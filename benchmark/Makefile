CC      ?= gcc
CFLAGS  := -O3 -Wall -Wextra -march=native
TARGETS := membench stream stream_omp

GAPBS_URL   := https://github.com/sbeamer/gapbs.git
XSBENCH_URL := https://github.com/ANL-CESAR/XSBench.git

GAPBS_DIR   := ext/gapbs
XSBENCH_DIR := ext/XSBench

.PHONY: all fetch gapbs xsbench clean distclean

all: $(TARGETS) gapbs xsbench

membench: membench.c
	$(CC) $(CFLAGS) $< -o $@

stream: stream.c
	$(CC) $(CFLAGS) -DSTREAM_ARRAY_SIZE=40000000 -DNTIMES=80 $< -o $@ -lm

stream_omp: stream.c
	$(CC) $(CFLAGS) -fopenmp -DSTREAM_ARRAY_SIZE=40000000 -DNTIMES=80 $< -o $@ -lm

$(GAPBS_DIR)/Makefile:
	@mkdir -p ext
	@if git -C .. submodule status benchmark/$(GAPBS_DIR) >/dev/null 2>&1; then \
		git -C .. submodule update --init --recursive benchmark/$(GAPBS_DIR); \
	else \
		git clone --depth 1 $(GAPBS_URL) $(GAPBS_DIR); \
	fi

$(XSBENCH_DIR)/openmp-threading/Makefile:
	@mkdir -p ext
	@if git -C .. submodule status benchmark/$(XSBENCH_DIR) >/dev/null 2>&1; then \
		git -C .. submodule update --init --recursive benchmark/$(XSBENCH_DIR); \
	else \
		git clone --depth 1 $(XSBENCH_URL) $(XSBENCH_DIR); \
	fi

fetch: $(GAPBS_DIR)/Makefile $(XSBENCH_DIR)/openmp-threading/Makefile

gapbs: $(GAPBS_DIR)/Makefile
	$(MAKE) -C $(GAPBS_DIR) pr bfs cc sssp

xsbench: $(XSBENCH_DIR)/openmp-threading/Makefile
	$(MAKE) -C $(XSBENCH_DIR)/openmp-threading

clean:
	rm -f $(TARGETS)
	@test ! -f $(GAPBS_DIR)/Makefile || $(MAKE) -C $(GAPBS_DIR) clean
	@test ! -f $(XSBENCH_DIR)/openmp-threading/Makefile || $(MAKE) -C $(XSBENCH_DIR)/openmp-threading clean

distclean: clean
	rm -rf ext