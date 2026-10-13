# Copyright lowRISC contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

# Configuration arguments for conductor.py (which can be parsed by argparse)

import argparse
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import psutil


class Mode(StrEnum):
    PROVE = 'prove'
    EXPLORE = 'explore'
    BMC = 'bmc'
    INFO = 'info'

    @staticmethod
    def from_str(name: str) -> 'Mode':
        for e in Mode:
            if name == e.value:
                return e
        msg = f"The name '{name}' is not a known mode."
        raise ValueError(msg)

@dataclass
class AppConfig:
    mode: Mode
    build_dir: Path
    fresh: bool
    no_store: bool
    by_step: bool
    properties: list[str] | None
    missing: bool
    start: int
    bmc_step: int
    bmc_start: int
    hard: bool
    no_run: bool
    no_kill: bool
    check_complete: bool
    max_mem_gb: float

    @staticmethod
    def from_args() -> 'AppConfig':
        '''Parse args with argparse'''

        parser = argparse.ArgumentParser(
            prog="conductor.py",
            description="Constructs and executes proofs.",
            epilog=("Additionally, the PROOFLOG env var sets the prooflog "
                    "file (i.e. one proof result per line, JSON), and the "
                    "LOGFILE env var sets the logging file."))

        parser.add_argument("mode", choices=["prove", "explore", "bmc", "info"],
                            help=("Proof mode. "
                                  "prove: run existing proofs if they exist; "
                                  "explore: attempt to discover new proofs; "
                                  "bmc: do BMC on each property individually; "
                                  "info: dump stats about cached proofs."))

        parser.add_argument("--build-dir",
                            type=Path,
                            default=Path("build"),
                            help=("The directory in which to work "
                                  "(default: build)"))

        parser.add_argument("--fresh",
                            action="store_true",
                            help=("In explore mode, do not use already "
                                  "constructed proofs, always construct new "
                                  "proofs."))

        parser.add_argument("--no-store",
                            action="store_true",
                            help=("In explore mode, do not store constructed "
                                  "proof strategies."))

        parser.add_argument("--by-step",
                            action="store_true",
                            help="In prove mode, do proofs one step at a time")

        parser.add_argument("-p", "--properties",
                            nargs="*",
                            default=None,
                            help=("Restrict to only the properties with the "
                                  "given names, otherwise all properties. "
                                  "Especially helpful for BMC."))

        parser.add_argument("--missing",
                            action="store_true",
                            help=("Equivalent to -p <each property that is "
                                  "not skipped but has no proof in a step "
                                  "where there are some proofs>"))

        parser.add_argument("-s", "--start",
                            type=int, default=0,
                            help="First step to start at. (default: 0)")

        parser.add_argument("--bmc-step", type=int, default=1,
                            help="Step size for BMC. (default: 1)")

        parser.add_argument("--bmc-start", type=int, default=4,
                            help="Start length for BMC. (default: 4)")

        parser.add_argument("--hard",
                            action="store_true",
                            help=("In explore mode, try harder to prove "
                                  "properties (1hr timeout, more engines)."))

        parser.add_argument("--no-run", action="store_true",
                            help=("In explore mode don't run proofs "
                                  "again to check steps."))

        parser.add_argument("--no-kill", action="store_true",
                            help=("Don't kill proof processes when running "
                                  "out of memory."))

        parser.add_argument("--check-complete",
                            action="store_true",
                            help=("In prove mode, fail if there are unskipped "
                                  "properties without proofs."))

        parser.add_argument("--frac-mem", type=float, default=0.5,
                            help=("Fraction of the machine's RAM to allocate. "
                                  "Default: 0.5 (which is 50 percent)"))

        args = parser.parse_args()

        # Check that frac_mem is positive and not more than 1
        if args.frac_mem <= 0 or 1 < args.frac_mem:
            raise RuntimeError(f"Invalid --frac-mem value: {args.frac_mem}. "
                               "It must be in the range (0, 1].")
        # Convert frac_mem into a size is GiB.
        mem = psutil.virtual_memory()
        max_mem_gb = (mem.total / (1024 ** 3)) * args.frac_mem


        return AppConfig(Mode.from_str(args.mode),
                         args.build_dir,
                         args.fresh,
                         args.no_store,
                         args.by_step,
                         args.properties,
                         args.missing,
                         args.start,
                         args.bmc_step,
                         args.bmc_start,
                         args.hard,
                         args.no_run,
                         args.no_kill,
                         args.check_complete,
                         max_mem_gb)
