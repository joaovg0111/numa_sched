SET extension_directory = './benchmark/ext/duckdb/extensions';
LOAD tpch;
CALL dbgen(sf = 3);
.timer on
PRAGMA tpch(1);
PRAGMA tpch(3);
PRAGMA tpch(5);
PRAGMA tpch(9);
PRAGMA tpch(18);
PRAGMA tpch(1);
PRAGMA tpch(3);
PRAGMA tpch(5);
PRAGMA tpch(9);
PRAGMA tpch(18);
