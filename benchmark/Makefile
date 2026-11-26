CC      ?= gcc
CFLAGS  := -O3 -Wall -Wextra -march=native
TARGETS := membench stream stream_omp

GAPBS_URL   := https://github.com/sbeamer/gapbs.git
XSBENCH_URL := https://github.com/ANL-CESAR/XSBench.git

ARCH_RAW    := $(shell uname -m)
DUCKDB_ARCH := $(if $(filter aarch64 arm64,$(ARCH_RAW)),arm64,amd64)
DUCKDB_URL  := https://github.com/duckdb/duckdb/releases/latest/download/duckdb-cli-linux-$(DUCKDB_ARCH).tar.gz

GAPBS_DIR   := ext/gapbs
XSBENCH_DIR := ext/XSBench
DUCKDB_DIR  := ext/duckdb
DUCKDB_BIN  := $(DUCKDB_DIR)/duckdb
DUCKDB_EXT  := $(DUCKDB_DIR)/extensions

.PHONY: all fetch gapbs xsbench duckdb clean distclean

all: $(TARGETS) gapbs xsbench duckdb

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

$(DUCKDB_EXT)/.tpch_installed:
	@mkdir -p $(DUCKDB_DIR) $(DUCKDB_EXT)
	@rm -f "$(DUCKDB_DIR)/duckdb.zip"
	@if [ ! -x "$(DUCKDB_BIN)" ]; then \
		echo "Downloading DuckDB CLI ($(DUCKDB_ARCH))..."; \
		(curl -fsSL "$(DUCKDB_URL)" -o "$(DUCKDB_DIR)/duckdb.tar.gz" || \
		 wget -O "$(DUCKDB_DIR)/duckdb.tar.gz" "$(DUCKDB_URL)") && \
		tar -xzf "$(DUCKDB_DIR)/duckdb.tar.gz" -C "$(DUCKDB_DIR)" && \
		rm -f "$(DUCKDB_DIR)/duckdb.tar.gz" && \
		chmod +x "$(DUCKDB_BIN)"; \
	fi
	$(DUCKDB_BIN) -c "SET extension_directory = '$(DUCKDB_EXT)'; INSTALL tpch;"
	@touch $@

fetch: $(GAPBS_DIR)/Makefile $(XSBENCH_DIR)/openmp-threading/Makefile $(DUCKDB_EXT)/.tpch_installed

gapbs: $(GAPBS_DIR)/Makefile
	$(MAKE) -C $(GAPBS_DIR) pr bfs cc sssp

xsbench: $(XSBENCH_DIR)/openmp-threading/Makefile
	$(MAKE) -C $(XSBENCH_DIR)/openmp-threading

duckdb: $(DUCKDB_EXT)/.tpch_installed

clean:
	rm -f $(TARGETS)
	@test ! -f $(GAPBS_DIR)/Makefile || $(MAKE) -C $(GAPBS_DIR) clean
	@test ! -f $(XSBENCH_DIR)/openmp-threading/Makefile || $(MAKE) -C $(XSBENCH_DIR)/openmp-threading clean

distclean: clean
	rm -rf ext