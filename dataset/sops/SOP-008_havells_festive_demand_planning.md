---
doc_id: SOP-008
tenant_scope: havells_electricals
title: Havells Electricals Regional Demand and Festive Buffer Stock SOP
---

# Havells Electricals Regional Demand and Festive Buffer Stock SOP

## Seasonal Demand Multipliers
Havells Electricals experiences peak distributor pull during summer cooling season (March–June for ceiling and exhaust fans) and festive lighting quarter (September–November for LED panels and decorative switches):
- **Fans (`HV-FAN-*`)**: Maintain **1.5x standard reorder_point** in Delhi, Hyderabad, and Chennai warehouses from February onward.
- **LED Lighting (`HV-LED-*`)**: Maintain **1.4x standard reorder_point** across Mumbai and Bengaluru hubs ahead of Q3/Q4 festive demand.
- **Industrial Cables (`HV-CBL-*`)**: Maintain steady safety stock aligned with infrastructure contractor orders in Bangkok and Mumbai.

## Replenishment Prioritization
When multiple Havells SKUs fall below `reorder_point`, managers must prioritize `open` order backlog coverage first, submitting separate `create_requisition` requests per SKU with unique `idempotency_key` identifiers.
