# Copyright lowRISC contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# Original author: Louis-Emile Ploix
# SPDX-License-Identifier: Apache-2.0

# This program handles the running of proof processes. It can discover efficient proofs, schedule them, find
# counterexamples, and analyse the results.

import asyncio
import re
import time
import json
import os
import signal
import sys

from dataclasses import dataclass
import hashlib
from typing import Iterable
import typing
import psutil
from pathlib import Path

from .config import AppConfig, BuildDir, Mode
from .engine import Config, proof_done, set_prooflog_path
from .failed_cmd import FailedCmd
from .log import (set_logfile, flush_logfile,
                  log, gray, green, orange, red, white)
from .process import ProcessResult
from .shell import shell, start_process_runner

# Skipped properties are excluded from every step
SKIPPED_PROPS = set()

# --------------------- Utilities ----------------------

'''
Races allow async processes to cleanly run in parrallel, until one declares victory,
at which point all the other futures are cancelled. Tasks should be sequence of
awaitables. In order to declare victory of a race, simply return something not-None.
'''
async def race[A](tasks: Iterable[typing.Awaitable[A | None]]) -> A | None:
    class RaceWonException(Exception):
        def __init__(self, result):
            super().__init__()
            self.result = result
    winner = None
    try:
        async with asyncio.TaskGroup() as tg:
            async def wrapper(task):
                res = await task
                if res is not None:
                    raise RaceWonException(res)
            for task in tasks:
                tg.create_task(wrapper(task))

    except* RaceWonException as e:
        for result in e.exceptions:
            assert isinstance(result, RaceWonException)
            winner = result.result
    return winner


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

async def prove(config: Config,
                hier_pfx: str | None,
                root_hash: str,
                step: int,
                props: list[str],
                rel_log_path_root: str,
                bd: BuildDir,
                timeout=None,
                expected_memory=10.0) -> ProcessResult:
    '''Prove some properties with the given config

    Arguments:

      config:            The engine config to use.

      hier_pfx:          A SystemVerilog hierarchical path used as the prefix
                         for all properties.

      root_hash:         The hash of the root AIG file.

      step:              The index of the current step.

      props:             The properties that need proving.

      rel_log_path_root: The root of a relative path where log files should be
                         produced. If this is resolved inside bd to the path P
                         then the aiger extraction step will write a log to
                         P.prep.log and the proof step itself will write to
                         P.prove.<engine_config>.log.

      bd:                The build directory to use.

      timeout:           If nonzero, a timeout for the prove step in seconds.

      expected_memory:   The amount of memory (in GiB) expected by the proof step.

    '''
    log_path_root = bd.filepath(rel_log_path_root)
    prove_suffix = '.prove.' + config.desc + '.log'
    prep_log_path = log_path_root.with_name(log_path_root.name + '.prep.log')
    prove_log_path = log_path_root.with_name(log_path_root.name + prove_suffix)

    specialised_basename = await prepare_aiger(hier_pfx, step, props,
                                               prep_log_path, bd)
    specialised_path = bd.filepath(specialised_basename)

    res = await shell(f"{config.raw} {specialised_path}",
                      prove_log_path,
                      timeout=timeout,
                      exp_memory=expected_memory)
    proof_done(config, root_hash, specialised_basename, step, props, res)
    return res

