# Copyright lowRISC contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

from datetime import datetime
from pathlib import Path
import typing

LOGFILE: typing.IO | None = None

def green(s): return f"\033[32;1m{s}\033[0m"
def red(s): return f"\033[31;1m{s}\033[0m"
def white(s): return f"\033[1m{s}\033[0m"
def orange(s): return f"\033[33;1m{s}\033[0m"
def gray(s): return f"\033[90m{s}\033[0m"

def close_logfile():
    global LOGFILE

    if LOGFILE is not None:
        LOGFILE.close()
        LOGFILE = None

def set_logfile(logfile: typing.IO):
    global LOGFILE

    close_logfile()
    LOGFILE = logfile
    # Blank line to mark new run
    LOGFILE.write('\n')

def flush_logfile():
    if LOGFILE is not None:
        LOGFILE.flush()

def log(*args, c=lambda x: x):
    '''Send all printed text to a log file, and prepend the date and time.'''
    timestr = datetime.now().strftime('[%d/%m/%y %H:%M:%S.%f]')
    s = " ".join(map(str, args))
    print(gray(timestr), c(s))

    if LOGFILE is not None:
        LOGFILE.write(f"{timestr} {s}")
