"""Carrier-neutral view of what is physically inside a parcel.

Every carrier that crosses a customs border wants the same facts about the
parcel's contents — description, quantity, unit, net weight, value, currency,
tariff number, country of origin — and each wants them under different key
names in a different envelope. This module answers the *what*; the per-carrier
payload builders do the *shape*.

It exists so Austrian Post and FedEx read the same rows from the same place:
the scanned ``Parcel Shipment Item`` rows when the operator picked items at the
station (so a split delivery declares per parcel what is actually in THAT
parcel), and the whole Delivery Note when no scan happened.

Value comes from the originating ``Delivery Note Item``: ``net_rate`` — the
value excluding tax is the customs basis — falling back to ``rate``. Tariff
number and country of origin come from the Item master.

Service items are excluded, with one configured exception -- see
``packable_non_stock_groups`` -- for services whose result is a physical thing
(a repaired shoe). A Carrier Service's ``shipping_item`` (and, where
configured, its customs-charge item) is a real Delivery Note line, so it would
otherwise be declared to customs as if it were a physical good: the shipping fee
inflates the declared value, and a pre-charged import-VAT line would be declared
as goods value and taxed again at the border.
"""

from __future__ import annotations

from typing import Any

import frappe
from frappe.utils import flt

from .bundles import components_by_line, expand_lines


def service_item_codes() -> set[str]:
    """Item codes that represent a charge, not shippable goods.

    Both live on Carrier Service: ``shipping_item`` for the freight fee and
    ``customs_item`` for pre-collected import duty and tax. Reading them from
    the master rather than a flag on the Item means a newly configured service
    is excluded the moment it is saved, with no second box to remember.
    """
    codes: set[str] = set()
    for fieldname in ("shipping_item", "customs_item"):
        codes.update(
            code for code in frappe.get_all("Carrier Service", pluck=fieldname) if code
        )

    return codes


def line_overrides(delivery_note, rows=None) -> dict[str, dict]:
    """Per Delivery Note Item name: what another app says is really in the box.

    Hook ``parcel_station_line_overrides``: each function gets the Delivery
    Note name (and the rows at hand) and returns ``{dn_item_name: {...}}`` with
    any of ``item_code``, ``item_name``, ``description``, ``weight_per_unit``.
    The case it exists for: a repair. The line is a service, the parcel holds
    the customer's shoe -- and the shoe is the Item whose tariff number and
    origin go on the customs form, while the value stays the line's.
    """
    if not delivery_note:
        return {}
    merged: dict[str, dict] = {}
    for path in frappe.get_hooks("parcel_station_line_overrides") or []:
        try:
            result = frappe.get_attr(path)(delivery_note, rows) or {}
        except Exception:
            frappe.logger().warning(
                f"[contents] line override hook {path} failed; showing lines as they are",
                exc_info=True,
            )
            continue
        for key, values in result.items():
            if key and values:
                merged.setdefault(key, {}).update(
                    {k: v for k, v in values.items() if v is not None}
                )
    return merged


def apply_line_overrides(rows, overrides, key="delivery_note_item", replace_weight=False):
    """Write ``overrides`` into dict rows in place (matched on ``row[key]``)."""
    if not overrides:
        return rows
    for row in rows:
        values = overrides.get(row.get(key))
        if not values:
            continue
        for field, value in values.items():
            if field == "weight_per_unit" and not replace_weight and flt(row.get("weight_per_unit")):
                continue
            row[field] = value
    return rows


def packable_non_stock_groups() -> set[str]:
    """Item Groups whose non-stock Items still go in a box.

    The rule "non-stock means charge" has one honest exception: a repair. The
    order line is a service -- "neu besohlen" -- but the station ships the
    repaired shoe, so the line must appear in the packing list. Which groups
    are like that is configuration (Parcel Station Settings), not a flag on the
    Item: a new repair service dropped into the group is packable at once.
    Sub-groups count.
    """
    try:
        settings = frappe.get_cached_doc("Parcel Station Settings")
        rows = settings.get("packable_item_groups") or []
    except Exception:
        return set()
    groups = {row.get("item_group") for row in rows if row.get("item_group")}
    if not groups:
        return set()
    from frappe.utils.nestedset import get_descendants_of

    expanded = set(groups)
    for group in groups:
        try:
            expanded.update(get_descendants_of("Item Group", group))
        except Exception:
            # An unknown group is a config slip, not a reason to hide goods.
            continue
    return expanded


def packable_item_codes(item_codes) -> set[str]:
    """Of the given item codes, the ones that can physically go in a parcel.

    A parcel station packs goods. Everything else on a Delivery Note — the
    freight fee, pre-collected import duty, any other service line — is money,
    not cargo: there is nothing to scan, nothing to weigh and nothing to hand to
    the courier, so offering it in the packing list only invites an operator to
    tick it.

    The test is ``Item.is_stock_item``, deliberately, rather than "is it
    registered as some Carrier Service's charge item". That named only the
    charge items we happened to know about — the freight fee was filtered while
    the import-charge line was not — and it would miss the next service item
    somebody adds. Stock/non-stock is the property that actually decides whether
    a thing can be shipped.

    Unknown codes are treated as packable: a missing Item master is a data
    problem, and quietly dropping a line from the packing list would hide it.
    """
    codes = {code for code in (item_codes or []) if code}
    if not codes:
        return set()

    known = frappe.get_all(
        "Item",
        filters={"name": ["in", list(codes)]},
        fields=["name", "is_stock_item", "item_group"],
    )
    goods_groups = packable_non_stock_groups()
    services = {
        row["name"]
        for row in known
        if not row["is_stock_item"] and row.get("item_group") not in goods_groups
    }
    return codes - services