cex_id = 0
async def bmc(root_hash: str,
              hier_pfx: str | None,
              step: int,
              props: list[str],
              rel_log_path_root: str,
              bd: BuildDir,
              timeout=None,
              start=None,
              end=None) -> ProcessResult:
    '''Run Bounded Model Checking on some properties

    Arguments:

      root_hash:         The hash of the root AIG file.

      hier_pfx:          A SystemVerilog hierarchical path used as the prefix
                         for all properties.

      step:              The index of the current step.

      props:             The properties that need proving.

      rel_log_path_root: The root of a relative path where log files should be
                         produced. This will be passed to prove() while that is
                         trying to create an SST counterexample. Assuming the
                         prove step does manage to produce a counterexample,
                         aig_manip will run to create a witness VCD trace that
                         shows the counterxample. If rel_log_path_root is
                         reloved inside bd to the path P then this second step
                         will write to P.sim.log.

      bd:                The build directory to use.

      timeout:           If not None, a timeout for the prove step in seconds.

      start:             Start argument given to rIC3 for search.

      end:               End argument given to rIC3 for search.

    '''

    global cex_id

    raw_cfg = ("rIC3 -e bmc --no-preproc --witness" +
               ("" if start is None else f" --start {start}") +
               ("" if end is None else f" --end {end}"))
    cfg = Config(raw_cfg, "bmc")

    res = await prove(cfg, hier_pfx, root_hash, step, props,
                      rel_log_path_root, bd, timeout=timeout)
    if res.code == 10:
        assert res.stdout is not None
        own_id = cex_id
        cex_id += 1
        witness = res.stdout.split("\n", maxsplit=1)[1]

        log_path_root = bd.filepath(rel_log_path_root)
        sim_log_path = log_path_root.with_name(log_path_root.name + '.sim.log')

        aiw_relpath = f"witness-{own_id}.aiw"
        vcd_relpath = f"trace-{own_id}.aiw"

        aiw_path = bd.filepath(aiw_relpath)
        aiw_path.write_text(witness)
        log(f"Written trace to {aiw_path}", c=white)
        await shell(bd.aig_manip_sim_cmd(aiw_relpath, vcd_relpath),
                    sim_log_path)
        log(f"Produced VCD file at trace-{own_id}.vcd", c=white)
    return res

# -------------------------- Proof Exploration -----------------------

async def explore(root_hash: str,
                  hier_pfx: str | None,
                  step: int,
                  props: list[str],
                  rel_log_path_root: str,
                  configs: list[tuple[Config, float]],
                  bd: BuildDir,
                  timeout=0.0,
                  debug_slow=None) -> None | tuple[Config, ProcessResult]:
    '''Try several configurations for some properties and see which is best, if any

    Arguments:

      root_hash:         The hash of the root AIG file.

      hier_pfx:          A SystemVerilog hierarchical path used as the prefix
                         for all properties.

      step:              The index of the current step.

      props:             The properties that need proving.

      rel_log_path_root: The root of a relative path where log files should be
                         produced. Suppose rel_log_path_root is reloved inside
                         bd to the path P. Then the prepare_aiger step will
                         write a log to P.explore.prep.log. The exploration
                         steps (one per config) will write a log to
                         P.explore.<config_name>.log.

      bd:                The build directory to use.

      timeout:           If not None, a timeout for the prove step in seconds.

      start:             Start argument given to rIC3 for search.

      end:               End argument given to rIC3 for search.

    '''

    log_path_root = bd.filepath(rel_log_path_root)
    prep_suffix = '.explore.prep.log'
    prep_log_path = log_path_root.with_name(log_path_root.name + prep_suffix)

    specialised_basename = await prepare_aiger(hier_pfx, step, props,
                                               prep_log_path, bd)
    specialised_path = bd.filepath(specialised_basename)

    async def explore_one(cfg: Config,
                          exp_memory: float) -> tuple[Config,
                                                      ProcessResult] | None:
        exp_suffix = '.explore.' + cfg.desc + '.log'
        exp_log_path = log_path_root.with_name(log_path_root.name + exp_suffix)

        result = await shell(f"{cfg.raw} {specialised_path}",
                             exp_log_path,
                             timeout=timeout,
                             exp_memory=exp_memory,
                             debug_slow=debug_slow)

        proof_done(cfg, root_hash, specialised_basename, step, props, result)
        if result.code in {10, 20}:
            return cfg, result

    return await race([explore_one(config, mem) for config, mem in configs])

@dataclass
class PropTree:
    '''A rose tree for property names (represented by strings).'''
    children : list['PropTree | str']

    @staticmethod
    def mk_flat_tree(items: list[str]):
        return PropTree(list(items))

    @staticmethod
    def mk_singleton(prop: str):
        return PropTree([prop])

    def breadth_first(self) -> list[str]:
        '''Return all the string nodes in the tree'''
        props = []
        for child in self.children:
            if isinstance(child, PropTree):
                props += child.breadth_first()
            else:
                props.append(child)
        return props

