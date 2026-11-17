---
doc_id: SOP-007
tenant_scope: havells_electricals
title: Havells Electricals Product Warranty and Defective Batch SLA
---

# Havells Electricals Product Warranty and Defective Batch SLA

## Standard Warranty Coverage by Product Line
- **BLDC & Ceiling Fans (`HV-FAN-*`)**: 24-month on-site replacement warranty against motor winding or PCB controller defects.
- **LED Lighting & Luminaires (`HV-LED-*`)**: 24-month replacement warranty against driver failure or >20% lumen depreciation.
- **Modular Switches & Switchgear (`HV-SWT-*`)**: 12-month commercial warranty; 10-year mechanical life guarantee on MCB contacts.
- **Armoured & FR-LSH Cables (`HV-CBL-*`)**: 12-month manufacturing defect warranty subject to insulation resistance test report.

## Defective Batch Escalation Protocol
If a distributor reports **3 or more defective units** from the same SKU batch within 14 days:
1. Raise a `high` severity escalation ticket via `ops.create_escalation_ticket` citing the `material_id` and city warehouse.
2. Quarantine remaining stock at the regional warehouse (`Bengaluru`, `Mumbai`, `Delhi`, `Hyderabad`, `Chennai`, or `Bangkok`).
3. Notify `#havells-quality-ops` via `ops.send_ops_alert` (manager role required).
