---
doc_id: SOP-006
tenant_scope: all
title: Sales Order Dispute Resolution and Cancellation SLA
---

# Sales Order Dispute Resolution and Cancellation SLA

## Order Status Definitions
- `open`: Confirmed distributor order awaiting warehouse dispatch.
- `fulfilled`: Delivered and signed off by distributor receiving dock.
- `disputed`: Delivery discrepancy, damaged goods, short-shipment, or billing mismatch reported by distributor.
- `cancelled`: Order voided prior to dispatch or closed via approved credit memo.

## Dispute Resolution Timeline
1. **Acknowledgement (within 4 hours)**: Log an operational ticket (`ops.create_escalation_ticket`) citing the `order_id` and `distributor_id`.
2. **Investigation (within 24 hours)**: Verify warehouse dispatch logs and inventory movement for the affected `material_id`.
3. **Resolution (within 72 hours)**: Issue replacement stock or convert order to `cancelled` with credit note approval.

## High-Value Dispute Escalation
Any `disputed` order exceeding **INR 250,000** must be escalated with severity `high` and flagged to the regional manager via `ops.send_ops_alert`.