def _component_net_rates(rows: list[dict]) -> dict[str, float]:
    """``net_rate`` per Packed Item row for the scanned bundle components in ``rows``.

    Looks the bundle lines up per Delivery Note, once, and only when a row
    actually references a Packed Item — the common bundle-free parcel costs
    nothing here.
    """
    by_note: dict[str, set[str]] = {}
    for row in rows:
        packed = row.get("packed_item")
        if packed and not row.get("net_rate") and row.get("delivery_note"):
            by_note.setdefault(row["delivery_note"], set()).add(packed)
    if not by_note:
        return {}

    rates: dict[str, float] = {}
    for note, wanted in by_note.items():
        lines = frappe.get_all(
            "Delivery Note Item",
            filters={"parent": note},
            fields=["name", "item_code", "item_name", "net_amount", "amount", "base_amount"],
        )
        for comps in components_by_line(note, lines).values():
            for comp in comps:
                if comp["name"] in wanted:
                    rates[comp["name"]] = flt(comp.get("net_rate"))
    return rates


def _first_delivery_note(sh: dict) -> str | None:
    for row in (sh.get("shipment_delivery_note") or []):
        get = row.get if isinstance(row, dict) else (lambda k: getattr(row, k, None))
        dn = get("delivery_note")
        if dn:
            return dn
    return None


def collect_parcel_contents(sh: dict) -> list[dict[str, Any]]:
    """Return one neutral dict per position actually inside this parcel.

    Keys: ``item_code``, ``item_name``, ``qty``, ``uom``, ``weight_per_unit``,
    ``rate``, ``currency``, ``tariff``, ``origin_country``. Values that are
    unavailable are ``None`` (or 0.0 for numerics) rather than absent, so
    callers can decide per carrier whether to omit or default them.

    Returns an empty list when the shipment has neither scanned rows nor a
    Delivery Note — callers must treat that as "declare nothing", not as an
    error, exactly as the Austrian Post payload always has.
    """
    shipment_name = sh.get("name")
    delivery_note = _first_delivery_note(sh)
    rows: list[dict] = []

    if shipment_name and frappe.db.table_exists("Parcel Shipment Item"):
        packed_col = (
            "psi.packed_item"
            if frappe.db.has_column("Parcel Shipment Item", "packed_item")
            else "NULL as packed_item"
        )
        rows = frappe.db.sql(
            f"""
            select psi.item_code, psi.item_name, psi.qty, psi.uom,
                   psi.weight_per_unit, psi.delivery_note, psi.delivery_note_item,
                   {packed_col}
            from `tabParcel Shipment Item` psi
            where psi.shipment = %s
            order by psi.idx
            """,
            shipment_name,
            as_dict=True,
        )

    if not rows and delivery_note:
        # No scan happened (auto flow without item selection) — declare the whole
        # Delivery Note instead of sending nothing at all.
        rows = frappe.db.sql(
            """
            select dni.name, dni.item_code, dni.item_name, dni.qty, dni.uom,
                   dni.weight_per_unit, dni.parent as delivery_note,
                   dni.name as delivery_note_item,
                   dni.net_amount, dni.amount, dni.base_amount
            from `tabDelivery Note Item` dni
            where dni.parent = %s
            order by dni.idx
            """,
            delivery_note,
            as_dict=True,
        )
        # A Product Bundle line is not cargo; its packed components are. Each
        # component carries its share of the line's net value as ``net_rate``.
        rows = expand_lines(rows, components_by_line(delivery_note, rows))
        # A repair line declares the shoe, not the service (hook).
        apply_line_overrides(rows, line_overrides(delivery_note, rows))

    if not rows:
        return []

    # Components scanned at the station were stored with their Packed Item
    # row; their value is the bundle line's, split over the components.
    component_rates = _component_net_rates(rows)

    currency = (
        frappe.db.get_value("Delivery Note", delivery_note, "currency")
        if delivery_note
        else None
    )

    service_items = service_item_codes()

    contents: list[dict[str, Any]] = []
    for row in rows:
        item_code = row.get("item_code")
        if item_code and item_code in service_items:
            # Shipping fee / import-charge line: a charge, not cargo.
            continue

        rate = None
        if row.get("packed_item"):
            rate = flt(row.get("net_rate")) or flt(component_rates.get(row["packed_item"]))
        elif row.get("delivery_note_item"):
            values = frappe.db.get_value(
                "Delivery Note Item",
                row["delivery_note_item"],
                ["net_rate", "rate"],
                as_dict=True,
            )
            if values:
                rate = flt(values.get("net_rate")) or flt(values.get("rate"))

        tariff = origin = None
        if item_code:
            master = frappe.db.get_value(
                "Item", item_code, ["customs_tariff_number", "country_of_origin"], as_dict=True
            )
            if master:
                tariff = (master.get("customs_tariff_number") or "").strip() or None
                origin = master.get("country_of_origin") or None

        contents.append(
            {
                "item_code": item_code,
                "item_name": row.get("item_name"),
                "qty": flt(row.get("qty")),
                "uom": row.get("uom"),
                "weight_per_unit": flt(row.get("weight_per_unit")),
                "rate": flt(rate) if rate else 0.0,
                "currency": currency,
                "tariff": tariff,
                "origin_country": origin,
            }
        )

    return contents
