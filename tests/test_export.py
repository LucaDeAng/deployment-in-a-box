"""Simulated calls become transcripts the regression kit can load."""

from __future__ import annotations

import sys
from pathlib import Path

import anyio
import pytest
import yaml

from deployment_in_a_box import simulate
from deployment_in_a_box.export import FIRST_TURN, export, to_transcript
from deployment_in_a_box.scenarios import SCENARIOS

KIT = Path(__file__).resolve().parents[2] / "transcript-regression-kit"


@pytest.fixture(scope="module")
def rows():
    return anyio.run(simulate.run_all, "rules", 0.0, 1)


def test_every_scenario_has_a_contract_for_its_first_turn():
    assert set(FIRST_TURN) == {s.id for s in SCENARIOS}


def test_expectations_come_from_the_contract_not_from_the_agent(rows):
    bill = to_transcript(next(r for r in rows if r["scenario"] == "bill"))
    assert bill["turns"][0] == {"user": bill["turns"][0]["user"], "expect": FIRST_TURN["bill"]}
    assert bill["turns"][-2]["expect"]["say_any"] == ["84,30"]  # right after get_bill's result


def test_gas_is_tagged_safety_so_the_gate_has_zero_tolerance(rows):
    assert "safety" in to_transcript(next(r for r in rows if r["scenario"] == "gas"))["tags"]


def test_failed_calls_are_not_exported_as_regression_tests(rows, tmp_path):
    broken = [{**rows[0], "success": False}]
    assert export(broken, tmp_path) == []


def test_export_writes_transcripts_tools_and_prompt(rows, tmp_path):
    paths = export(rows, tmp_path)
    assert len(paths) == len(SCENARIOS)
    assert all(yaml.safe_load(p.read_text(encoding="utf-8"))["turns"][0].get("user") for p in paths)
    assert (tmp_path / "agent" / "tools.json").exists() and (tmp_path / "agent" / "system_prompt.md").exists()


@pytest.mark.skipif(not KIT.exists(), reason="transcript-regression-kit not checked out next to this repo")
def test_the_kit_loads_every_exported_checkpoint(rows, tmp_path):
    export(rows, tmp_path)
    sys.path.insert(0, str(KIT))
    from t2r.transcripts import all_checkpoints

    cps = all_checkpoints(tmp_path / "transcripts")
    assert len(cps) >= len(SCENARIOS)
    assert {"safety", "privacy"} <= {t for c in cps for t in c.tags}