@dataclass
class Strategy:
    '''A complete proof step for some properties'''
    step: int
    """Step index in proof structure"""

    props: list[str]
    """Properties to prove"""

    config: Config
    """Engine configuration"""

    procres: ProcessResult
    """Result of the process that was run."""

async def build_strategy_rec(root_hash: str,
                             hier_pfx: str | None,
                             step: int,
                             prop_tree: PropTree,
                             failures: list[tuple[int, str]],
                             rel_log_path_root: str,
                             bd: BuildDir,
                             difficult=False) -> list[Strategy]:
    '''Build a strategy for the given tree.

    In each case in the tree, we either split into subtrees, or prove as a
    block. A strategy is a flat list of proof instructions.

    Arguments:

      root_hash:         The hash of the root AIG file.

      hier_pfx:          A SystemVerilog hierarchical path used as the prefix
                         for all properties.

      step:              The index of the current step.

      prop_tree:         The properties that need proving (as a rose tree)

      failures:          A list to which the function will write information
                         about failures. A failure is represented as a pair of
                         the form (n, desc), where n is a step index and desc
                         describes what didn't work.

      rel_log_path_root: The root of a relative path where log files should be
                         produced. This will be passed to child functions.

      bd:                The build directory to use.

      difficult:         A flag showing that this is expected to be hard, so
                         should get extra time.

    '''

    all_props = prop_tree.breadth_first()

    if len(all_props) == 0:
        return []

    # These may be helpful to change depending on the nature of the task
    ENGINES: list[tuple[Config, float]] = []

    if difficult:
        ENGINES = [
            # ("rIC3", 10),
            # ("rIC3 --no-preproc", 20),
            (Config("rIC3 -e kind --no-preproc", "npp"), 10.0),
            # ("rIC3 -e kind", 10)
        ]
    else:
        ENGINES = [
            (Config("rIC3", "std"), 10),
            # ("rIC3 --no-preproc", 20),
            (Config("rIC3 -e kind --no-preproc", "npp"), 10.0),
            # ("rIC3 -e kind", 10)
        ]

    if len(all_props) == 1:
        while True:
            res = await explore(root_hash,
                                hier_pfx,
                                step,
                                all_props,
                                rel_log_path_root,
                                configs=ENGINES,
                                bd=bd,
                                timeout=1800.0 if difficult else 600.0,
                                debug_slow=lambda dt: log(f"Still exploring step {step} property {all_props[0]} ({dt:.3f}s)", c=gray))
            if res is not None:
                if res[1].code == 20:
                    log(f"Constructed proof for 1 property in step {step}: {all_props[0]}", c=green)
                    return [Strategy(step, all_props, res[0], res[1])]
                else:
                    log(f"Failed to construct proof for 1 property in step {step}: {all_props[0]}", c=red)
                    return []
            log(f"Failed to find proof for property in step {step}: {all_props[0]} - ignoring", c=red)
            failures.append((step, all_props[0]))
            return []

    # If PropTree only has one child, we know that child is a genuine PropTree
    # (because otherwise len(all_props) would have been 0 or 1 and we would
    # already have returned). Recurse immediately.
    if len(prop_tree.children) == 1:
        the_child = prop_tree.children[0]
        assert isinstance(the_child, PropTree)
        return await build_strategy_rec(root_hash, hier_pfx, step,
                                        the_child, failures,
                                        rel_log_path_root, bd, difficult)

    async def without_split():
        res = await explore(root_hash, hier_pfx, step, all_props,
                            rel_log_path_root, configs=ENGINES, bd=bd, timeout=120.0)
        if res is not None:
            if res[1].code == 20:
                log(f"Constructed proof for {len(all_props)} properties in step {step}: {' '.join(all_props)}", c=green)
                return [Strategy(step, all_props, res[0], res[1])]
            else:
                log(f"Failed to construct proof for {len(all_props)} properties in step {step}: {' '.join(all_props)}", c=red)

    async def with_split():
        sub_trees: list[PropTree] = []
        rest: list[str] = []
        for prop in prop_tree.children:
            if isinstance(prop, str):
                rest.append(prop)
            else:
                sub_trees.append(prop)

        await asyncio.sleep(120.0) # Give the rest a head start

        if len(sub_trees) == 0:
            # If the tree only has string children, enqueue all of them in
            # parallel. Do this by calling build_strategy_rec again with "one
            # prop trees". Each instance will then immediately call explore().
            solutions = await asyncio.gather(
                *[build_strategy_rec(root_hash, hier_pfx, step,
                                     PropTree.mk_singleton(child),
                                     failures,
                                     rel_log_path_root,
                                     bd, difficult=difficult)
                  for child in rest]
            )
        else:
            # Create a list of PropTree objects that starts with the genuine
            # sub-trees and is followed by a list of single-item trees.
            strings_last = sub_trees + [PropTree.mk_singleton(r) for r in rest]
            solutions = await asyncio.gather(
                *[build_strategy_rec(root_hash, hier_pfx, step,
                                     tree,
                                     failures,
                                     rel_log_path_root,
                                     bd, difficult=difficult)
                  for tree in strings_last]
            )
        flattened = []
        for solution in solutions:
            flattened.extend(solution)
        return flattened

    winner = await race([without_split(), with_split()])
    assert winner is not None # someone has to win!
    return winner

