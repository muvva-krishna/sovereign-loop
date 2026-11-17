---
doc_id: SOP-005
tenant_scope: all
title: Inventory Reorder Points and Safety Stock Replenishment
---

# Inventory Reorder Points and Safety Stock Replenishment

## Low-Stock Detection
An SKU is flagged as below reorder point whenever `quantity_on_hand < reorder_point`. Regional planners should run `erp.get_low_stock_items` daily filtered by hub city (`Bengaluru`, `Mumbai`, `Delhi`, `Hyderabad`, `Chennai`, `Bangkok`).

## Replenishment Sizing Formula
Standard replenishment quantity is calculated as:
`target_order_qty = (reorder_point * 2) - quantity_on_hand`
Ensure `target_order_qty` does not exceed the 10,000-unit per-requisition gateway limit (`R-101`).

## Escalation Severity Matrix
- **Critical**: `quantity_on_hand == 0` (stockout) on an active SKU with open orders -> create `critical` escalation ticket and trigger `send_ops_alert` to `#ops-stockouts`.
- **High**: `quantity_on_hand < 0.5 * reorder_point` -> create `high` escalation ticket and raise a `create_requisition` request (managers only).
- **Medium**: `0.5 * reorder_point <= quantity_on_hand < reorder_point` -> include in routine weekly replenishment requisition batch.
