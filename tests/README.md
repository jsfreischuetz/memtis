MEMTIS crash regressions
=======================

Run from the MEMTIS repository root:

```sh
python3 tests/test_metadata.py
python3 tests/test_launcher.py
python3 ../tiering_solutions/tests/test_memtis_runner.py
```

The metadata test compiles the actual kernel helper bodies against mock page
objects with AddressSanitizer and UndefinedBehaviorSanitizer. It covers the
nonzero-PTE-index/NULL-metadata crash, untracked and file huge pages, invalid
hotness indexes, histogram accounting, and complete metadata copying. The mock
page layout preserves the union alias that previously let a `mapping` write
overwrite the next access-information entry during migration. LeakSanitizer
is disabled because it cannot inspect threads in the development sandbox.

The launcher test intercepts HTMM and NUMA-policy syscalls: near must issue
`set_mempolicy(MPOL_DEFAULT, NULL, 0)`, other modes must not reset policy, and a
reset failure must prevent the workload from starting. The runner tests use
fake workloads and fake sudo/mail commands; they do not alter host settings
or send mail.

Both launcher variants also keep the child stopped until sampling is attached,
then resume it before exec. Tests inspect the child's stopped state during the
mocked attachment syscall and verify attachment failure prevents execution.

These tests do not exercise kernel locking or prove stability under a live
MEMTIS workload. After booting the patched kernel, validate anonymous THP
splitting through partial `MADV_FREE`/`MADV_DONTNEED`, shmem THP reclaim, and
workload start/exit both outside and inside an HTMM-enabled cgroup. Capture
the kernel journal throughout and check for UBSAN, LRU accounting warnings,
oopses, and lockups before accepting benchmark results. The machine was on
the generic kernel during the source-level tests.

The near runner uses no `numactl --preferred` or `--membind` option. Its
launcher clears any inherited memory policy to the default. CPU affinity
still controls which node is local; default-policy fallback and MEMTIS
tiering decisions remain possible.
