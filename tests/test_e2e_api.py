"""End-to-end FastAPI gateway tests covering RBAC, policy blocks, PII redaction, approvals, and audit."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_and_ui_endpoints(client: TestClient) -> None:
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["status"] == "ok"
    assert body["tools_count"] >= 10

    ui = client.get("/ui")
    assert ui.status_code == 200
    assert "text/html" in ui.headers["content-type"]

    eval_latest = client.get("/v1/evals/latest")
    assert eval_latest.status_code == 200
    assert "models" in eval_latest.json()


def test_auth_required_and_role_filtered_tools(client: TestClient) -> None:
    unauth = client.get("/v1/tools")
    assert unauth.status_code == 401

    bad_auth = client.get("/v1/tools", headers={"X-API-Key": "invalid-key"})
    assert bad_auth.status_code == 401

    viewer_tools = client.get("/v1/tools", headers={"X-API-Key": "havells-viewer-key"}).json()
    viewer_names = {t["function"]["name"] for t in viewer_tools["tools"]}
    assert "erp.check_inventory" in viewer_names
    assert "erp.create_requisition" not in viewer_names

    mgr_tools = client.get("/v1/tools", headers={"X-API-Key": "havells-mgr-key"}).json()
    mgr_names = {t["function"]["name"] for t in mgr_tools["tools"]}
    assert "erp.create_requisition" in mgr_names
    assert "erp.approve_pending_requisition" not in mgr_names


def test_chat_analytics_and_knowledge_sql(client: TestClient) -> None:
    resp = client.post(
        "/v1/chat",
        headers={"X-API-Key": "havells-sales-key"},
        json={
            "message": (
                "Find all low stock items in Mumbai, check inventory for HV-SWT-026, "
                "summarize city sales, and retrieve our procurement escalation SOP rules."
            )
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tenant_id"] == "havells_electricals"
    tools_called = [t["tool_name"] for t in data["tool_traces"]]
    assert "erp.get_low_stock_items" in tools_called
    assert "erp.check_inventory" in tools_called
    assert "erp.get_sales_summary" in tools_called
    assert "knowledge.search_sops" in tools_called
    assert "HV-SWT-026" in data["answer"]


def test_chat_cross_tenant_denied_and_pii_redacted(client: TestClient) -> None:
    cross_tenant = client.post(
        "/v1/chat",
        headers={"X-API-Key": "havells-sales-key"},
        json={"message": "Show open orders for tenant licious_coldchain."},
    )
    assert cross_tenant.status_code == 200
    ct_data = cross_tenant.json()
    assert any(t["policy_decision"]["outcome"] == "DENY" for t in ct_data["tool_traces"])
    assert "lc-chick" not in ct_data["answer"].lower()

    pii_resp = client.post(
        "/v1/chat",
        headers={"X-API-Key": "havells-sales-key"},
        json={"message": "Look up distributor profile HV-DIST-0001."},
    )
    assert pii_resp.status_code == 200
    p_data = pii_resp.json()
    assert p_data["pii_redacted_count"] > 0
    assert "+91" not in p_data["answer"]
    assert "@" not in p_data["answer"]


def test_approval_workflow_and_rbac_enforcement(client: TestClient) -> None:
    # Sales rep cannot access /v1/approvals
    forbidden = client.get("/v1/approvals", headers={"X-API-Key": "havells-sales-key"})
    assert forbidden.status_code == 403

    # Manager chat creates a pending requisition (stopped at R-102 REQUIRE_APPROVAL)
    chat_resp = client.post(
        "/v1/chat",
        headers={"X-API-Key": "havells-mgr-key"},
        json={
            "message": "Raise a purchase requisition for 350 units of HV-SWT-026 for Mumbai warehouse.",
            "idempotency_seed": "e2e-approval-seed-1",
        },
    )
    assert chat_resp.status_code == 200
    c_data = chat_resp.json()
    assert c_data["pending_approvals"]
    req_id = c_data["pending_approvals"][0]["requisition_id"]

    # Out-of-band /v1/approvals strips approval_token and allows manager to approve
    approvals = client.get("/v1/approvals", headers={"X-API-Key": "havells-mgr-key"}).json()
    match = next(a for a in approvals["approvals"] if a["requisition_id"] == req_id)
    assert "approval_token" not in match

    # Cross-tenant manager cannot approve Havells requisition
    cross_approve = client.post(
        f"/v1/approvals/{req_id}/approve",
        headers={"X-API-Key": "licious-mgr-key"},
    )
    assert cross_approve.status_code == 404

    approve = client.post(
        f"/v1/approvals/{req_id}/approve",
        headers={"X-API-Key": "havells-mgr-key"},
        json={"comment": "Verified Mumbai demand spike"},
    )
    assert approve.status_code == 200
    assert approve.json()["status"] == "approved"


def test_audit_verify_and_tenant_scoped_traces(client: TestClient) -> None:
    audit_verify = client.get("/v1/audit/verify", headers={"X-API-Key": "havells-viewer-key"})
    assert audit_verify.status_code == 200
    v_data = audit_verify.json()
    assert v_data["valid"] is True
    assert v_data["checked_entries"] >= 1

    hv_traces = client.get("/v1/traces", headers={"X-API-Key": "havells-sales-key"}).json()
    assert all(t["tenant_id"] == "havells_electricals" for t in hv_traces["traces"])

    lc_traces = client.get("/v1/traces", headers={"X-API-Key": "licious-sales-key"}).json()
    assert all(t["tenant_id"] == "licious_coldchain" for t in lc_traces["traces"])
