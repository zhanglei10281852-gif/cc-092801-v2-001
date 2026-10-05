from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.compute.service import ComputeOperationsService
from app.core.clock import FrozenClock
from app.core.errors import ConflictError, LeaseExpiredError
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
        json={"worker_id": "w1", "result": {"value": 3.14}, "metrics": {"seconds": 2}},
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
    failed = service.fail(first["id"], "worker-a", "numeric_error", "数值不收敛", True)
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


def build_service() -> ComputeOperationsService:
    from app.database import init_db

    init_db()
    clock = FrozenClock(datetime(2026, 9, 26, 2, 0, tzinfo=UTC))
    service = ComputeOperationsService(get_connection(), clock)
    service.create_template(TEMPLATE, "administrator")
    return service


def test_expired_lease_complete_rejected_and_safely_recovered(client):
    service = build_service()
    task = service.submit(submit_payload("lease-expire-complete"))
    claimed = service.claim("worker-a", ["solver-a"], 10)
    assert claimed and claimed["id"] == task["id"]
    service.clock.advance(seconds=11)

    with pytest.raises(LeaseExpiredError) as expired:
        service.complete(task["id"], "worker-a", {"value": 1}, {})
    assert expired.value.code == "lease_expired"

    details = service.get_task(task["id"])
    assert details["status"] == "queued"
    assert details["lease_owner"] == ""
    assert details["results"] == []
    actions = [item["action"] for item in details["interventions"]]
    assert actions == ["lease_recovery", "receipt_rejected"]
    rejection = details["interventions"][-1]
    assert rejection["actor"] == "worker-a"
    assert "领取时限已过" in rejection["reason"]

    with pytest.raises(ConflictError):
        service.heartbeat(task["id"], "worker-a", 10)
    with pytest.raises(ConflictError):
        service.fail(task["id"], "worker-a", "late_error", "过期的失败回执", True)

    reclaimed = service.claim("worker-b", ["solver-a"], 60)
    assert reclaimed and reclaimed["lease_owner"] == "worker-b"
    assert reclaimed["attempt_count"] == 2
    with pytest.raises(ConflictError):
        service.complete(task["id"], "worker-a", {"value": 1}, {})

    heartbeat = service.heartbeat(task["id"], "worker-b", 60)
    assert heartbeat["lease_owner"] == "worker-b"
    completed = service.complete(task["id"], "worker-b", {"value": 2}, {"seconds": 5})
    assert completed["status"] == "succeeded"

    details = service.get_task(task["id"])
    assert details["status"] == "succeeded"
    assert len(details["results"]) == 1
    assert details["results"][0]["created_by"] == "worker-b"
    rejected = [item for item in details["interventions"] if item["action"] == "receipt_rejected"]
    assert [item["actor"] for item in rejected] == ["worker-a"] * 4


def test_expired_lease_heartbeat_and_fail_converge_to_failed(client):
    service = build_service()
    task = service.submit(submit_payload("lease-expire-heartbeat"))
    service.claim("worker-a", ["solver-a"], 10)
    service.clock.advance(seconds=11)

    with pytest.raises(LeaseExpiredError):
        service.heartbeat(task["id"], "worker-a", 10)
    assert service.get_task(task["id"])["status"] == "queued"

    reclaimed = service.claim("worker-a", ["solver-a"], 10)
    assert reclaimed["attempt_count"] == 2
    service.clock.advance(seconds=11)
    with pytest.raises(LeaseExpiredError):
        service.fail(task["id"], "worker-a", "late_error", "过期的失败回执", True)

    details = service.get_task(task["id"])
    assert details["status"] == "failed"
    assert details["last_error_code"] == "lease_expired"
    actions = [item["action"] for item in details["interventions"]]
    assert actions == ["lease_recovery", "receipt_rejected", "lease_recovery", "receipt_rejected"]
    assert details["results"] == []


def test_stale_attempt_count_receipt_cannot_overwrite_new_claim(client):
    service = build_service()
    task = service.submit(submit_payload("lease-fencing-token"))
    first_claim = service.claim("worker-a", ["solver-a"], 10)
    service.clock.advance(seconds=11)
    recovered = service.recover_expired()
    assert recovered["recovered"] == [task["id"]]

    second_claim = service.claim("worker-a", ["solver-a"], 60)
    assert second_claim["attempt_count"] == first_claim["attempt_count"] + 1

    with pytest.raises(ConflictError) as stale:
        service.complete(task["id"], "worker-a", {"value": 1}, {}, attempt_count=first_claim["attempt_count"])
    assert "领取已失效" in stale.value.message
    assert service.get_task(task["id"])["status"] == "running"
    assert service.get_task(task["id"])["results"] == []

    completed = service.complete(task["id"], "worker-a", {"value": 2}, {}, attempt_count=second_claim["attempt_count"])
    assert completed["status"] == "succeeded"


def test_expired_lease_receipt_rejected_over_api(client):
    create_template(client)
    task = client.post("/api/compute/tasks", json=submit_payload("lease-expire-api")).json()
    claimed = client.post("/api/compute/tasks/claim", json={"worker_id": "worker-a", "capabilities": ["solver-a"], "lease_seconds": 30})
    assert claimed.status_code == 200 and claimed.json()["task"]["id"] == task["id"]
    get_connection().execute("UPDATE compute_tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?", (task["id"],))

    expired = client.post(
        f"/api/compute/tasks/{task['id']}/complete",
        json={"worker_id": "worker-a", "result": {"value": 1}, "metrics": {}},
    )
    assert expired.status_code == 409
    assert expired.json()["error"]["code"] == "lease_expired"

    details = client.get(f"/api/compute/task-details/{task['id']}").json()
    assert details["status"] == "queued"
    assert details["results"] == []
    actions = [item["action"] for item in details["interventions"]]
    assert actions == ["lease_recovery", "receipt_rejected"]
    assert details["interventions"][-1]["actor"] == "worker-a"

    reclaimed = client.post("/api/compute/tasks/claim", json={"worker_id": "worker-b", "capabilities": ["solver-a"], "lease_seconds": 60})
    assert reclaimed.status_code == 200 and reclaimed.json()["task"]["lease_owner"] == "worker-b"
    stale = client.post(
        f"/api/compute/tasks/{task['id']}/complete",
        json={"worker_id": "worker-a", "result": {"value": 1}, "metrics": {}},
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "conflict"
    completed = client.post(
        f"/api/compute/tasks/{task['id']}/complete",
        json={"worker_id": "worker-b", "result": {"value": 2}, "metrics": {"seconds": 3}},
    )
    assert completed.status_code == 200 and completed.json()["status"] == "succeeded"

    details = client.get(f"/api/compute/task-details/{task['id']}").json()
    assert details["status"] == "succeeded"
    assert len(details["results"]) == 1
    assert details["results"][0]["created_by"] == "worker-b"
    summary = client.get("/api/compute/summary").json()
    assert summary["states"].get("succeeded") == 1
