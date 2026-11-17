---
doc_id: SOP-001
tenant_scope: all
title: Purchase Requisition Approval Thresholds and Controls
---

# Purchase Requisition Approval Thresholds and Controls

## Mandatory Human-in-the-Loop Approval
All purchase requisitions submitted via `erp.create_requisition` enter the `pending_approval` state regardless of monetary value. AI assistants and automated workflows are strictly prohibited from self-approving or rejecting requisitions (`R-002`). Approvals and rejections must occur out-of-band through the authenticated Admin API using a valid `approval_token`.

## Quantity Ceilings and Monetary Tiers
- **Hard Quantity Ceiling (`R-101`)**: Any single purchase requisition exceeding **10,000 units** is automatically denied by deterministic gateway policy. Split large replenishment waves or escalate to VP Procurement.
- **Tier 1 (Under INR 250,000)**: Regional Operations Manager approval required via Admin API.
- **Tier 2 (INR 250,000 – INR 1,500,000)**: Finance Controller + Category Head dual review before releasing the `approval_token`.
- **Tier 3 (Above INR 1,500,000)**: CFO sign-off mandatory prior to out-of-band token issuance.

## Idempotency and Duplicate Prevention
Callers must supply a deterministic `idempotency_key` when retrying `create_requisition` calls after transient network timeouts. Reusing an `idempotency_key` within the same `tenant_id` returns the original requisition with `cached_idempotent=true` without creating duplicate PO liabilities.
