#!/usr/bin/env python3
# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Regenerate ``parcel/tracking/post_event_codes.py`` from the Post's own list.

Source: ``tests/fixtures/austrian_post_event_reason_list.xlsx`` — the
"Stammdaten: Event-/Reason-Liste (=Statusliste)" that the format spec
("Trackingdaten an Kunden" V2.1, section 1) points at, downloaded from
secure.post.at/downloads/EventReasonList.xlsx.

Run after refreshing that download:

    python3 scripts/generate_post_event_codes.py

Two things come out of the list:

* the German wording for every (type, reason) pair, which turns an event row in
  the Desk from "ZUH/BN" into "Sendung in Zustellung";
* a status class for every pair whose wording names the event outcome —
  delivered, ready for pickup, out for delivery, returned, problem.

The classes exist because ``ShipmentState`` describes the parcel at
FILE-CREATION time, not at the event's own timestamp: a morning "auf
Zustelltour" scan arrives in the noon file carrying state ZU once the parcel
was delivered in between, and status-by-state would pin a green "Delivered"
pill next to the words "Sendung in Zustellung". Where the wording is
unambiguous, the event row now shows its own meaning; the state remains the
fallback for everything a text does not settle (transport, distribution,
customs, delays), because there it genuinely is the better signal.

Only stdlib: the xlsx is read as the zip of XML it is, so no build-time
dependency is added for a file that changes once a year.
"""

from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET
import zipfile

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "tests" / "fixtures" / "austrian_post_event_reason_list.xlsx"
TARGET = ROOT / "erpnext_parcel_station" / "parcel" / "tracking" / "post_event_codes.py"

#: Ordered; first match on the lower-cased German wording wins, so the order
#: carries meaning:
#:
#: * returns before problems, so "Retour - Annahme verweigert" counts as a
#:   return rather than a refusal;
#: * the ``None`` rule shields notification texts — "Zweite Benachrichtigung
#:   zugestellt" is about the NOTICE, and the "zugestellt" needle of the
#:   delivered class would otherwise mark the parcel delivered;
#: * "wird nochmals zugestellt" and "in Zustellung" before "zugestellt" for
#:   the same substring reason.
#:
#: Only unambiguous wordings get a class at all. Everything else (transport,
#: distribution, customs, delays, "kassiert", "benachrichtigt") stays
#: unclassified and defers to ShipmentState — the state knows where the
#: parcel is; these texts don't say more than that.
RULES: tuple[tuple[str | None, tuple[str, ...]], ...] = (
    (
        "STATUS_RETURNED",
        ("retour", "rücksendung", "rückgesendet", "rückgabe der sendung an den absender"),
    ),
    (
        "STATUS_PROBLEM",
        (
            "verweiger", "beschädig", "zurückgehalten", "konnte nicht zugestellt",
            "empfänger unbekannt", "ungenügend", "anschriftsproblem", "einfuhrverbot",
            "verbotenem inhalt", "nicht verladen", "unterfrankierung",
            "erfolgloser zustellversuch", "abholhindernis", "sonstiges zollstellungshindernis",
        ),
    ),
    (None, ("benachrichtigung",)),
    (
        "STATUS_READY_FOR_PICKUP",
        ("abholbereit", "hinterlegt", "empfangsbox eingelegt", "abholwand eingelegt"),
    ),
    (
        "STATUS_OUT_FOR_DELIVERY",
        ("in zustellung", "im zustellprozess", "nochmals zugestellt"),
    ),
    (
        "STATUS_DELIVERED",
        (
            "zugestellt", "ausgefolgt", "eingeworfen", "ersatzempfänger übergeben",
            "empfangsbox entnommen", "abholwand entnommen",
        ),
    ),
)


def _classify(german: str) -> str | None:
    text = german.lower()
    for status, needles in RULES:
        if any(needle in text for needle in needles):
            return status  # a None status means: deliberately unclassified
    return None


def _read_rows(archive: zipfile.ZipFile) -> dict[tuple[str, str], str]:
    # The workbook ships both a shared-string table and inline strings, and the
    # sheets actually use the inline form — reading only <v> yields nothing.
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        shared = [
            "".join(node.text or "" for node in si.iter(NS + "t"))
            for si in ET.fromstring(archive.read("xl/sharedStrings.xml"))
        ]

    rows: dict[tuple[str, str], str] = {}
    for name in archive.namelist():
        if not name.startswith("xl/worksheets/sheet"):
            continue
        for row in ET.fromstring(archive.read(name)).iter(NS + "row"):
            cells: dict[str, str] = {}
            for cell in row.iter(NS + "c"):
                column = "".join(ch for ch in cell.get("r", "") if ch.isalpha())
                value = cell.find(NS + "v")
                inline = cell.find(NS + "is")
                text = ""
                if value is not None:
                    text = shared[int(value.text)] if cell.get("t") == "s" else (value.text or "")
                elif inline is not None:
                    text = "".join(node.text or "" for node in inline.iter(NS + "t"))
                if text.strip():
                    cells[column] = text.strip()
            # A = Reason Code, B = Type Code, C = Event Type Name, D = Name in Deutsch
            reason, type_code = cells.get("A", ""), cells.get("B", "")
            german = cells.get("D") or cells.get("C") or ""
            if reason and type_code and reason != "Reason Code" and german:
                rows[(type_code, reason)] = german
    return rows


def main() -> None:
    with zipfile.ZipFile(SOURCE) as archive:
        rows = _read_rows(archive)

    overrides = {key: _classify(text) for key, text in rows.items()}
    overrides = {key: status for key, status in overrides.items() if status}

    lines = [
        "# Copyright (c) 2026, Devich Daniel and Contributors",
        "# See license.txt",
        '"""GENERATED — do not edit by hand.',
        "",
        "Regenerate with ``python3 scripts/generate_post_event_codes.py`` after",
        "refreshing ``tests/fixtures/austrian_post_event_reason_list.xlsx`` from",
        "secure.post.at/downloads/EventReasonList.xlsx. The generator carries the",
        "classification rules and the reasoning behind them.",
        '"""',
        "",
        "from .status_mapping import (",
        "    STATUS_DELIVERED,",
        "    STATUS_OUT_FOR_DELIVERY,",
        "    STATUS_PROBLEM,",
        "    STATUS_READY_FOR_PICKUP,",
        "    STATUS_RETURNED,",
        ")",
        "",
        "#: (ParcelEventTypeCode, ParcelEventReasonCode) -> the Post's own German",
        "#: wording. Used as the event description, so a row reads as a sentence",
        "#: rather than as two three-letter codes.",
        f"EVENT_TEXTS: dict[tuple[str, str], str] = {{  # {len(rows)} pairs",
    ]
    for (type_code, reason), text in sorted(rows.items()):
        lines.append(f'    ("{type_code}", "{reason}"): {text!r},')
    lines += [
        "}",
        "",
        "#: The pairs whose official wording names the event outcome — delivered,",
        "#: ready for pickup, out for delivery, returned, problem. For these the",
        "#: event row shows its own meaning; ShipmentState (the parcel's state at",
        "#: FILE-CREATION time) stays the fallback for everything unclassified.",
        f"EVENT_OVERRIDES: dict[tuple[str, str], str] = {{  # {len(overrides)} pairs",
    ]
    for (type_code, reason), status in sorted(overrides.items()):
        lines.append(f'    ("{type_code}", "{reason}"): {status},')
    lines += ["}", ""]

    TARGET.write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)}: {len(rows)} texts, {len(overrides)} overrides")


if __name__ == "__main__":
    main()
