from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock
from app.core.errors import LeaseStaleError
from app.database import get_connection, transaction


TEMPLATE = {
    "code": "solver-a",
    "name": "方程求解模板",
    "algorithm": "solver-a",
    "parameter_schema": {
        "iterations": {"type": "integer", "required": True, "minimum": 1, "maximum": 10000},
        "tolerance": {"type": "number", "required": False, "minimum": 0.0, "maximum": 1.0},
        "mode": {"type": "string", "required": True, "choices": ["fast", "accurate"]},
    },
    "default_parameters": {"tolerance": 0.001},
    "max_runtime_seconds": 300,
    "max_attempts": 2,
}


def submit_payload(key: str, *, user: str = "researcher-1", priority: int = 50) -> dict:
    return {
        "template_code": "solver-a",
        "project_code": "project-a",
        "requested_by": user,
        "parameters": {"iterations": 100, "mode": "accurate"},
        "priority": priority,
        "idempotency_key": key,
    }


def create_template(client) -> None:
    response = client.post("/api/compute/templates?actor=administrator", json=TEMPLATE)
    assert response.status_code == 201, response.text


def test_template_submission_idempotency_and_parameter_validation(client):
    create_template(client)
    first = client.post("/api/compute/tasks", json=submit_payload("request-000001"))
    second = client.post("/api/compute/tasks", json=submit_payload("request-000001"))
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    invalid = submit_payload("request-000002")
    invalid["parameters"]["iterations"] = 20000
    rejected = client.post("/api/compute/tasks", json=invalid)
    assert rejected.status_code == 422


def test_priority_capability_claim_and_result_version(client):
    create_template(client)
    low = client.post("/api/compute/tasks", json=submit_payload("priority-low", priority=10)).json()
    high = client.post("/api/compute/tasks", json=submit_payload("priority-high", priority=90)).json()
    no_match = client.post("/api/compute/tasks/claim", json={"worker_id": "w0", "capabilities": ["other"], "lease_seconds": 60})
    assert no_match.status_code == 200 and no_match.json()["task"] is None
    claimed = client.post("/api/compute/tasks/claim", json={"worker_id": "w1", "capabilities": ["solver-a"], "lease_seconds": 60})
    assert claimed.status_code == 200
    assert claimed.json()["task"]["id"] == high["id"]
    completed = client.post(
        f"/api/compute/tasks/{high['id']}/complete",
        json={"worker_id": "w1", "lease_epoch": claimed.json()["task"]["lease_epoch"], "result": {"value": 3.14}, "metrics": {"seconds": 2}},
    )
    assert completed.status_code == 200
    details = client.get(f"/api/compute/task-details/{high['id']}").json()
    assert details["status"] == "succeeded"
    assert details["current_result_version"] == 1
    assert len(details["results"]) == 1
    assert low["status"] == "queued"


def test_quota_cancel_retry_priority_and_batch_interventions(client):
    create_template(client)
    quota = client.put(
        "/api/compute/quotas?actor=administrator",
        json={"subject_type": "user", "subject_key": "limited", "max_queued": 1, "max_running": 1, "daily_submissions": 2},
    )
    assert quota.status_code == 200
    one = client.post("/api/compute/tasks", json=submit_payload("quota-one", user="limited")).json()
    blocked = client.post("/api/compute/tasks", json=submit_payload("quota-two", user="limited"))
    assert blocked.status_code == 409
    cancelled = client.post(f"/api/compute/tasks/{one['id']}/cancel", json={"actor": "administrator", "reason": "项目暂停"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    retried = client.post(f"/api/compute/tasks/{one['id']}/retry", json={"actor": "administrator", "reason": "项目恢复", "priority": 95})
    assert retried.status_code == 200 and retried.json()["priority"] == 95
    other = client.post("/api/compute/tasks", json=submit_payload("batch-other", user="other-user")).json()
    batch = client.post(
        "/api/compute/tasks/batch",
        json={"task_ids": [one["id"], other["id"]], "operation": "priority", "actor": "administrator", "reason": "紧急算例", "priority": 99},
    )
    assert batch.status_code == 200
    assert len(batch.json()["succeeded"]) == 2
    details = client.get(f"/api/compute/task-details/{one['id']}").json()
    assert [item["action"] for item in details["interventions"]] == ["cancel", "retry", "priority"]


def test_failure_backoff_and_expired_lease_recovery(client):
    from app.database import init_db

    init_db()
    clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=UTC))
    service = ComputeOperationsService(get_connection(), clock)
    service.create_template(TEMPLATE, "administrator")
    first = service.submit(submit_payload("failure-000001"))
    claimed = service.claim("worker-a", ["solver-a"], 10)
    assert claimed and claimed["id"] == first["id"]
    failed = service.fail(first["id"], "worker-a", "numeric_error", "数值不收敛", True, claimed["lease_epoch"])
    assert failed["status"] == "queued"
    assert failed["available_at"] > failed["updated_at"]
    clock.advance(seconds=2)
    claimed_again = service.claim("worker-a", ["solver-a"], 10)
    assert claimed_again and claimed_again["attempt_count"] == 2
    clock.advance(seconds=11)
    recovered = service.recover_expired()
    assert recovered["exhausted"] == [first["id"]]
    details = service.get_task(first["id"])
    assert details["status"] == "failed"
    assert details["interventions"][-1]["action"] == "lease_recovery"