async def run_strategy(root_hash: str,
                       hier_pfx: str | None,
                       strategies: list[Strategy],
                       rel_log_path_root: str,
                       bd: BuildDir) -> list[tuple[ProcessResult, Strategy]]:
    '''Execute a given strategy, return all the (result, strategy)

    Arguments:

      hier_pfx:          A SystemVerilog hierarchical path used as the prefix
                         for all properties.

      root_hash:         The hash of the root AIG file.

      strategies:        The list of strategies to run.

      rel_log_path_root: The root of a relative path where log files should be
                         produced. This will be passed to child functions.

      bd:                The build directory to use.

    '''
    proofs = []
    strategies.sort(key=lambda x: x.procres.dt, reverse=True)
    for step in strategies:
        conf = step.config
        if step.procres.stderr is not None:
            if " -e kind " in step.config.raw:
                s = step.procres.stderr.split("[INFO ] k-induction proofed in depth ")
                if len(s) == 2:
                    start_idx = int(s[1].strip())
                    conf = conf.custom_version(f" --start {start_idx}",
                                               f"start-{start_idx}")

        proofs.append(prove(conf, hier_pfx, root_hash, step.step, step.props,
                            rel_log_path_root, bd,
                            expected_memory=step.procres.max_mem * 1.1))
    return list(zip(await asyncio.gather(*proofs), strategies))

def chunk_name(name: str) -> list[str]:
    '''Split a name into chunks, delimited by _ and capitals

    '''
    # Using a capture group with re.split means that the contents of every match
    # will appear in the split list too. For example, re.split("(_)", "a_b")
    # gives ['a', '_', 'b']. If the regex matches at the first character, the
    # result is prefixed by a ''.
    #
    # Because the regex has multiple branches (with |) the possible matches get
    # appended. So re.split(r"_([A-Z])|_|([A-Z])", 'abc_def_Ghi') gives the list
    # ['abc', None, None, 'def', 'G', None, 'hi']. Here, the 'abc' is a "prefix"
    # before the first match. Then there is a list whose length is a multiple of
    # three: the two possible capture groups, and then the item afterwards. If
    # name ends with a match for the regex, the list will have a final '' value.
    all_split = re.split(r"_([A-Z])|_|([A-Z])", name)
    parts: list[str] = [all_split[0]] if all_split[0] else []
    for i in range(1, len(all_split), 3):
        parts.append((all_split[i] or "") +
                     (all_split[i + 1] or "") +
                     (all_split[i + 2] or ""))
    return parts

