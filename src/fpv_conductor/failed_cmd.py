# Copyright lowRISC contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

from dataclasses import dataclass
from pathlib import Path


@dataclass
class FailedCmd(Exception):
    cmd: str
    '''The command that failed'''

    retcode: int | None
    '''Return code from the command.'''

    log_path: Path | None
    '''A path to the command's log file'''

    
