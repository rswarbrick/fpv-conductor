# Copyright lowRISC contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import json
import time

from dataclasses import dataclass
from pathlib import Path

from .log import (log, green, orange, red)
from .process import ProcessResult


PROOFLOG_PATH: Path | None = None

def set_prooflog_path(path: Path):
    global PROOFLOG_PATH
    PROOFLOG_PATH = path


@dataclass
class Config:
    '''An engine configuration'''
    raw: str
    """The description of the config to pass to the engine"""

    desc: str
    """Short description of config (used for log file names)"""

    def custom_version(self, raw_suff: str, desc_suff: str):
        return Config(f"{self.raw} {raw_suff}", f"{self.desc}.{desc_suff}")

    def to_tree(self) -> dict:
        return { "raw": self.raw, "desc": self.desc }

    @staticmethod
    def parse(tree: object) -> 'Config':
        if not isinstance(tree, dict):
            raise TypeError("Tree is not a dict.")

        raw_raw = tree.get("raw")
        if raw_raw is None:
            raise ValueError("No raw key in tree.")
        if not isinstance(raw_raw, str):
            raise ValueError("raw in tree is not a string.")

        raw_desc = tree.get("desc")
        if raw_desc is None:
            raise ValueError("No desc key in tree.")
        if not isinstance(raw_desc, str):
            raise ValueError("desc in tree is not a string.")

        return Config(raw_raw, raw_desc)


def proof_done(engine_config: Config,
               root_hash: str,
               rel_path: str,
               step: int,
               props: list[str],
               procres: ProcessResult):
    '''Log a nice message on proof completion, and store in the proof log'''

    if PROOFLOG_PATH is None:
        raise RuntimeError("PROOFLOG_PATH not set.")

    with open(PROOFLOG_PATH, "a") as f:
        json.dump([time.time(), [props, step, root_hash, engine_config.raw], vars(procres)], f)
        f.write("\n")

    match procres.code:
        case 20:
            log(f"UNSAT: {len(props)} properties in step {step} proven in {procres.dt:.3f}s with {procres.max_mem:.3f}GB", c=green)
        case 10:
            log(f"==========================================================================================================================", c=red)
            log(f"==========================================================================================================================", c=red)
            log(f"==========================================================================================================================", c=red)
            log(f"       ==================== SAT: CEX in step {step}, discovered in {procres.dt:.3f}s with {procres.max_mem:.3f}GB ==================", c=red)
            log(f"==========================================================================================================================", c=red)
            log(f"==========================================================================================================================", c=red)
            log(f"==========================================================================================================================", c=red)
            log(f"One of these properties in step {step} is not valid: {' '.join(props)}", c=red)
            log(f"To recover a witness run the following:")
            log(f"    {engine_config.raw} {rel_path} --witness | tail -n +2 > witness.aiw")
            log(f"Or the following:")
            log(f"    rIC3 -e bmc --no-preproc {rel_path} --witness | tail -n +2 > witness.aiw")
            log(f"Or, to compare against another engine:")
            log(f"    python3 smt2manip.py build/all.smt2 sel.smt2 {step} {' '.join(props)}")
            log(f"    yosys-smtbmc -s yices sel.smt2")
            log(f"Then to view the trace:")
            log(f"    build/aig-manip build/all.aig simulate build/all.ywmap build/all.vmap witness.aiw trace.vcd")
            log(f"    gtkwave trace.vcd")
        case 30:
            log(f"UNDETERMINED: Failed to find a CEX or proof for {len(props)} properties in step {step} ({procres.dt:.3f}s with {procres.max_mem:.3f}GB)", c=orange)
        case -9:
            log(f"KILLED by OS: Failed to prove {len(props)} properties in step {step} ({procres.dt:.3f}s with {procres.max_mem:.3f}GB)", c=red)
        case None:
            log(f"TIMEOUT: Failed to prove {len(props)} properties in step {step} ({procres.dt:.3f}s with {procres.max_mem:.3f}GB)", c=red)
        case n:
            log(f"Unknown Exit Code {n}: Failed to prove {len(props)} properties in step {step} ({procres.dt:.3f}s with {procres.max_mem:.3f}GB)", c=red)

