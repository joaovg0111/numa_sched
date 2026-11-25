# Makefile for numa_migr (self-contained, no scx repo needed)
#
#   make
#   make run PID=1234 INTERVAL=10
#
# Dependencies (Debian/Ubuntu):
#   apt install clang llvm libbpf-dev libnuma-dev libelf-dev zlib1g-dev \
#               linux-tools-common linux-tools-$(uname -r)   # bpftool

BUILD_DIR := build

CLANG   ?= clang
BPFTOOL ?= bpftool
CC      ?= gcc

ARCH := $(shell uname -m | sed -e 's/x86_64/x86/' -e 's/aarch64/arm64/' \
                               -e 's/ppc64le/powerpc/' -e 's/riscv64/riscv/' \
                               -e 's/loongarch64/loongarch/')

VMLINUX_H  := $(BUILD_DIR)/vmlinux.h
BPF_OBJ    := $(BUILD_DIR)/numa_migr.bpf.o
BPF_LINKED := $(BUILD_DIR)/numa_migr.bpf.linked.o
SKEL_H     := $(BUILD_DIR)/numa_migr.bpf.skel.h
TARGET     := numa_migr

BPF_CFLAGS := -g -O2 -Wall -Wno-missing-declarations \
              -Wno-compare-distinct-pointer-types \
              -target bpf -mcpu=v3 -D__TARGET_ARCH_$(ARCH) -I$(BUILD_DIR)

USER_CFLAGS := -g -O2 -Wall -I$(BUILD_DIR) \
               $(shell pkg-config --cflags libbpf 2>/dev/null)
USER_LDLIBS := $(shell pkg-config --libs libbpf 2>/dev/null || echo -lbpf) \
               -lelf -lz -lpthread

PID      ?=
INTERVAL ?= 0

.PHONY: all clean run check
.DELETE_ON_ERROR:

all: check $(TARGET)

check:
	@command -v $(BPFTOOL) >/dev/null || { echo "error: bpftool not found"; exit 1; }
	@command -v $(CLANG)   >/dev/null || { echo "error: clang not found";   exit 1; }
	@test -r /sys/kernel/btf/vmlinux || \
	  { echo "error: /sys/kernel/btf/vmlinux missing (need CONFIG_DEBUG_INFO_BTF=y)"; exit 1; }

$(BUILD_DIR):
	mkdir -p $@

$(VMLINUX_H): | $(BUILD_DIR)
	$(BPFTOOL) btf dump file /sys/kernel/btf/vmlinux format c > $@

$(BPF_OBJ): numa_migr.bpf.c $(VMLINUX_H) | $(BUILD_DIR)
	$(CLANG) $(BPF_CFLAGS) -c $< -o $@

$(BPF_LINKED): $(BPF_OBJ)
	$(BPFTOOL) gen object $@ $<

$(SKEL_H): $(BPF_LINKED)
	$(BPFTOOL) gen skeleton $< name numa_migr > $@

$(TARGET): numa_migr.c $(SKEL_H)
	$(CC) $(USER_CFLAGS) $< -o $@ $(USER_LDLIBS)

run: $(TARGET)
	@test -n "$(PID)" || { echo "usage: make run PID=<tgid> [INTERVAL=<ms>]"; exit 1; }
	sudo ./$(TARGET) -p $(PID) -i $(INTERVAL)

clean:
	rm -rf $(BUILD_DIR) $(TARGET)