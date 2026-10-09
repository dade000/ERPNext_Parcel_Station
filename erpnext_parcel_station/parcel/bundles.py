"""Product Bundles on a Delivery Note, as the parcel station sees them.

ERPNext models a Product Bundle as a *non-stock* parent Item: the Delivery Note
line carries the bundle (``SOCKEN-5ER-BUNDLE x1``, priced as one), while the
goods that actually leave the shelf — five pairs of socks — live in the
``Packed Item`` child table, each row pointing back at its bundle line through
``parent_detail_docname``.

The station packs goods, so it has to look at the packed rows, not the bundle
line. The bundle line itself fails the ``is_stock_item`` test that hides charge
lines (freight fee, pre-collected import duty) — which is right, there is no
"bundle" to put in a box — but without this module that left a bundle-only
Delivery Note with an empty packing list and nothing to ship.

What the components inherit from their bundle line:

* **Value.** A Packed Item carries no price; the customer paid for the bundle.
  The line's amount is split over its components, weighted by the packed
  ``rate`` when ERPNext filled one in and by quantity otherwise, so a customs
  declaration lists five pairs at a fifth of the bundle price rather than one
  bundle line at full price plus five items at zero.
* **Weight.** From the component's Item master. The bundle Item is not a
  physical thing and has no weight of its own.
* **Quantity accounting.** Shipped quantities are tracked per Packed Item row,
  so a split delivery can send three pairs today and two tomorrow.
"""

from __future__ import annotations

from typing import Any, Iterable

import frappe
from frappe.utils import flt


def _get(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def packed_rows(delivery_note: str) -> list[dict[str, Any]]:
    """Every Packed Item row of a Delivery Note, in table order."""
    if not delivery_note:
        return []
    return frappe.get_all(
        "Packed Item",
        filters={"parent": delivery_note, "parenttype": "Delivery Note"},
        fields=[
            "name", "parent_detail_docname", "parent_item", "item_code", "item_name",
            "description", "qty", "uom", "rate",
        ],
        order_by="idx asc",
    )


def split_line_value(line_amount: float, components: Iterable[dict[str, Any]]) -> dict[str, float]:
    """Per-unit value of each component, so that the components sum to the line.

    Weighted by ``rate * qty`` when the packed rows carry a rate — ERPNext fills
    it from the component's own price list entry when there is one — and by
    quantity alone otherwise. Returns ``{packed_row_name: per_unit_value}``.
    """
    rows = list(components)
    if not rows:
        return {}

    by_value = {
        _get(r, "name"): flt(_get(r, "rate")) * flt(_get(r, "qty")) for r in rows
    }
    weights = by_value if any(w > 0 for w in by_value.values()) else {
        _get(r, "name"): flt(_get(r, "qty")) for r in rows
    }
    total = sum(weights.values())
    if total <= 0:
        return {_get(r, "name"): 0.0 for r in rows}

    out: dict[str, float] = {}
    for r in rows:
        name = _get(r, "name")
        qty = flt(_get(r, "qty"))
        share = flt(line_amount) * weights[name] / total
        out[name] = share / qty if qty else 0.0
    return out


def components_by_line(delivery_note: str, dn_items: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """Bundle components grouped by the Delivery Note Item they belong to.

    ``dn_items`` are the note's item rows (documents or dicts); a line appears
    in the result only when Packed Item rows point at it. Each component dict is
    shaped like a packing-list entry: ``name`` is the Packed Item row (unique,
    the key shipped quantities are tracked under), ``delivery_note_item`` the
    bundle line, ``rate`` its share of the line's amount, ``net_rate`` its share
    of the net amount (the customs basis), ``weight_per_unit`` from the Item
    master.
    """
    packed = packed_rows(delivery_note)
    if not packed:
        return {}

    lines = {_get(row, "name"): row for row in dn_items}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for p in packed:
        grouped.setdefault(p.get("parent_detail_docname"), []).append(p)
    grouped.pop(None, None)

    codes = {p.get("item_code") for p in packed if p.get("item_code")}
    weights: dict[str, float] = {}
    if codes:
        for row in frappe.get_all(
            "Item", filters={"name": ["in", list(codes)]}, fields=["name", "weight_per_unit"]
        ):
            weights[row["name"]] = flt(row.get("weight_per_unit"))

    result: dict[str, list[dict[str, Any]]] = {}
    for line_name, comps in grouped.items():
        line = lines.get(line_name)
        if line is None:
            # Packed rows whose bundle line is not on the note we were given:
            # nothing to inherit from, so nothing to offer.
            continue

        gross = flt(_get(line, "base_amount")) or flt(_get(line, "amount"))
        net = flt(_get(line, "net_amount")) or flt(_get(line, "amount"))
        gross_rates = split_line_value(gross, comps)
        net_rates = split_line_value(net, comps)

        result[line_name] = [
            {
                "name": p["name"],
                "packed_item": p["name"],
                "delivery_note_item": line_name,
                "item_code": p.get("item_code"),
                "item_name": p.get("item_name") or p.get("item_code"),
                "description": p.get("description") or "",
                "qty": flt(p.get("qty")),
                "uom": p.get("uom"),
                "rate": gross_rates.get(p["name"], 0.0),
                "net_rate": net_rates.get(p["name"], 0.0),
                "weight_per_unit": weights.get(p.get("item_code"), 0.0),
                "bundle_item_code": _get(line, "item_code"),
                "bundle_item_name": _get(line, "item_name") or _get(line, "item_code"),
            }
            for p in comps
        ]
    return result


def expand_lines(dn_items: Iterable[Any], components: dict[str, list[dict[str, Any]]]) -> list[Any]:
    """The note's rows with every bundle line replaced by its components.

    Non-bundle rows pass through untouched (whatever type they came in as), so
    callers that read ``item_code``/``qty``/``weight_per_unit`` off a row keep
    working; component rows are the dicts from :func:`components_by_line`.
    """
    out: list[Any] = []
    for row in dn_items:
        comps = components.get(_get(row, "name"))
        if comps:
            out.extend(comps)
        else:
            out.append(row)
    return out


def component_qty_for_bundles(component: dict[str, Any], line_qty: float, bundles: float) -> float:
    """How many of a component go out when ``bundles`` of its line are packed.

    A Packed Item's ``qty`` is the total for the whole line (5 pairs for one
    bundle, 10 for two), so the per-bundle count is ``qty / line_qty``.
    """
    per_bundle = flt(component.get("qty")) / (flt(line_qty) or 1)
    return per_bundle * flt(bundles)
