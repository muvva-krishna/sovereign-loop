from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any

from contracts.audit_crypto import GENESIS_HASH, compute_entry_hash
from services.mcp_erp import db as erp_db
from services.mcp_erp.models import (
    AuditLog,
    Distributor,
    InventoryItem,
    OpsTicket,
    PurchaseRequisition,
    SalesOrder,
)
from services.mcp_erp.tools import verify_all_audit_entries

CITIES: tuple[tuple[str, str, str, str], ...] = (
    ("Bengaluru", "South India", "BLR", "29"),
    ("Mumbai", "West India", "BOM", "27"),
    ("Delhi", "North India", "DEL", "07"),
    ("Hyderabad", "South India", "HYD", "36"),
    ("Chennai", "South India", "MAA", "33"),
    ("Bangkok", "Southeast Asia", "BKK", "99"),
)

_DISTRIBUTOR_PREFIXES = (
    "Apex",
    "Bharat",
    "Cauvery",
    "Deccan",
    "Eastern",
    "Frontier",
    "Ganga",
    "Horizon",
    "Indus",
    "Jaya",
    "Kaveri",
    "Lotus",
    "Metro",
    "Narmada",
    "Orient",
    "Pacific",
    "Prime",
    "Saffron",
    "Shakti",
    "Siam",
    "Southern",
    "Sunrise",
    "United",
    "Vanguard",
    "Zenith",
)

_DISTRIBUTOR_SUFFIXES_BY_TENANT: dict[str, tuple[str, ...]] = {
    "havells_electricals": (
        "Electricals",
        "Power Systems",
        "Switchgear Traders",
        "Lighting Mart",
        "Volt Agencies",
        "Cables & Controls",
    ),
    "licious_coldchain": (
        "ColdChain Foods",
        "Fresh Mart",
        "Chilled Logistics",
        "Gourmet Proteins",
        "Culinary Supply",
        "Prime Meats & Seafood",
    ),
}

_TIERS: tuple[tuple[str, tuple[int, int]], ...] = (
    ("platinum", (2_500_000, 5_000_000)),
    ("gold", (1_000_000, 2_400_000)),
    ("silver", (300_000, 950_000)),
)

_HAVELLS_CATALOG: tuple[tuple[str, str, tuple[str, ...], tuple[int, int]], ...] = (
    (
        "FAN",
        "Fans",
        (
            "Stealth Air BLDC Ceiling Fan 1200mm",
            "Festiva ES Energy Saver Ceiling Fan",
            "VentilAir Heavy Duty Exhaust Fan 300mm",
            "Velocity Neo High-Speed Pedestal Fan",
            "Ambrose Wall-Mount Oscillating Fan",
        ),
        (2200, 6800),
    ),
    (
        "SWT",
        "Switches",
        (
            "Crabtree Athena 16A Modular Socket",
            "Reo Elegant 6A One-Way Switch Pack",
            "Euro-II 32A Double Pole MCB",
            "Fabio Smart Wi-Fi Touch Switch 4M",
            "Standard Distribution Board 8-Way SPN",
        ),
        (180, 2400),
    ),
    (
        "LED",
        "LED Lighting",
        (
            "Glaze 15W Slim Round LED Panel",
            "Adore 20W Cool White LED Batten",
            "Endura 50W IP65 LED Floodlight",
            "Steller 12W Architectural Downlight",
            "Dazzle 9W Smart RGBW LED Bulb Pack",
        ),
        (250, 4200),
    ),
    (
        "CBL",
        "Cables",
        (
            "Life Line Plus HRFR 1.5 sqmm 90m Coil",
            "Armoured XLPE 4-Core Power Cable 16 sqmm",
            "FRLS Flexible Copper Wire 2.5 sqmm",
            "Coaxial RG-6 Shielded Communication Cable",
            "Solar DC TUV-Rated 4 sqmm Cable Drum",
        ),
        (1400, 9500),
    ),
)

