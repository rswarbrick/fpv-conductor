# Copyright lowRISC contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import os
from datetime import datetime


LOGFILE = os.environ.get("LOGFILE", "logfile.txt")

def green(s): return f"\033[32;1m{s}\033[0m"
def red(s): return f"\033[31;1m{s}\033[0m"
def white(s): return f"\033[1m{s}\033[0m"
def orange(s): return f"\033[33;1m{s}\033[0m"
def gray(s): return f"\033[90m{s}\033[0m"

def write_logfile(s):
    if not "NO_LOG" in os.environ:
        with open(LOGFILE, "a") as f:
            f.write(s + "\n")

'''Send all printed text to a log file, and prepend the date and time.'''
def log(*args, c=lambda x: x):
    timestr = datetime.now().strftime('[%d/%m/%y %H:%M:%S.%f]')
    s = " ".join(map(str, args))
    print(gray(timestr), c(s))
    write_logfile(f"{timestr} {s}")

write_logfile("") # Blank line to mark new run
