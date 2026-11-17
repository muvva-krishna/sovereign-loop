---
doc_id: SOP-009
tenant_scope: licious_coldchain
title: Licious ColdChain Temperature Excursion and Fleet SLA
---

# Licious ColdChain Temperature Excursion and Fleet SLA

## Mandatory Cold-Chain Thermal Bands
All Licious ColdChain storage hubs and reefer trucks must maintain uninterrupted temperature compliance:
- **Chilled Fresh Poultry & Seafood (`LC-CHICK-*`, `LC-SEA-*`)**: Strict **0°C to 4°C** core product temperature.
- **Marinades & Ready-to-Cook (`LC-MAR-*`, `LC-RTC-*`)**: **0°C to 4°C** chilled or **-18°C** blast-frozen depending on SKU specification.

## Excursion Breach Response (15-Minute Rule)
Any warehouse cold-room or reefer telemetry excursion above **4.5°C for more than 15 consecutive minutes** constitutes a food-safety incident:
1. Immediately create a **`critical`** operational ticket via `ops.create_escalation_ticket` referencing the affected `material_id` or `order_id`.
2. Dispatch an urgent alert via `ops.send_ops_alert` to channel `#licious-coldchain-critical` (manager authorization).
3. Quarantine the impacted batch; never dispatch compromised perishable stock to distributors.
4. Initiate emergency replenishment via `erp.create_requisition` so hub stock recovers before morning dispatch cut-off.