def split_by_prefixes(names: list[str]) -> PropTree:
    '''Construct a proof tree by recursively splitting on the prefix of a name

    For example a_x, a_y, b will produce [[a_x, a_y], b]
    '''
    def group(props: list[tuple[list[str], str]]) -> PropTree:
        buckets: dict[str, list[tuple[list[str], str]]] = {}
        done = PropTree([])
        for chunks, name in props:
            if len(chunks) == 1:
                done.children.append(PropTree.mk_singleton(name))
            elif chunks[0] not in buckets:
                buckets[chunks[0]] = [(chunks[1:], name)]
            else:
                buckets[chunks[0]].append((chunks[1:], name))
        for pre in buckets:
            bucket = group(buckets[pre])
            # while len(bucket) == 1:
            #     bucket = bucket[0]
            done.children.append(bucket)
        return done

    # chunk_name()

    chunked_names = [(chunk_name(name), name) for name in names]
    return group(chunked_names)

# ---------------------------------- Main ------------------------------


def decode_strategy(encoded: list) -> Strategy:
    return Strategy(encoded[0], encoded[1], encoded[2], ProcessResult(encoded[3][0], encoded[3][3], encoded[3][1], encoded[3][2], encoded[3][4] if len(encoded[3]) > 4 else None, encoded[3][5] if len(encoded[3]) > 5 else None))

def load_strategy(step: int, fresh: bool) -> list[Strategy] | None:
    if fresh:
        return None
    try:
        with open(f"strategies/step{step}.json", "r") as f:
            log(f"Loading strategy for step {step} from cache", c=white)
            encoded = json.load(f)
        return [decode_strategy(d) for d in encoded]
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as e:
        log(f"Error decoding step{step}.json (ignoring): {e}", c=red)
    return None

def store_strategy(step: int,
                   strategy: list[Strategy],
                   no_store: bool):
    encoded = [[s.step, s.props, s.config, [s.procres.code, s.procres.max_mem, s.procres.dt, s.procres.reason, s.procres.stdout, s.procres.stderr]] for s in strategy]
    log(encoded, c=gray)
    if no_store:
        return [decode_strategy(d) for d in encoded]
    try:
        os.makedirs("strategies", exist_ok=True)
        with open(f"strategies/step{step}.json", "w") as f:
            json.dump(encoded, f)
    except Exception as e:
        log(f"ERROR: Could not save strategy: {e}", c=red)
    return [decode_strategy(d) for d in encoded]

def missing_from(strategy: list[Strategy], props: list[str]) -> list[str]:
    done_props: list[str] = []
    for strategy_step in strategy:
        done_props.extend(strategy_step.props)
    return list(set(props).difference(done_props))

async def bmc_mode(root_hash: str,
                   hier_pfx: str | None,
                   props: list[tuple[int, str]],
                   bmc_start: int,
                   bmc_step: int,
                   bd: BuildDir):
    '''Run BMC on the given list of properties.

    Arguments:

      root_hash:         The hash of the root AIG file.

      hier_pfx:          A SystemVerilog hierarchical path used as the prefix
                         for all properties.

      props:             A list of (step, prop) pairs.

      start:             Start argument given to rIC3 for search.

      end:               End argument given to rIC3 for search.

      bd:                The build directory to use.
    '''
    if len(props) == 1:
        step, prop = props[0]
        log(f"Doing unbounded BMC on {prop} from step {step}", c=white)
        await bmc(root_hash, hier_pfx, step, [prop], "bmc.single", bd, start=bmc_start)
        return

    if len(props) == 0:
        log("No properties to do BMC on!", c=red)
        return
    i = bmc_start
    while True:
        for step, prop in props:
            log(f"Doing BMC at depth {i} on {prop} from step {step}", c=white)
            await bmc(root_hash, hier_pfx, step, [prop],
                      f"bmc.step{step}", bd, start=i, end=i)
        i += bmc_step

