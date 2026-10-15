# Copyright lowRISC contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import psutil
import time
import typing

from pathlib import Path
from math import floor

from .config import AppConfig, BuildDir
from .failed_cmd import FailedCmd
from .log import log, gray
from .process import Process


def global_memory_free():
    '''Returns the global free memory according to psutil'''
    return psutil.virtual_memory().available / (1024 ** 3)


class ProcessRunner:
    '''Schedules (and reschedules) Processes according to the memory and CPU
    constraints of the system.'''

    GLOBAL_MEMORY_BUFFER = 5 # Never allocate more than (total ram - buffer)GB memory
    POLL_DELAY = 0.1 # Time between poll cycles
    MAX_RUNNING = (psutil.cpu_count() or 8) - 2 # Number of CPUs, with a couple spares

    def __init__(self, cfg: AppConfig):
        self._cfg = cfg

        self.pending: list[tuple[Process, Path | None]] = []
        self.running: list[tuple[Process, tuple[Path, typing.IO] | None]] = []
        self.first_start = time.time()
        self.debug_count = 0

        # Check that max_mem_gb is more than twice as big as
        # GLOBAL_MEMORY_BUFFER. The latter is meant to be "some extra buffering
        # space", so it definitely shouldn't be as much as half of the total
        # space available.
        if (cfg.max_mem_gb <= 2 * ProcessRunner.GLOBAL_MEMORY_BUFFER):
            msg = (f"The allowed RAM is {cfg.max_mem_gb} GiB, which is not "
                   "more than twice the size of the buffer memory "
                   f"({ProcessRunner.GLOBAL_MEMORY_BUFFER} GiB)")
            raise RuntimeError(msg)

        log(f"Process runner will have a maximum of "
            "{ProcessRunner.MAX_RUNNING} processes, "
            f"and currently sees {self.mem_avail():.3f}GB free.")

    def enqueue(self, proc: Process, out_file: Path | None):
        '''Enqueue a process that should be run'''
        self.pending.insert(0, (proc, out_file))

    def start_loop(self):
        asyncio.get_running_loop().call_soon(lambda: self.poll())

    def children_used_mem(self):
        return sum(proc.max_memory for proc, _ in self.running)

    def mem_avail(self):
        if self._cfg.max_mem_gb == 0:
            return global_memory_free()
        return min(global_memory_free(),
                   max(self._cfg.max_mem_gb - self.children_used_mem(), 0))

    def poll(self):
        # Kill recently started processes until memory is OK, unless there is just one, then there's no point(?)
        free = self.mem_avail()
        while not self._cfg.no_kill and free < ProcessRunner.GLOBAL_MEMORY_BUFFER and len(self.running) > 1:
            last, last_path_file = self.running.pop()
            last.kill_restart()

            last_path: Path | None = None
            if last_path_file is not None:
                last_path = last_path_file[0]
                last_path_file[1].close()

            free += max(last.max_memory, 3)
            last.poll()
            self.pending.append((last, last_path))

        while self.pending and (len(self.running) < ProcessRunner.MAX_RUNNING):
            next_job, next_log_path = self.pending[-1]
            if (next_job.expected_memory > free - ProcessRunner.GLOBAL_MEMORY_BUFFER):
                # There's not space for the next job
                break

            if self.first_start == 0:
                self.first_start = time.time()

            # Pop the (next_job, next_log_path) pair from self.pending: we're
            # going to start running the job.
            self.pending.pop()

            if next_log_path is not None:
                next_log_file = open(next_log_path, 'w')
                next_log_pair = (next_log_path, next_log_file)
            else:
                next_log_file = None
                next_log_pair = None

            next_job.start(next_log_file)
            free -= max(next_job.expected_memory, 3) # otherwise lots of zeros won't help
            self.running.append((next_job, next_log_pair))

        # Drop processes that finished
        p = 0
        while p < len(self.running):
            if self.running[p][0].poll():
                self.running.pop(p)
            else:
                p += 1

        n = floor((time.time() - self.first_start) / 30.0)
        if n > self.debug_count:
            log((f"Running {len(self.running)} processes, with {len(self.pending)} pending. "
                 f"Memory used right now: {self.children_used_mem():.3f}GB"),
                c=gray)
            self.debug_count = n

        asyncio.get_running_loop().call_later(ProcessRunner.POLL_DELAY, lambda: self.poll())

# ------------------------ Proof Primitives ------------------

aiger_idx = 0
async def prepare_aiger(hier_pfx: str | None,
                        step: int,
                        props: list[str],
                        log_path: Path,
                        bd: BuildDir) -> str:
    '''Make an aiger file for the properties at the given step.

    Returns the (base) name of that file.

    Arguments:

      hier_pfx: A SystemVerilog hierarchical path used as the prefix for all
                properties.

      step:     The index of the step that is being run.

      props:    A list of the properties that will be run in this step.

      bd:       The directory in which the build is running.

    '''
    global aiger_idx

    name = f"aiger-{aiger_idx}.aig"
    aiger_idx += 1
    cmd = bd.aig_manip_select_cmd(hier_pfx, bd.filepath(name), step, props)
    retcode = (await shell(cmd, log_path, exp_memory=0.5)).code
    if retcode != 0:
        raise FailedCmd(cmd, retcode, log_path)

    return name

PROCESS_RUNNER: ProcessRunner | None = None

def start_process_runner(cfg: AppConfig):
    global PROCESS_RUNNER

    if PROCESS_RUNNER is not None:
        raise RuntimeError("Have already started process_runner.")
    PROCESS_RUNNER = ProcessRunner(cfg)
    PROCESS_RUNNER.start_loop()


async def shell(cmd: str,
                log_path: Path | None,
                exp_memory: float = 0.0,
                timeout = None,
                debug_slow = None):
    '''Run a shell command in the global process runner

    Arguments:

      cmd:        The command to be run (as a string)

      log_path:   The path where the output of the command will be written, or
                  None if the output should be discarded.

      exp_memory: The amount of memory (in GiB) that the command is
                  expected to use.

      timeout:    If not None, a time-out in seconds.

      debug_slow: If not None, a "still alive" message that should be written
                  periodically to show that the process is still running.

    '''
    assert PROCESS_RUNNER is not None
    proc = Process(cmd, expected_memory=exp_memory,
                   timeout=timeout, debug_slow=debug_slow,
                   log_path=log_path)
    PROCESS_RUNNER.enqueue(proc, log_path)
    return await proc.future
