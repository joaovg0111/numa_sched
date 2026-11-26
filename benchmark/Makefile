CC      ?= gcc
CFLAGS  := -O3 -Wall -Wextra -march=native
TARGETS := membench stream stream_omp

.PHONY: all clean

all: $(TARGETS)

membench: membench.c
	$(CC) $(CFLAGS) $< -o $@

# 40M elements * 8B * 3 arrays ≈ 915 MiB (stays under 2GiB static BSS limit)
stream: stream.c
	$(CC) $(CFLAGS) -DSTREAM_ARRAY_SIZE=40000000 -DNTIMES=80 $< -o $@ -lm

stream_omp: stream.c
	$(CC) $(CFLAGS) -fopenmp -DSTREAM_ARRAY_SIZE=40000000 -DNTIMES=80 $< -o $@ -lm

clean:
	rm -f $(TARGETS)