#!/usr/bin/env python3
"""Exercise the real launcher with intercepted HTMM/policy syscalls."""
from pathlib import Path
import os
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = r'''
#include <assert.h>
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/syscall.h>
#include <linux/mempolicy.h>
long __wrap_syscall(long number, ...) {
    if (number == SYS_set_mempolicy) {
        va_list args;
        va_start(args, number);
        assert(va_arg(args, int) == MPOL_DEFAULT);
        assert(va_arg(args, void *) == NULL);
        assert(va_arg(args, unsigned long) == 0);
        va_end(args);
        puts("POLICY_DEFAULT_NO_NODES");
        fflush(stdout);
        if (getenv("TEST_POLICY_FAILURE")) { errno = EPERM; return -1; }
        return 0;
    }
    assert(number == 449 || number == 450);
    if (number == 449) {
        const char *marker = getenv("TEST_EXEC_MARKER");
        assert(marker);
        /* Hold attachment open until the child proves it has executed.
         * A synchronized launcher would time out here. */
        for (int i = 0; i < 2000 && access(marker, F_OK) != 0; i++)
            usleep(1000);
        assert(access(marker, F_OK) == 0);
        if (getenv("TEST_START_FAILURE")) { errno = ENOMEM; return -1; }
        puts("SAMPLING_ATTACHED");
        fflush(stdout);
    }
    return 0;
}
'''

with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp)
    (path / "syscalls.c").write_text(WRAPPER)
    for variant in ([], ["-D__NOPID"]):
        subprocess.run(["gcc", "-Wall", "-Wextra", "-Werror", *variant,
                    str(ROOT / "memtis-userspace/launch_bench.c"), str(path / "syscalls.c"),
                    "-Wl,--wrap=syscall", "-o", str(path / "launcher")], check=True)
        for mode in ("near", "slow", "artifact"):
            marker = path / "executed"
            marker.unlink(missing_ok=True)
            env = dict(os.environ, MEMTIS_INITIAL_PLACEMENT=mode,
                       TEST_EXEC_MARKER=str(marker))
            result = subprocess.run([str(path / "launcher"), "/bin/sh", "-c",
                                     'echo WORKLOAD_STARTED; touch "$TEST_EXEC_MARKER"; exit 7'],
                                    env=env, capture_output=True, text=True, timeout=5)
            assert result.returncode == 0, result  # Artifact does not propagate child status.
            assert "POLICY_DEFAULT_NO_NODES" not in result.stdout, result
            assert result.stdout.index("WORKLOAD_STARTED") < result.stdout.index("SAMPLING_ATTACHED"), result
        marker = path / "must-not-launch"
        env = dict(os.environ, MEMTIS_INITIAL_PLACEMENT="near", TEST_POLICY_FAILURE="1",
                   TEST_EXEC_MARKER=str(marker))
        result = subprocess.run([str(path / "launcher"), "/usr/bin/touch", str(marker)],
                                env=env, capture_output=True, text=True, timeout=5)
        assert result.returncode == 0 and marker.exists(), result
        assert "POLICY_DEFAULT_NO_NODES" not in result.stdout, result
        marker = path / "executed"
        marker.unlink(missing_ok=True)
        env = dict(os.environ, MEMTIS_INITIAL_PLACEMENT="near", TEST_START_FAILURE="1",
                   TEST_EXEC_MARKER=str(marker))
        result = subprocess.run([str(path / "launcher"), "/bin/sh", "-c",
                                 'touch "$TEST_EXEC_MARKER"; exit 9'],
                                env=env, capture_output=True, text=True, timeout=5)
        # Preserve the artifact's handling of the start return value. The
        # baseline kernel also reports success for initialization failures.
        assert result.returncode == 0 and marker.exists(), result
        assert "SAMPLING_ATTACHED" not in result.stdout, result
print("PASS: original concurrent launch, inherited memory policy, and original start/exit handling")
