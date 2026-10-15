# Copyright lowRISC contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import asyncio
import subprocess
import psutil
import time
import typing

from math import floor
from pathlib import Path

from .log import log, gray


class ProcessResult:
    '''
    Information regarding the completion of a process, including memory/time and stdio.
    '''

    def __init__(self, code, reason, max_mem, dt, stdout, stderr):
        self.code: int | None = code
        '''
        OK = the process finished
        TIMEOUT = the process was killed by us due to a timeout
        UNINTERESTED = the process was killed by us, probably because another process in the race won
        '''
        self.reason: str = reason
        self.max_mem: float = max_mem
        self.dt: float = dt
        self.stdout: str | None = stdout
        self.stderr: str | None = stderr

    @staticmethod
    def from_proc(proc: subprocess.Popen,
                  code: int | None,
                  reason: str,
                  max_mem: float,
                  dt: float):
        stdout_contents: str | None = None
        stderr_contents: str | None = None
        if proc.stdout is not None:
            stdout_contents = proc.stdout.read().decode()
        if proc.stderr is not None:
            stderr_contents = proc.stderr.read().decode()
        return ProcessResult(code, reason, max_mem, dt,
                             stdout_contents,
                             stderr_contents)

    def to_tree(self) -> dict:
        ret: dict = {"code": self.code,
                     "max_mem": self.max_mem,
                     "dt": self.dt,
                     "reason": self.reason}
        if self.stdout is not None:
            ret["stdout"] = self.stdout
        if self.stderr is not None:
            ret["stderr"] = self.stderr

        return ret

    @staticmethod
    def parse(tree: object) -> 'ProcessResult':
        if not isinstance(tree, dict):
            raise TypeError("Tree is not a dict.")

        raw_code = tree.get('code')
        if raw_code is None:
            raise ValueError("No code key in dict.")
        if not isinstance(raw_code, int):
            raise ValueError("code is not an int.")

        raw_reason = tree.get('reason')
        if raw_reason is None:
            raise ValueError("No reason key in dict.")
        if not isinstance(raw_reason, str):
            raise ValueError("reason is not a string.")

        raw_max_mem = tree.get('max_mem')
        if raw_max_mem is None:
            raise ValueError("No max_mem key in dict.")
        if not (isinstance(raw_max_mem, float) or
                isinstance(raw_max_mem, int)):
            raise ValueError("max_mem is not a float.")

        raw_dt = tree.get('dt')
        if raw_dt is None:
            raise ValueError("No dt key in dict.")
        if not (isinstance(raw_dt, float) or
                isinstance(raw_dt, int)):
            raise ValueError("dt is not a float.")

        raw_stdout = tree.get('stdout')
        if raw_stdout is not None and not isinstance(raw_stdout, str):
            raise ValueError("stdout is given and not a string.")

        raw_stderr = tree.get('stderr')
        if raw_stderr is not None and not isinstance(raw_stderr, str):
            raise ValueError("stderr is given and not a string.")

        return ProcessResult(raw_code, raw_reason,
                             raw_max_mem, raw_dt,
                             raw_stdout, raw_stderr)


class ProcessFuture(asyncio.Future):
    '''
    Finishes when a process exits, and cancelling kills that process.
    Produces a ProcessResult.
    '''

    def __init__(self, loop, proc):
        super().__init__(loop=loop)
        self.proc = proc

    def cancel(self, msg = None):
        self.proc.kill()
        return super().cancel(msg=msg)

class Process:
    '''
    A process, which me killed, restarted and awaited (via the .future property).
    Handles the tracking of time and timeouts, and of memory consumption.
    debug_slow allows for periodic logging.
    '''

    def __init__(self,
                 cmd,
                 expected_memory = 0.0,
                 expected_time = 0.0,
                 timeout = None,
                 debug_slow = None,
                 log_path: Path | None = None):
        self.cmd: str = cmd
        self.expected_memory: float = expected_memory
        self.expected_time: float = expected_time
        self.timeout: float | None = timeout
        self.kill_: str | None = None # Either kill, restart or None

        self.debug_slow = debug_slow # A message to write when the process is taking a long time
        self.debug_slow_count = 0

        self.log_path : Path | None = log_path
        self.proc = None # Will be set on start()
        self.psproc = None

        self.future: asyncio.Future[ProcessResult] = ProcessFuture(asyncio.get_running_loop(), self)

        self.max_memory = 0
        self.start_time = 0

    def start(self, log_file: typing.IO | None):
        log("$", self.cmd)
        self.proc = subprocess.Popen(self.cmd, shell=True,
                                     stdin=subprocess.DEVNULL,
                                     stdout=log_file,
                                     stderr=log_file)
        self.psproc = psutil.Process(pid=self.proc.pid)
        self.start_time = time.time()
        self.max_memory = 0
        self.debug_slow_count = 0

    # Give up on the process, may be called by anyone
    def kill(self):
        self.kill_ = "kill"

    # Kill the process with the expectation that it will be restarted again.
    # Only to be called by ProcessRunner
    def kill_restart(self):
        self.kill_ = "restart"

    def poll(self):
        assert self.proc is not None
        assert self.psproc is not None

        gb_in_bytes = 1024 ** 3
        self.max_memory = max(self.psproc.memory_info().rss / gb_in_bytes,
                              self.max_memory)

        dt = time.time() - self.start_time
        code = self.proc.poll()
        if code is not None and self.kill_ == None:
            log(f"Finished `{self.cmd}` ({dt:.3f}s) ({self.max_memory:.3f}GB)")
            if not self.future.cancelled():
                self.future.set_result(ProcessResult.from_proc(self.proc, code, "OK", self.max_memory, dt))
                # self.future.set_result((code, self.max_memory, dt, "OK", self.proc.stdout.read().decode(), self.proc.stderr.read().decode()))
            return True
        elif self.kill_ == "restart":
            log(f"Kill (will restart) `{self.cmd}` ({dt:.3f}s) ({self.max_memory:.3f}GB)", c=gray)
            self.proc.kill()
            self.kill_ = None
            self.expected_memory = max(self.expected_memory, self.max_memory)
            self.expected_time = max(self.expected_time, dt)
            return True
        elif self.timeout is not None and dt > self.timeout:
            log(f"Kill `{self.cmd}` ({dt:.3f}s) ({self.max_memory:.3f}GB)")
            self.proc.kill()
            if not self.future.cancelled():
                self.future.set_result(ProcessResult.from_proc(self.proc, None, "TIMEOUT", self.max_memory, dt))
                # self.future.set_result((None, self.max_memory, dt, "TIMEOUT", None, self.proc.stderr.read().decode()))
            return True
        elif self.kill_ == "kill":
            log(f"Kill (uninterested) `{self.cmd}` ({dt:.3f}s) ({self.max_memory:.3f}GB)", c=gray)
            self.proc.kill()
            if not self.future.cancelled():
                self.future.set_result(ProcessResult.from_proc(self.proc, None, "UNINTERESTED", self.max_memory, dt))
                # self.future.set_result((None, self.max_memory, dt, "UNINTERESTED", None, self.proc.stderr.read().decode()))
            return True

        # Every 60 seconds call the debug_slow handler
        n = floor(dt / 60.0)
        if n > self.debug_slow_count:
            if self.debug_slow is not None:
                self.debug_slow(dt)
            self.debug_slow_count = n

        return False