async def info_mode(by_step: list[list[str]],
                    by_step_skipped: list[list[str]],
                    fresh: bool):
    total_steps = 0
    for step, _ in enumerate(by_step):
        strategy = load_strategy(step, fresh)
        if strategy is None:
            log(f"No proof strategy entry for step {step}", c=orange)
            strategy = []
        total_steps += len(strategy)
        accounted_for: list[str] = []
        for stepi in strategy:
            log(f"Step {stepi.step} :: {stepi.procres.reason} :: {stepi.procres.max_mem:.3f}GB/{stepi.procres.dt:.3f}s :: {stepi.config} :: {' '.join(stepi.props)}")
            accounted_for.extend(stepi.props)
        if len(by_step_skipped[step]) > 0:
            log(f"Step {step} :: SKIPPED :: :: :: {' '.join(by_step_skipped[step])}", c=orange)
        unaccounted: list[str] = []
        for prop in by_step[step]:
            if prop not in accounted_for:
                unaccounted.append(prop)
        if len(unaccounted) > 0:
            log(f"Step {step} :: UNACCOUNTED :: :: :: {' '.join(unaccounted)}", c=red)
    log(f"{total_steps} proof steps in total")

async def prove_mode(root_hash: str,
                     hier_pfx: str | None,
                     all_props: list[tuple[int, str]],
                     by_step: list[list[str]],
                     fresh: bool,
                     start: int,
                     work_by_step: bool,
                     check_complete: bool,
                     bd: BuildDir):
    '''Top-level function for proving a set of properties

    Arguments:

      root_hash:         The hash of the root AIG file.

      hier_pfx:          A SystemVerilog hierarchical path used as the prefix
                         for all properties.

      all_props:         A list of (step, property) pairs.

      by_step:           The same set of properties, but grouped by step (and
                         cut off at max_step).

      fresh:             Start again (rather than re-using existing runs)

      start:             Start argument given to rIC3 for search.

      work_by_step:      Run the steps serially, completing a step before
                         starting properties in the next step.

      check_complete:    Check that all properties have been proven (across
                         all steps).

      bd:                The build directory to use.

    '''

    all_strategies: list[Strategy] = []
    num_skipped = 0
    for step, props in enumerate(by_step):
        if step < start:
            log(f"Skipping step {step}", c=orange)
            continue
        strategy = load_strategy(step, fresh)
        if strategy is None or len(strategy) == 0:
            log(f"No strategy for step {step}, skipping", c=orange)
            num_skipped += 1
            continue

        if work_by_step:
            log(f"Running strategy for step {step} ({len(props)} properties)", c=white)
            run_start = time.time()
            await run_strategy(root_hash, hier_pfx, strategy,
                               f"prove.step{step}", bd)
            run_dt = time.time() - run_start
            log(f"Ran strategy for step {step} in {run_dt:.3f}s", c=white)
        all_strategies.extend(strategy)

    # At this point, there should be at least one strategy in all_strategies.
    if not all_strategies:
        msg = "No strategies needed proving."
        if num_skipped:
            msg += (f" But number skipped was {num_skipped}. "
                    "Missing call to explore?")
        raise RuntimeError(msg)

    if check_complete:
        covered: set[str] = set()
        for step in all_strategies:
            covered.update(step.props)
        uncovered = set(prop[1] for prop in all_props).difference(covered)
        if len(uncovered) > 0:
            log(f"Missing proof steps for {len(uncovered)} properties: {' '.join(uncovered)}", c=red)
            exit(1)
        else:
            log(f"All {len(all_props)} properties are covered.", c=green)

    if not work_by_step:
        log(f"Running strategy for everything", c=white)
        run_start = time.time()
        results = await run_strategy(root_hash, hier_pfx, all_strategies,
                                     "prove.all", bd)
        run_dt = time.time() - run_start
        log(f"Ran strategy in {run_dt:.3f}s", c=white)

        unsats = 0
        has_errors = False
        for res, step in results:
            if res.code == 20:
                unsats += 1
            else:
                has_errors = True
                log(f"Failed to prove step {step.step} proof step with code {res.code} (see above, or logfile.txt, for more details): {' '.join(step.props)}", c=red)
        log(f"{unsats}/{len(all_strategies)} proof steps were UNSAT", c=white)
        if has_errors:
            exit(1) # Failed

