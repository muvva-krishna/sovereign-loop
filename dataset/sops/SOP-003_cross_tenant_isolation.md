---
doc_id: SOP-003
tenant_scope: all
title: Cross-Tenant Data Isolation and Sovereignty Policy
---

# Cross-Tenant Data Isolation and Sovereignty Policy

## Strict Tenant Binding
Every API key maps deterministically to a single `tenant_id` (`havells_electricals` or `licious_coldchain`). All ERP queries, inventory checks, distributor lookups, SOP searches, and operational tickets are scoped to the authenticated caller's `tenant_id`.

## Zero Cross-Tenant Visibility (`R-001`)
- An agent or user authenticated under one tenant must **never** query, infer, aggregate, or reference SKUs, sales orders, distributors, or tenant-specific SOPs belonging to another tenant.
- Any explicit `tenant_id` tool argument that does not match the caller's authenticated `tenant_id` is immediately blocked with outcome `DENY` (`R-001`) and recorded in the SHA-256 hash-chained audit log.

## Knowledge Corpus Scoping
Global SOPs (`tenant_scope: all`) are visible to all tenants. Tenant-specific operational playbooks (`tenant_scope: havells_electricals` or `tenant_scope: licious_coldchain`) must never leak across tenant boundaries in either `search_sops` or `get_sop_document`.