def _recycled_setup(clock: FrozenClock, key: str, *, max_attempts: int = 3):
    """领取后令租约过期并安全回收，返回(服务单, 旧领取信息, 回收后的排队行)。"""
    from app.database import init_db

    init_db()
    service = ComputeOperationsService(get_connection(), clock)
    service.create_template(TEMPLATE | {"max_attempts": max_attempts}, "administrator")
    task = service.submit(submit_payload(key))
    claimed = service.claim("worker-old", ["solver-a"], 10)
    assert claimed and claimed["id"] == task["id"]
    old_epoch = claimed["lease_epoch"]
    clock.advance(seconds=11)
    recovered = service.recover_expired()
    assert recovered["recovered"] == [task["id"]]
    queued = service.get_task(task["id"])
    assert queued["status"] == "queued"
    return service, task, claimed, old_epoch


def test_expired_claim_is_safely_recycled_and_epoch_advances(client):
    clock = FrozenClock(datetime(2026, 10, 5, 2, 0, tzinfo=UTC))
    service, task, claimed, old_epoch = _recycled_setup(clock, "recycle-epoch-01")
    assert old_epoch == 1
    queued = service.get_task(task["id"])
    # 回收清空旧持有者、不保留到期租约，但世代已经提升。
    assert queued["lease_owner"] == ""
    assert queued["lease_expires_at"] == ""
    assert queued["lease_epoch"] == old_epoch + 1
    recovery = queued["interventions"][-1]
    assert recovery["action"] == "lease_recovery"
    assert "worker-old" in recovery["reason"]
    assert str(old_epoch) in recovery["reason"]


def test_stale_complete_fail_and_heartbeat_are_rejected_after_recycle(client):
    clock = FrozenClock(datetime(2026, 10, 5, 3, 0, tzinfo=UTC))
    service, task, claimed, old_epoch = _recycled_setup(clock, "stale-receipts-01")
    task_id = task["id"]

    with pytest.raises(LeaseStaleError) as heartbeat_exc:
        service.heartbeat(task_id, "worker-old", 60, old_epoch)
    assert heartbeat_exc.value.context["reason"] in {"lease_expired", "lease_recycled"}

    with pytest.raises(LeaseStaleError) as complete_exc:
        service.complete(task_id, "worker-old", {"value": 9.9}, {}, old_epoch)
    assert complete_exc.value.context["reason"] in {"lease_expired", "lease_recycled"}

    with pytest.raises(LeaseStaleError) as fail_exc:
        service.fail(task_id, "worker-old", "late_failure", "迟到的失败回执", False, old_epoch)
    assert fail_exc.value.context["reason"] in {"lease_expired", "lease_recycled"}

    details = service.get_task(task_id)
    # 旧回执没有改动任何状态，也没有写入结果版本。
    assert details["status"] == "queued"
    assert details["lease_owner"] == ""
    assert details["results"] == []
    rejected = [item for item in details["interventions"] if item["action"] == "receipt_rejected"]
    assert {item["actor"] for item in rejected} == {"worker-old"}
    assert len(rejected) == 3


def test_rejected_complete_rolls_back_inserted_result_row(client):
    clock = FrozenClock(datetime(2026, 10, 5, 4, 0, tzinfo=UTC))
    service, task, claimed, old_epoch = _recycled_setup(clock, "stale-rollback-01")
    with pytest.raises(LeaseStaleError):
        service.complete(task["id"], "worker-old", {"value": 1.0}, {}, old_epoch)
    # 即使旧领取在 INSERT 结果行之后才撞到守卫，结果行也必须随事务回滚。
    assert service.get_task(task["id"])["results"] == []
    details = service.get_task(task["id"])
    assert any(item["action"] == "receipt_rejected" for item in details["interventions"])