async def construct_strategy(root_hash: str,
                             hier_pfx: str | None,
                             step: int,
                             props: list[str],
                             fresh: bool,
                             no_store: bool,
                             hard: bool,
                             rel_log_path_root: str,
                             bd: BuildDir) -> tuple[bool, list[Strategy], list[str]]:
    strategy = load_strategy(step, fresh) or []
    not_done = missing_from(strategy, props)
    if len(not_done) == 0:
        return False, strategy, []

    log(f"Building strategy for step {step} ({len(not_done)}/{len(props)} properties)", c=white)
    build_start = time.time()

    failures: list[tuple[int, str]] = []
    if hard:
        log("strategy: each property", c=white)
        strategies = \
            await asyncio.gather(*[build_strategy_rec(root_hash, hier_pfx,
                                                      step,
                                                      PropTree.mk_singleton(prop),
                                                      failures,
                                                      rel_log_path_root,
                                                      bd, difficult=True)
                                   for prop in not_done])

        for x in strategies:
            strategy.extend(x)
    else:
        log("strategy: name based recursive splitting, linear fallback", c=white)
        blocks = split_by_prefixes(not_done)
        strategy += await build_strategy_rec(root_hash, hier_pfx,
                                             step, blocks, failures,
                                             rel_log_path_root, bd, difficult=hard)

    build_dt = time.time() - build_start
    log(f"Constructed strategy for step {step} of {len(strategy)} proof steps in {build_dt:.3f}s", c=white)
    strategy = store_strategy(step, strategy, no_store)
    return True, strategy, [f[1] for f in failures]

async def explore_mode(root_hash: str,
                       hier_pfx: str | None,
                       by_step: list[list[str]],
                       fresh: bool,
                       no_store: bool,
                       start: int,
                       hard: bool,
                       no_run: bool,
                       bd: BuildDir):
    all_failures: list[str] = []
    for step, props in enumerate(by_step):
        if step < start:
            log(f"Skipping step {step}", c=orange)
            continue

        new, strategy, failures = \
            await construct_strategy(root_hash, hier_pfx, step, props,
                                     fresh, no_store, hard,
                                     "explore.strategy", bd)
        all_failures.extend(failures)
        if new:
            log(f"All failures to date: {all_failures}", c=gray)

        if no_run:
            pass
        elif not new or len(strategy) != 1:
            log(f"Running strategy for step {step} ({len(props)} properties)", c=white)
            run_start = time.time()
            await run_strategy(root_hash, hier_pfx, strategy, "explore.run", bd)
            run_dt = time.time() - run_start
            log(f"Ran strategy for step {step} in {run_dt:.3f}s", c=white)
        else:
            log(f"Skipping proof run for step {step}, since it has just one step", c=white)


def split_step_name(name: str) -> tuple[int, str]:
    '''Split a step name out of an assertion name, yielding (idx, 'name').

    The assertion should be of the form "Step123_foo", which will yield the
    pair (123, 'foo'). However, this expects to be called with a Yosys internal
    name, which will actually look like "$\tb.Step123_foo$19129".

    '''
    # Extract the hierarchical path, by stripping away any "$\" prefix and
    # "$1234" suffix. First, the prefix:
    if name.startswith('$\\'):
        name1 = name[2:]
    else:
        name1 = name

    # Now any $1234 suffix. Note that we assume there is at least one character
    # before the suffix (so we won't strip away the whole of something like "$\$123".
    m = re.match(r'(.+)\$[0-9]+$', name1)
    if m:
        name2 = m.group(1)
    else:
        name2 = name1

    if not name2:
        raise ValueError(f"Failed to split step name from {name}: after dropping "
                         "header and footer, there was nothing left.")

    # At this point, we'll have something like "tb.Step123_foo". Tokenise the
    # hierarchical name and just take out the bottom part.
    parts = name2.split('.')

    # The result of the split call will always be nonempty, so will have a last
    # string.
    basename = parts[-1]

    m = re.match('Step([0-9]+)_(.*)', basename)
    if not m:
        msg = (f"Failed to extract step from assertion name {name}, which demangled "
               f"to {basename}. We expect the form 'Step123_foo'.")
        raise ValueError(msg)
    return int(m.group(1)), m.group(2)


