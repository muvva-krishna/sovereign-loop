---
doc_id: SOP-010
tenant_scope: licious_coldchain
title: Licious Perishable Shelf-Life FEFO and Daily Replenishment SOP
---

# Licious Perishable Shelf-Life FEFO and Daily Replenishment SOP

## First-Expired, First-Out (FEFO) Dispatch
Fresh poultry (`LC-CHICK-*`) carries a **48-hour chilled shelf life** from processing plant dispatch; fresh seafood (`LC-SEA-*`) carries a **36-hour chilled shelf life**. Warehouse pickers must strictly enforce FEFO lot rotation across Bengaluru, Mumbai, Delhi, Hyderabad, Chennai, and Bangkok cold hubs.

## Daily Reorder Cadence
- Unlike durable goods, perishable SKUs must be replenished in frequent, right-sized batches to avoid spoilage write-offs while preventing morning stockouts.
- Whenever `quantity_on_hand < reorder_point`, regional cold-chain managers should submit a `create_requisition` sized for **2 days of projected distributor demand** (never exceeding 10,000 units per requisition per `R-101`).
- Any SKU with remaining shelf life under **12 hours** at the hub must be flagged via `ops.create_escalation_ticket` (`severity: medium`) for flash clearance or controlled disposal.
