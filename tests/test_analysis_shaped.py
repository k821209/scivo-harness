"""The run-record nudge is spent on analyses, not on anything that spells one.

It is given ONCE, at the second analysis-shaped command of a session. The
test for "analysis-shaped" was a pattern over the whole text, so a `sed` on a
path through `lib/python3.11/` and a `pytest` run used it up — it fired on the
reviewer, twice, while reviewing it (2026-11-10/11).
"""
from __future__ import annotations

import asyncio

import pytest

from scivo_harness.guardrails import Guardrails, analysis_shaped


@pytest.mark.parametrize("command", [
    "python train.py --lr 3e-4",
    "python3 scripts/fig2.py",
    ".venv/bin/python analysis/run.py",
    "/usr/bin/python3.11 fit.py",
    "Rscript deseq.R",
    "samtools view -c x.bam",
    "cd runs && snakemake -j 8",
    "zcat x.vcf.gz | bcftools view -H | wc -l",
    "FOO=1 plink --bfile x --assoc",
    "python -c 'import pandas as pd; print(pd.read_csv(\"x\").mean())'",
    "ssh b200 'cd /data && python train.py'",
    "ssh -p 2244 node2 nextflow run main.nf",
])
def test_a_command_that_runs_an_analysis_counts(command):
    assert analysis_shaped(command)


@pytest.mark.parametrize("command", [
    "sed -n 205,262p .venv/lib/python3.11/site-packages/x/types.py",   # the one that fired
    ".venv/bin/python -m pytest -q",
    "python -m pip install -e .",
    "python --version",
    "ls ~/miniconda3/envs/vh/bin/python",
    "grep -rn plink docs/",
    "cat notes/samtools.md",
    "git log --oneline -- scripts/python_helpers.sh",
    "which python3",
    "ssh b200 'tail -5 train.log'",
    "ssh b200 'ls /opt/gatk'",
    "echo 'run python train.py tomorrow'",
])
def test_a_command_that_only_mentions_one_does_not(command):
    assert not analysis_shaped(command)


def test_a_line_that_cannot_be_read_falls_back_to_the_pattern():
    """Over-counting there costs a reminder; under-counting costs a result
    with no provenance."""
    assert analysis_shaped("python fit.py > out.txt")            # writes a file: unreadable
    assert analysis_shaped("for f in *.bam; do samtools index $(basename $f); done")
    assert not analysis_shaped("ls > listing.txt")


def test_the_nudge_survives_the_noise_and_lands_on_the_second_real_run():
    rails = Guardrails()

    def bash(command):
        out = asyncio.run(rails.on_bash({"tool_input": {"command": command}}, None, None))
        return (out.get("hookSpecificOutput") or {}).get("additionalContext", "")

    assert bash("sed -n 1,9p .venv/lib/python3.11/site-packages/x.py") == ""
    assert bash(".venv/bin/python -m pytest -q") == ""
    assert bash("python fit.py") == ""                           # the first real one
    assert bash("grep -rn plink docs/") == ""
    said = bash("python fig2.py")                                # the second
    assert "second analysis-shaped command" in said
    assert rails.adhoc_runs == 2