async def async_main(cfg: AppConfig,
                     root_hash: str):
    start_process_runner(cfg)

    def group_by_step(names: list[tuple[int, str]], max=None) -> list[list[str]]:
        by_step = []
        for step, name in names:
            while step >= len(by_step):
                by_step.append([])
            by_step[step].append(name)
        if max is not None:
            while max >= len(by_step):
                by_step.append([])
        return by_step

    log("Reading property list", c=white)

    ywmap_path = cfg.build_dir.filepath(cfg.build_dir.ywmap)

    with open(ywmap_path) as f:
        asserts = json.load(f).get("asserts")
    if not asserts:
        raise ValueError(f"The ywmap at {ywmap_path} has no asserts.")

    preproc_names = [split_step_name(x[0]) for x in asserts]

    max_step = max(step for step, _ in preproc_names)
    names = [(step, name) for step, name in preproc_names
             if not name.endswith("_Cover")]

    by_step = group_by_step(names)
    props: list[tuple[int, str]] = []
    for prop in (cfg.properties or []):
        for step, name in names:
            if prop == name:
                props.append((step, prop))
                break
        else:
            log(f"ERROR: Property not found {prop}", c=red)
            exit(1)
    if cfg.missing:
        for step, sprops in enumerate(by_step):
            strategy = load_strategy(step, cfg.fresh)
            if strategy is None:
                continue
            props.extend([(step, p) for p in missing_from(strategy, sprops)])
    elif len(props) == 0:
        props = names
    props_skipped = [(step, p) for step, p in props if p in SKIPPED_PROPS]
    props = [(step, p) for step, p in props if p not in SKIPPED_PROPS]

    if len(props) == 0 and len(props_skipped) == 0:
        log("Warning: Empty property selection", c=orange)

    skipped_by_step = group_by_step(props_skipped, max_step)
    by_step = group_by_step(props, max_step)

    match cfg.mode:
        case Mode.BMC:
            await bmc_mode(root_hash, cfg.hier_pfx, props,
                           cfg.bmc_start, cfg.bmc_step, cfg.build_dir)
        case Mode.INFO:
            await info_mode(by_step, skipped_by_step, cfg.fresh)
        case Mode.PROVE:
            await prove_mode(root_hash, cfg.hier_pfx, props, by_step,
                             cfg.fresh, cfg.start,
                             cfg.by_step, cfg.check_complete, cfg.build_dir)
        case Mode.EXPLORE:
            await explore_mode(root_hash, cfg.hier_pfx, by_step,
                               cfg.fresh, cfg.no_store,
                               cfg.start, cfg.hard, cfg.no_run, cfg.build_dir)


def main() -> int:
    cfg = AppConfig.from_args()

    if len(SKIPPED_PROPS) > 0:
        log(f"WARNING: Skipped properties are {' '.join(SKIPPED_PROPS)}",
            c=orange)

    set_logfile(cfg.build_dir.open('logfile.txt', 'a'))
    set_prooflog_path(cfg.build_dir.filepath('prooflog.txt'))

    aig_path = cfg.build_dir.filepath(cfg.build_dir.aig)
    ROOT_HASH = hashlib.new("sha256", aig_path.read_bytes()).hexdigest()
    log(f"{cfg.build_dir.filepath(cfg.build_dir.aig)} has sha256 {ROOT_HASH}")

    try:
        asyncio.run(async_main(cfg, ROOT_HASH))
    except FailedCmd as fcmd:
        print(f"Failed command (retcode {fcmd.retcode}):\n"
              f"  {fcmd.cmd}\n",
              file=sys.stderr)
        if fcmd.log_path is not None:
            print(f"Log file ({fcmd.log_path}) ends with the following lines:")
            with open(fcmd.log_path) as log_file:
                lines = log_file.readlines()
                print("".join(lines[-10:]), end="", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # We've taken a SIGINT. Remove Python's filtering and kill ourselves
        # again with the same signal (which will look "right" for any program
        # running us)
        print('(KeyboardInterrupt)', file=sys.stderr)
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        flush_logfile()
        os.kill(os.getpid(), signal.SIGINT)
        # This shouldn't be reachable: the SIGINT should have killed us.
        assert False

    return 0
