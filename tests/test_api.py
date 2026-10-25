from fastapi.testclient import TestClient

from finops.api.app import app
from finops.demo import seed_demo


def test_api_endpoints():
    seed_demo()  # into the per-test DB from conftest; the app's lifespan keeps that binding
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}

        summary = client.get("/summary").json()
        assert summary["total_monthly_savings"] > 0
        assert set(summary["by_risk_tier"]) <= {"safe_auto", "approval", "manual_only"}

        recs = client.get("/recommendations", params={"risk_tier": "safe_auto"}).json()
        assert recs and all(r["risk_tier"] == "safe_auto" for r in recs)

        ebs = client.get("/resources", params={"type": "ebs"}).json()
        assert ebs and all(r["type"] == "ebs" for r in ebs)
        assert client.get("/resources/demo-i-api-staging/metrics").json()

        rec_id = recs[0]["id"]
        approved = client.post(f"/recommendations/{rec_id}/approve", json={"approved_by": "tester"}).json()
        assert approved["status"] == "approved"
        dry = client.post(f"/actions/{approved['id']}/execute", params={"dry_run": True}).json()
        assert dry["dry_run"] is True and dry["plan"]
        assert client.get("/actions").json()[0]["id"] == approved["id"]

        manual = client.get("/recommendations", params={"risk_tier": "manual_only"}).json()
        no_action = next(r for r in manual if r["action_type"] is None)
        assert client.post(f"/recommendations/{no_action['id']}/plan").status_code == 400

        assert client.get("/runs").json()[0]["kind"] == "analyze"
        assert "compute_optimizer" in client.get("/validation").json()
