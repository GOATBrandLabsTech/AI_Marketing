"""Temporary: deliberately failing test to prove the server deploy gate
rejects a red push. Removed in the very next commit."""


def test_deploy_gate_rejects_a_failing_push():
    assert False, "deliberate failure - deploy gate end-to-end check"
