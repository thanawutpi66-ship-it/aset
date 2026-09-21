import threading

import pytest

from aset_batt.app.operation_state import ApplicationState, OperationState


@pytest.mark.parametrize("kind", ["quick", "iec", "hppc", "cycle"])
def test_sequence_cancel_blocks_restart_until_worker_release(kind):
    state = OperationState()
    first = state.claim(kind)
    assert first is not None
    assert state.running(first)
    assert state.request_cancel(first)
    assert state.state is ApplicationState.CANCELLING
    assert first.cancel.is_set()
    assert state.claim(kind) is None
    state.cleanup(first)
    assert state.release(first)
    second = state.claim(kind)
    assert second is not None and second.run_id != first.run_id


@pytest.mark.parametrize("kind", ["eta", "pk", "gitt", "cca"])
def test_characterization_restart_uses_distinct_generation(kind):
    state = OperationState()
    old = state.claim(f"characterize:{kind}")
    assert old is not None
    state.running(old)
    state.request_cancel(old)
    assert state.claim(f"characterize:{kind}") is None
    state.cleanup(old)
    assert state.release(old)
    new = state.claim(f"characterize:{kind}")
    assert new is not None and new.run_id != old.run_id
    # A late callback from the old worker cannot release the new lease.
    assert not state.release(old)
    assert state.active is new


def test_estop_latched_worker_cannot_become_idle_from_late_release():
    state = OperationState()
    lease = state.claim("quick")
    state.running(lease)
    state.latch_estop()
    state.cleanup(lease)
    assert state.release(lease)
    assert state.state is ApplicationState.ESTOP_LATCHED
    assert state.claim("quick") is None
    assert state.reset_estop(workers_exited=True, outputs_off_confirmed=True)
    assert state.state is ApplicationState.IDLE


@pytest.mark.parametrize("terminal", ["success", "cancel", "exception"])
def test_characterization_release_is_exactly_once(terminal):
    state = OperationState()
    lease = state.claim("characterize:pk")
    state.running(lease)
    if terminal == "cancel":
        state.request_cancel(lease)
    state.cleanup(lease)
    assert state.release(lease, fault="worker failed" if terminal == "exception" else "")
    assert not state.release(lease)
    assert state.active is None
