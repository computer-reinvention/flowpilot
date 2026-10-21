"""Acceptance gate `script_reuse_repair` on real Chromium with a synthetic loopback site."""

import pytest

from tests.task_acceptance_support.evidence import Evidence
from tests.task_acceptance_support.script_reuse import check_script_reuse_repair, prove_script_reuse_repair


@pytest.mark.parametrize('hosted', [False, True], ids=['repl', 'hosted'])
async def test_script_reuse_repair_gate(tmp_path, hosted):
    evidence = await prove_script_reuse_repair(tmp_path, hosted=hosted)
    # (a) two parameterized reuses + (b) repaired run + reuse of the repaired script.
    assert evidence['searches'] == 4
    # (c) partial effect applied exactly once and completed without double-apply.
    assert evidence['adds'] == 1 and evidence['extras'] == 1
    # Local REPL: exactly one automatic selector repair. Hosted: never automatic.
    assert evidence['automatic_repairs'] == (0 if hosted else 1)
    assert evidence['input_errors'] == 1


def test_gate_records_requirement(tmp_path):
    evidence = Evidence('system')
    assert evidence.check('script_reuse_repair', lambda: check_script_reuse_repair(str(tmp_path)), 'script_reuse_repair')
    assert evidence.requirements['script_reuse_repair']['outcome'] == 'passed'
    assert evidence.checks[-1]['partial_adds'] == [1, 1]