_LICIOUS_CATALOG: tuple[tuple[str, str, tuple[str, ...], tuple[int, int]], ...] = (
    (
        "CHICK",
        "Fresh Poultry",
        (
            "Chicken Curry Cut Skinless 500g Chilled",
            "Chicken Breast Boneless Antibiotic-Free 450g",
            "Chicken Drumsticks Tangdi Pack 500g",
            "Chicken Thigh Boneless Cubes 450g",
            "Country Chicken Curry Cut 500g",
        ),
        (190, 420),
    ),
    (
        "SEA",
        "Seafood",
        (
            "Atlantic Salmon Fillet Fresh Chilled 300g",
            "Indian Prawns Cleaned & Deveined 250g",
            "Seer Fish Surmai Steaks 500g",
            "Rohu Bengali Cut Fresh 500g",
            "Pomfret Whole Cleaned Chilled 500g",
        ),
        (320, 980),
    ),
    (
        "MAR",
        "Marinades",
        (
            "Amritsari Achari Murgh Tikka 450g",
            "Hyderabadi Chicken Dum Marinade 500g",
            "Malabar Pepper Prawns Marinated 300g",
            "Classic Tandoori Chicken Half-Bird 500g",
            "Peri-Peri Chicken Wings Tray 400g",
        ),
        (260, 560),
    ),
    (
        "RTC",
        "Ready-to-Cook",
        (
            "Crispy Chicken Nuggets Frozen Pack 400g",
            "Lucknowi Chicken Galouti Kebab 250g",
            "Prawn Tempura Crunch Tray 250g",
            "Herb Butter Chicken Breast Fillet 300g",
            "Classic Chicken Seekh Kebab 320g",
        ),
        (240, 620),
    ),
)

_ORDER_STATUS_WEIGHTS: tuple[tuple[str, int], ...] = (
    ("fulfilled", 55),
    ("open", 30),
    ("disputed", 10),
    ("cancelled", 5),
)


def _make_gstin(state_code: str, tenant_code: str, idx: int) -> str:
    letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    c2 = letters[(idx * 7) % len(letters)]
    num = f"{1000 + idx:04d}"
    check = letters[(idx * 3 + 5) % len(letters)]
    return f"{state_code}AABC{tenant_code}{num}{c2}1Z{check}"


def _build_distributors(rng: random.Random) -> list[Distributor]:
    distributors: list[Distributor] = []
    tenants = [
        ("havells_electricals", "HV", "H"),
        ("licious_coldchain", "LC", "L"),
    ]
    for tenant_id, prefix, gst_char in tenants:
        suffixes = _DISTRIBUTOR_SUFFIXES_BY_TENANT[tenant_id]
        for i in range(1, 251):
            city, region, _hub, state_code = CITIES[(i - 1) % len(CITIES)]
            tier_name, (min_lim, max_lim) = _TIERS[i % len(_TIERS)]
            offset = 0 if prefix == "HV" else 11
            p_name = _DISTRIBUTOR_PREFIXES[(i * 3 + offset) % len(_DISTRIBUTOR_PREFIXES)]
            s_name = suffixes[(i + rng.randint(0, 5)) % len(suffixes)]
            dist_id = f"{prefix}-DIST-{i:03d}"
            slug = f"{p_name.lower()}.{prefix.lower()}{i:03d}"
            phone_num = f"+91-98{rng.randint(10000000, 99999999)}"
            credit_limit = round(rng.randrange(min_lim, max_lim + 1, 50_000), 2)
            distributors.append(
                Distributor(
                    distributor_id=dist_id,
                    tenant_id=tenant_id,
                    name=f"{p_name} {s_name} ({city})",
                    city=city,
                    region=region,
                    phone=phone_num,
                    email=f"orders@{slug}.in",
                    gstin=_make_gstin(state_code, gst_char, i),
                    credit_limit=credit_limit,
                    tier=tier_name,
                )
            )
    return distributors


