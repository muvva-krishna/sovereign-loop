---
doc_id: SOP-002
tenant_scope: all
title: Distributor Credit Limits and Exposure Governance
---

# Distributor Credit Limits and Exposure Governance

## Credit Tier Structure
Distributors are assigned to one of three commercial tiers based on annual turnover, GST compliance, and repayment track record:
- **Platinum**: Credit limit INR 2,500,000 – INR 5,000,000; 45-day net terms.
- **Gold**: Credit limit INR 1,000,000 – INR 2,499,999; 30-day net terms.
- **Silver**: Credit limit INR 300,000 – INR 999,999; 15-day net terms or advance NEFT.

## Credit Hold Triggers
A distributor account must be placed on automatic dispatch hold if:
1. Open + disputed sales order exposure exceeds **90% of `credit_limit`**.
2. Any invoice remains overdue beyond 15 days past net terms.
3. Two or more `disputed` sales orders remain unresolved for over 7 business days.

## Escalation Workflow
Sales representatives (`sales_rep`) and managers (`manager`) reviewing distributor exposure must create an operational escalation ticket (`ops.create_escalation_ticket`) with severity `high` referencing the `distributor_id` before authorizing override dispatches.