def test_new_claim_progress_is_not_overwritten_by_stale_receipt(client):
    clock = FrozenClock(datetime(2026, 10, 5, 5, 0, tzinfo=UTC))
    service, task, claimed, old_epoch = _recycled_setup(clock, "fencing-new-owner-01")
    task_id = task["id"]

    # 新工作人员重新领取，世代再次提升；随后正常完成。
    new_claim = service.claim("worker-new", ["solver-a"], 30)
    assert new_claim and new_claim["id"] == task_id
    new_epoch = new_claim["lease_epoch"]
    assert new_epoch > old_epoch
    completed = service.complete(task_id, "worker-new", {"value": 3.14}, {"seconds": 2}, new_epoch)
    assert completed["status"] == "succeeded"
    assert completed["current_result_version"] == 1

    # 旧工作人员拿着旧世代提交完成/失败/心跳，全部被拒绝且不能覆盖新终态。
    for call in (
        lambda: service.complete(task_id, "worker-old", {"value": 9.9}, {}, old_epoch),
        lambda: service.fail(task_id, "worker-old", "late", "迟到失败", False, old_epoch),
        lambda: service.heartbeat(task_id, "worker-old", 60, old_epoch),
    ):
        with pytest.raises(LeaseStaleError):
            call()

    details = service.get_task(task_id)
    assert details["status"] == "succeeded"
    assert details["lease_owner"] == ""
    assert details["current_result_version"] == 1
    assert len(details["results"]) == 1
    assert details["results"][0]["created_by"] == "worker-new"
    assert any(item["action"] == "receipt_rejected" and item["actor"] == "worker-old" for item in details["interventions"])


def test_epoch_mismatch_is_rejected_even_when_lease_unexpired(client):
    # 同一工作者伪造/错拿世代也不能写入：世代是权威 fencing 凭证。
    clock = FrozenClock(datetime(2026, 10, 5, 6, 0, tzinfo=UTC))
    from app.database import init_db

    init_db()
    service = ComputeOperationsService(get_connection(), clock)
    service.create_template(TEMPLATE, "administrator")
    task = service.submit(submit_payload("fencing-epoch-01"))
    claimed = service.claim("worker-a", ["solver-a"], 60)
    with pytest.raises(LeaseStaleError) as exc:
        service.complete(task["id"], "worker-a", {"v": 1}, {}, claimed["lease_epoch"] + 5)
    assert exc.value.context["reason"] == "epoch_mismatch"
    # 拒绝不影响服务单：仍可凭正确世代正常完成。
    done = service.complete(task["id"], "worker-a", {"v": 1}, {}, claimed["lease_epoch"])
    assert done["status"] == "succeeded"


def test_stale_receipt_endpoints_return_structured_conflict(client):
    create_template(client)
    task = client.post("/api/compute/tasks", json=submit_payload("http-stale-01")).json()
    claimed = client.post("/api/compute/tasks/claim", json={"worker_id": "w-old", "capabilities": ["solver-a"], "lease_seconds": 5}).json()["task"]
    old_epoch = claimed["lease_epoch"]
    # 不经过回收：租约本身到期后旧回执也应直接被拒绝。
    import time

    time.sleep(6)
    response = client.post(
        f"/api/compute/tasks/{task['id']}/complete",
        json={"worker_id": "w-old", "lease_epoch": old_epoch, "result": {"v": 1}, "metrics": {}},
    )
    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "lease_stale"
    assert body["context"]["reason"] == "lease_expired"
    assert body["context"]["worker_id"] == "w-old"
    details = client.get(f"/api/compute/task-details/{task['id']}").json()
    assert details["status"] == "running"
    assert any(item["action"] == "receipt_rejected" for item in details["interventions"])
    # 查询、重试、取消接口仍然可用：回收后可重新领取，取消流程不受影响。
    assert client.get(f"/api/compute/task-details/{task['id']}").status_code == 200
    recover = client.post("/api/compute/recovery/expired-leases?actor=recovery-worker")
    assert recover.status_code == 200 and recover.json()["recovered"] == [task["id"]]
    cancel = client.post(f"/api/compute/tasks/{task['id']}/cancel", json={"actor": "manager", "reason": "现场撤单"})
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancelled"