def _build_inventory(rng: random.Random) -> list[InventoryItem]:
    items: list[InventoryItem] = []
    specs = [
        ("havells_electricals", "HV", _HAVELLS_CATALOG),
        ("licious_coldchain", "LC", _LICIOUS_CATALOG),
    ]
    for tenant_id, prefix, catalog in specs:
        for i in range(1, 101):
            group_idx = min((i - 1) // 25, 3)
            cat_code, category, descriptions, (min_p, max_p) = catalog[group_idx]
            material_id = f"{prefix}-SKU-100" if i == 100 else f"{prefix}-{cat_code}-{i:03d}"
            city, _region, hub_code, _state = CITIES[(i - 1) % len(CITIES)]
            desc = f"{descriptions[(i - 1) % len(descriptions)]} [{i:03d}]"
            unit_price = round(rng.uniform(min_p, max_p), 2)
            reorder_point = rng.randint(120, 450)
            # Exactly 25% (first 25 of 100, distributed across all 6 cities) below reorder_point
            if i <= 25:
                qty_on_hand = rng.randint(5, max(10, reorder_point - 25))
            else:
                qty_on_hand = rng.randint(reorder_point + 30, reorder_point + 900)

            items.append(
                InventoryItem(
                    tenant_id=tenant_id,
                    material_id=material_id,
                    description=desc,
                    category=category,
                    warehouse=f"WH-{prefix}-{hub_code}",
                    city=city,
                    unit_price=unit_price,
                    quantity_on_hand=qty_on_hand,
                    reorder_point=reorder_point,
                )
            )
    return items


def _build_sales_orders(
    rng: random.Random,
    distributors: list[Distributor],
    inventory: list[InventoryItem],
) -> list[SalesOrder]:
    orders: list[SalesOrder] = []
    dist_by_tenant: dict[str, list[Distributor]] = {}
    sku_by_tenant: dict[str, list[InventoryItem]] = {}
    for d in distributors:
        dist_by_tenant.setdefault(d.tenant_id, []).append(d)
    for item in inventory:
        sku_by_tenant.setdefault(item.tenant_id, []).append(item)

    status_pool: list[str] = []
    for status_name, weight in _ORDER_STATUS_WEIGHTS:
        status_pool.extend([status_name] * weight)

    start_date = date(2026, 6, 1)
    for tenant_id, prefix in (("havells_electricals", "HV"), ("licious_coldchain", "LC")):
        t_dists = dist_by_tenant[tenant_id]
        t_skus = sku_by_tenant[tenant_id]
        for idx in range(1, 2601):
            dist = t_dists[rng.randrange(len(t_dists))]
            sku = t_skus[rng.randrange(len(t_skus))]
            status = status_pool[rng.randrange(len(status_pool))]
            units = rng.randint(5, 120)
            total_value = round(float(sku.unit_price) * units, 2)
            order_dt = start_date + timedelta(days=rng.randint(0, 165))
            orders.append(
                SalesOrder(
                    order_id=f"{prefix}-SO-{idx:04d}",
                    tenant_id=tenant_id,
                    distributor_id=dist.distributor_id,
                    customer_name=dist.name,
                    city=dist.city,
                    status=status,
                    order_date=order_dt,
                    total_value=total_value,
                    material_id=sku.material_id,
                )
            )
    return orders


def _build_requisitions(inventory: list[InventoryItem]) -> list[PurchaseRequisition]:
    sku_lookup = {(item.tenant_id, item.material_id): float(item.unit_price) for item in inventory}
    base_time = datetime(2026, 11, 15, 9, 0, 0, tzinfo=timezone.utc)

    specs: list[tuple[str, str, str, int, str, str, str | None, datetime | None]] = [
        (
            "PR-HV-001",
            "havells_electricals",
            "HV-FAN-001",
            300,
            "u_havells_mgr_01",
            "pending_approval",
            "hv-tok-seed-001",
            None,
        ),
        (
            "PR-HV-002",
            "havells_electricals",
            "HV-FAN-002",
            250,
            "u_havells_mgr_01",
            "pending_approval",
            "hv-tok-seed-002",
            None,
        ),
        (
            "PR-HV-003",
            "havells_electricals",
            "HV-FAN-003",
            150,
            "u_havells_mgr_01",
            "approved",
            "hv-tok-seed-003",
            base_time + timedelta(hours=3),
        ),
        (
            "PR-LC-001",
            "licious_coldchain",
            "LC-CHICK-001",
            400,
            "u_licious_mgr_01",
            "pending_approval",
            "lc-tok-seed-001",
            None,
        ),
        (
            "PR-LC-002",
            "licious_coldchain",
            "LC-CHICK-002",
            350,
            "u_licious_mgr_01",
            "pending_approval",
            "lc-tok-seed-002",
            None,
        ),
        (
            "PR-LC-003",
            "licious_coldchain",
            "LC-CHICK-003",
            200,
            "u_licious_mgr_01",
            "approved",
            "lc-tok-seed-003",
            base_time + timedelta(hours=4),
        ),
    ]

    reqs: list[PurchaseRequisition] = []
    for idx, (req_id, tenant_id, material_id, qty, req_by, status, tok, app_at) in enumerate(specs):
        unit_price = sku_lookup[(tenant_id, material_id)]
        reqs.append(
            PurchaseRequisition(
                requisition_id=req_id,
                tenant_id=tenant_id,
                material_id=material_id,
                quantity=qty,
                unit_price=unit_price,
                total_value=round(unit_price * qty, 2),
                requested_by=req_by,
                status=status,
                approval_token=tok,
                idempotency_key=f"seed-idem-{req_id.lower()}",
                trace_id=f"seed-trace-{req_id.lower()}",
                created_at=base_time + timedelta(minutes=idx * 15),
                approved_at=app_at,
            )
        )
    return reqs


def _build_ops_tickets() -> list[OpsTicket]:
    base_time = datetime(2026, 11, 16, 8, 30, 0, tzinfo=timezone.utc)
    return [
        OpsTicket(
            ticket_id="TKT-HV-001",
            tenant_id="havells_electricals",
            title="Low BLDC fan buffer stock at Bengaluru hub",
            severity="high",
            related_id="HV-FAN-001",
            status="open",
            idempotency_key="seed-tkt-hv-001",
            created_at=base_time,
        ),
        OpsTicket(
            ticket_id="TKT-LC-001",
            tenant_id="licious_coldchain",
            title="Cold-room compressor check scheduled for Mumbai hub",
            severity="critical",
            related_id="LC-CHICK-002",
            status="open",
            idempotency_key="seed-tkt-lc-001",
            created_at=base_time + timedelta(minutes=10),
        ),
    ]


def _build_genesis_audit() -> AuditLog:
    args_json = json.dumps(
        {"seed": 42, "tenants": ["havells_electricals", "licious_coldchain"]},
        sort_keys=True,
        separators=(",", ":"),
    )
    timestamp_iso = datetime(2026, 11, 17, 0, 0, 0, tzinfo=timezone.utc).isoformat()
    result_summary = "Seeded 500 distributors, 200 SKUs, 5200 sales orders, 6 requisitions, 2 ops tickets"
    entry_hash = compute_entry_hash(
        prev_hash=GENESIS_HASH,
        tenant_id="system",
        trace_id="seed-000",
        actor_id="system:seed",
        tool_name="seed_database",
        arguments_json=args_json,
        outcome="SUCCESS",
        result_summary=result_summary,
        timestamp_iso=timestamp_iso,
    )
    return AuditLog(
        tenant_id="system",
        trace_id="seed-000",
        actor_id="system:seed",
        tool_name="seed_database",
        arguments=args_json,
        outcome="SUCCESS",
        result=result_summary,
        timestamp_iso=timestamp_iso,
        prev_hash=GENESIS_HASH,
        entry_hash=entry_hash,
    )


def verify_seed_invariants() -> dict[str, Any]:
    """Assert seeded dataset counts, low-stock ratios, audit integrity, and tenant isolation."""
    with erp_db.SessionLocal() as db:
        dist_count = db.query(Distributor).count()
        hv_dist = db.query(Distributor).filter(Distributor.tenant_id == "havells_electricals").count()
        lc_dist = db.query(Distributor).filter(Distributor.tenant_id == "licious_coldchain").count()

        sku_count = db.query(InventoryItem).count()
        hv_sku = db.query(InventoryItem).filter(InventoryItem.tenant_id == "havells_electricals").count()
        lc_sku = db.query(InventoryItem).filter(InventoryItem.tenant_id == "licious_coldchain").count()

        hv_low = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.tenant_id == "havells_electricals",
                InventoryItem.quantity_on_hand < InventoryItem.reorder_point,
            )
            .count()
        )
        lc_low = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.tenant_id == "licious_coldchain",
                InventoryItem.quantity_on_hand < InventoryItem.reorder_point,
            )
            .count()
        )

        order_count = db.query(SalesOrder).count()
        hv_orders = db.query(SalesOrder).filter(SalesOrder.tenant_id == "havells_electricals").count()
        lc_orders = db.query(SalesOrder).filter(SalesOrder.tenant_id == "licious_coldchain").count()

        req_count = db.query(PurchaseRequisition).count()
        pending_reqs = (
            db.query(PurchaseRequisition)
            .filter(PurchaseRequisition.status == "pending_approval")
            .count()
        )
        approved_reqs = (
            db.query(PurchaseRequisition).filter(PurchaseRequisition.status == "approved").count()
        )
        ticket_count = db.query(OpsTicket).count()

        # Cross-tenant linkage invariant: every sales order's distributor & material belong to same tenant
        dist_rows = db.query(Distributor.distributor_id, Distributor.tenant_id).all()
        dist_tenant_map = {d.distributor_id: d.tenant_id for d in dist_rows}
        sku_rows = db.query(InventoryItem.tenant_id, InventoryItem.material_id).all()
        sku_tenant_map = {(i.tenant_id, i.material_id) for i in sku_rows}
        order_rows = db.query(
            SalesOrder.tenant_id, SalesOrder.distributor_id, SalesOrder.material_id
        ).all()
        for so in order_rows:
            if dist_tenant_map.get(so.distributor_id) != so.tenant_id:
                raise AssertionError(f"Cross-tenant distributor leak on order for {so.distributor_id}")
            if (so.tenant_id, so.material_id) not in sku_tenant_map:
                raise AssertionError(f"Cross-tenant material leak on order for {so.material_id}")

    audit_check = verify_all_audit_entries()

    assert dist_count == 500 and hv_dist == 250 and lc_dist == 250, (
        f"Unexpected distributor counts: {dist_count}"
    )
    assert sku_count == 200 and hv_sku == 100 and lc_sku == 100, f"Unexpected SKU counts: {sku_count}"
    assert hv_low == 25 and lc_low == 25, f"Unexpected low stock counts: hv={hv_low}, lc={lc_low}"
    assert order_count >= 5000 and hv_orders >= 2500 and lc_orders >= 2500, (
        f"Unexpected order counts: {order_count}"
    )
    assert req_count == 6 and pending_reqs == 4 and approved_reqs == 2, (
        f"Unexpected requisition counts: {req_count}"
    )
    assert ticket_count == 2, f"Unexpected ops ticket count: {ticket_count}"
    assert audit_check.valid, f"Audit hash chain verification failed: {audit_check.reason}"

    return {
        "distributors": dist_count,
        "skus": sku_count,
        "low_stock_skus": {"havells_electricals": hv_low, "licious_coldchain": lc_low},
        "sales_orders": order_count,
        "purchase_requisitions": req_count,
        "ops_tickets": ticket_count,
        "audit_chain_valid": audit_check.valid,
        "audit_entries": audit_check.total_entries,
    }


def seed_database(reset: bool = True) -> dict[str, Any]:
    """Deterministically seed the multi-tenant ERP and Ops database."""
    if reset:
        erp_db.Base.metadata.drop_all(bind=erp_db.engine)
    erp_db.init_db(erp_db.engine)

    with erp_db.SessionLocal() as db:
        if not reset and db.query(Distributor).count() > 0:
            return verify_seed_invariants()

        rng = random.Random(42)
        distributors = _build_distributors(rng)
        inventory = _build_inventory(rng)
        sales_orders = _build_sales_orders(rng, distributors, inventory)
        requisitions = _build_requisitions(inventory)
        tickets = _build_ops_tickets()
        genesis_audit = _build_genesis_audit()

        db.add_all(distributors)
        db.add_all(inventory)
        db.flush()
        db.add_all(sales_orders)
        db.add_all(requisitions)
        db.add_all(tickets)
        db.add(genesis_audit)
        db.commit()

    return verify_seed_invariants()


if __name__ == "__main__":
    summary = seed_database(reset=True)
    print(json.dumps(summary, indent=2))
