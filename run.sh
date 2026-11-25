#!/bin/bash
cd "$(dirname "$0")"

USE_MIGR=false
if [[ "$1" == "-m" || "$1" == "--migr" ]]; then
    USE_MIGR=true
    sudo -v
fi

numactl --cpunodebind=0 --membind=0 ./benchmark/membench -m chase -s 1024 &
PID=$!

if $USE_MIGR; then
    sudo ./migr/numa_migr -p "$PID" -i 100 -n 2
fi

wait "$PID"
