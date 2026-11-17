---
doc_id: SOP-004
tenant_scope: all
title: PII Handling, Redaction, and Audit Trail Integrity
---

# PII Handling, Redaction, and Audit Trail Integrity

## Protected PII Fields
Distributor profiles (`erp.get_distributor_profile`) contain regulated personally identifiable and tax information:
- **Phone numbers** (e.g. `+91-98XXXXXXXX`)
- **Email addresses** (corporate and personal contacts)
- **GSTIN numbers** (15-character Indian Goods and Services Tax Identification Numbers)

## Gateway Scrubbing Requirements
All tool outputs returning distributor contact details or tax identifiers pass through the Sovereign Gateway PII scrubber before reaching LLM prompts or end-user responses. Raw phone numbers, email addresses, and GSTINs must be masked (`[REDACTED_PHONE]`, `[REDACTED_EMAIL]`, `[REDACTED_GSTIN]`), and redaction counts must be recorded on the response trace.

## Secret and Token Hygiene in Audit Logs
Sensitive credentials—especially `approval_token` values on purchase requisitions—must be replaced with `***REDACTED***` prior to computing `entry_hash` and persisting rows to `audit_log`. Every audit entry chains to the previous `entry_hash` via SHA-256 (`contracts.audit_crypto`).
