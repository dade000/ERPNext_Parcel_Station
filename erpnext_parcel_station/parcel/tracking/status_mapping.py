# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
"""Carrier status codes -> canonical tracking status.

Pure functions only — no frappe imports — so the mapping tables can be unit
tested and reasoned about without a site.

The canonical vocabulary is the (extended) option list of the core
``Shipment.tracking_status`` Select field, see
``patches/v15/extend_shipment_tracking_status_options.py``:

    In Progress, Announced, In Transit, Out for Delivery,
    Delivered, Problem, Returned, Lost

Design rule: **unknown codes degrade, they never crash.** Every carrier keeps
inventing event codes; an unmapped one falls back to the coarsest signal the
carrier gives us (Austrian Post: ``ShipmentState``; FedEx/GLS: ``IN_TRANSIT``)
so the worst outcome of a new code is a less specific status, not a failed
import. Callers that want to notice new codes can compare
``map_*`` results against :data:`STATUS_IN_TRANSIT` and log.
"""

from __future__ import annotations

# Canonical statuses (values of the Shipment.tracking_status Select).
STATUS_IN_PROGRESS = "In Progress"
STATUS_ANNOUNCED = "Announced"
STATUS_IN_TRANSIT = "In Transit"
STATUS_OUT_FOR_DELIVERY = "Out for Delivery"
#: Deposited for collection: Post office / Abholstation, GLS ParcelShop,
#: FedEx Hold at Location. NOT terminal — the carrier reports a final
#: Delivered when the customer collects, and a parcel sitting uncollected
#: long enough ages into the digest's stuck section (carriers return it
#: after their deadline, which is exactly when ops wants to know).
STATUS_READY_FOR_PICKUP = "Ready for Pickup"
STATUS_DELIVERED = "Delivered"
STATUS_PROBLEM = "Problem"
STATUS_RETURNED = "Returned"
STATUS_LOST = "Lost"

#: Statuses a shipment never leaves again. They stop the FedEx/GLS polling and
#: are sticky in ``events.update_shipment_status`` (a late-arriving transit
#: event must not "un-deliver" a parcel).
TERMINAL_STATUSES = frozenset({STATUS_DELIVERED, STATUS_RETURNED, STATUS_LOST})

#: Statuses the daily digest reports as needing attention.
PROBLEM_STATUSES = frozenset({STATUS_PROBLEM, STATUS_RETURNED, STATUS_LOST})


# ---------------------------------------------------------------------------
# Austrian Post (POSTTRACK XML, TrackingEvent_V2.0.0)
# ---------------------------------------------------------------------------

#: ``ShipmentState`` is the authoritative field, confirmed against nine real
#: POSTTRACK files: the same (type, reason) pair carries different states —
#: AZT/AT appears as both IZ and ZU, VER/VT as both IV and IZ, ZUH/BN as both
#: IZ and ZU — so the code pair cannot be the signal and the state must be.
#:
#: Observed states and the event codes they arrive with:
#:   AV  avisiert                    AVI/SE
#:   IV  in Verkehr                  BEI/EZ, PUG/UT, IMP/IB, VER/*, TRA/*,
#:                                   CUS/*, CUD/CQ, EXP/*
#:   IZ  in Zustellung               AZT/AT, ZUH/BN, ZUH/TV
#:   EB  hinterlegt, abholbereit     HIA/HO — the only event carrying a
#:                                   BranchKey (the pickup post office), which
#:                                   the Post's webservice spec confirms is
#:                                   filled for exactly BEI-EAW, HIA-HO, CLA-CAF
#:   ZU  zugestellt                  ZUS/ZU, ZUS/ZA, ZUS/ZP, ZUS/ZB, ZUH/HS
#:   RU  Zustellung gescheitert      ZUH/SH, ZUH/FL — a retained parcel, not a
#:                                   return: both observed cases were delivered
#:                                   within a day, so this stays In Transit and
#:                                   the digest's stuck check catches the ones
#:                                   that do not resolve
#:   AN  Annahme des Retour-Pakets   ANA/AO — on the RETURN parcel, whose own
#:                                   IdentCode differs from ours, so it arrives
#:                                   as an orphan rather than on the shipment
#:
#: NOTE: the state describes the parcel at FILE CREATION, not at the event's
#: own timestamp — an AZT event from 08:08 carries ZU when the parcel was
#: delivered at 11:52 and the file was cut at 12:04. That is why the state is
#: only the FALLBACK for an event's canonical status: pairs whose official
#: wording names the outcome (post_event_codes.EVENT_OVERRIDES) show their own
#: meaning, so a "Sendung in Zustellung" row can never wear a Delivered pill.
#: The Shipment's status still ends up right either way, because the newest
#: event decides and the delivering scan travels in the same file.
_POST_SHIPMENT_STATES = {
    "AV": STATUS_ANNOUNCED,
    "IV": STATUS_IN_TRANSIT,
    "IZ": STATUS_OUT_FOR_DELIVERY,
    "EB": STATUS_READY_FOR_PICKUP,
    "ZU": STATUS_DELIVERED,
    "RU": STATUS_IN_TRANSIT,
    "AN": STATUS_IN_TRANSIT,
}

#: Overrides for the combinations the state genuinely cannot express, taken
#: from the ZUS reason table in the Post's own format spec ("Trackingdaten an
#: Kunden" V2.1, 17.12.2025, section 2). Both of these arrive with
#: ShipmentState ZU — the parcel *was* handed over — so without the override
#: they would read as an ordinary successful delivery:
#:
#:   ZUS/BZ  "beschädigt übergeben"      -> the customer received damage
#:   ZUS/ZR  "Zustellung Retoursendung"  -> what was delivered is the RETURN,
#:                                          i.e. the goods came back to us
#:
#: The remaining documented ZUS reasons are all ordinary deliveries and need no
#: override: ZN (an Nachbar), ZP (persönlich), ZU (sonstige), ZV (an
#: Postbevollmächtigten), ZW (an Wohnungsinhaber), ZY (an Familienmitglied).
#:
#: The full Event-/Reason list lives in a separate, non-public document
#: ("Stammdaten: Event-/Reason-Liste (=Statusliste)"), so this table grows only
#: with combinations that are documented or observed — never guessed.
_POST_EVENT_OVERRIDES: dict[tuple[str, str], str] = {
    ("ZUS", "BZ"): STATUS_PROBLEM,
    ("ZUS", "ZR"): STATUS_RETURNED,
}

#: Fallback keyed on the event type alone, used only when ShipmentState is
#: absent. The spec marks ShipmentState optional (O) while both code fields are
#: mandatory (M), so an event without a state is legal and would otherwise
#: default to In Transit — including a delivery.
_POST_TYPE_FALLBACK = {
    "AVI": STATUS_ANNOUNCED,
    "AZT": STATUS_OUT_FOR_DELIVERY,
    "HIA": STATUS_READY_FOR_PICKUP,
    "ZUS": STATUS_DELIVERED,
}


def map_post_event(shipment_state: str | None, type_code: str | None, reason_code: str | None) -> str:
    """Canonical status for one Austrian Post tracking event."""
    type_code = (type_code or "").strip().upper()
    reason_code = (reason_code or "").strip().upper()
    state = (shipment_state or "").strip().upper()

    # The generated table first: those are the pairs whose meaning the state
    # cannot carry (refusal, damage, return). Then the state, which is the
    # better answer to "where is the parcel now". Then, only when the optional
    # state is absent, the event type.
    from .post_event_codes import EVENT_OVERRIDES

    override = _POST_EVENT_OVERRIDES.get((type_code, reason_code)) or EVENT_OVERRIDES.get(
        (type_code, reason_code)
    )
    if override:
        return override
    if state:
        return _POST_SHIPMENT_STATES.get(state, STATUS_IN_TRANSIT)
    return _POST_TYPE_FALLBACK.get(type_code, STATUS_IN_TRANSIT)


def post_event_text(type_code: str | None, reason_code: str | None) -> str:
    """The Post's own German wording for a (type, reason) pair, or ""."""
    from .post_event_codes import EVENT_TEXTS

    return EVENT_TEXTS.get(
        ((type_code or "").strip().upper(), (reason_code or "").strip().upper()), ""
    )


# ---------------------------------------------------------------------------
# FedEx (Track API v1)
# ---------------------------------------------------------------------------

#: ``latestStatusDetail.code`` / ``scanEvents[].derivedStatusCode`` values.
#: Source: FedEx Track API documentation (track status codes).
_FEDEX_CODES = {
    "IN": STATUS_ANNOUNCED,  # Initiated / label created
    "OC": STATUS_ANNOUNCED,  # Order created / shipment information sent to FedEx
    "PU": STATUS_IN_TRANSIT,  # Picked up
    "AR": STATUS_IN_TRANSIT,  # Arrived at facility
    "DP": STATUS_IN_TRANSIT,  # Departed facility
    "IT": STATUS_IN_TRANSIT,  # In transit
    "AF": STATUS_IN_TRANSIT,  # At FedEx facility
    "SF": STATUS_IN_TRANSIT,  # At sort facility
    "FD": STATUS_IN_TRANSIT,  # At FedEx destination facility
    "TR": STATUS_IN_TRANSIT,  # Transfer
    "CD": STATUS_IN_TRANSIT,  # Clearance delay (still moving; digest catches stalls)
    "DY": STATUS_IN_TRANSIT,  # Delay
    "HL": STATUS_READY_FOR_PICKUP,  # Hold at Location — deposited for customer pickup
    "OD": STATUS_OUT_FOR_DELIVERY,
    "DL": STATUS_DELIVERED,
    "DE": STATUS_PROBLEM,  # Delivery exception
    "SE": STATUS_PROBLEM,  # Shipment exception
    "CA": STATUS_PROBLEM,  # Shipment cancelled
    "DD": STATUS_PROBLEM,  # Delivery delayed
    "RS": STATUS_RETURNED,  # Return to shipper
    "RP": STATUS_RETURNED,  # Return label
}


def map_fedex_code(code: str | None) -> str:
    return _FEDEX_CODES.get((code or "").strip().upper(), STATUS_IN_TRANSIT)


# ---------------------------------------------------------------------------
# GLS (ShipIT parcel details)
# ---------------------------------------------------------------------------

#: ``StatusCode`` of a ShipIT history entry, compared without spaces and
#: underscores. ShipIT knows exactly six (WS-REST-API 5.2.5, "Track your
#: parcels"; production runs backend 5.1.5 and answered with the same codes
#: on 2026-10-02):
#:
#:   DATA_RECEIVED  parcel data was sent to GLS
#:   PICKUP         first scan, the parcel enters the GLS network
#:   HUB            at a GLS hub, being sorted to its destination
#:   IN_DELIVERY    out with the driver
#:   DELIVERED      delivered at final destination
#:   CANCELLED      parcel data cancelled, GLS will not handle the parcel
#:
#: Production also sends DELIVERY_DEPOT (the parcel is at the depot that
#: delivers it), which the documentation does not list.
#:
#: There is NO code for "not delivered", "deposited at a ParcelShop" or
#: "returned": ShipIT tells those apart only in the free-text Description,
#: see ``map_gls_event``.
#: The remaining keys below are the vocabulary of GLS's public tracking
#: (PREADVICE, DELIVEREDPS, …), kept for an installation that answers in
#: those words; ShipIT itself never sends them.
#:
#: Two mistakes this table used to make: DATA_RECEIVED was unknown and fell
#: through to "In Transit" — the customer read "on its way" as soon as the
#: label was printed — and IN_DELIVERY did not match INDELIVERY, so "out for
#: delivery" never showed.
_GLS_STATUSES = {
    # GLS has the data, not the parcel.
    "DATARECEIVED": STATUS_ANNOUNCED,
    "PREADVICE": STATUS_ANNOUNCED,
    "PICKUP": STATUS_IN_TRANSIT,
    # In practice "picked up", "handed over" and "reached the parcel centre"
    # all arrive as HUB.
    "HUB": STATUS_IN_TRANSIT,
    "DELIVERYDEPOT": STATUS_IN_TRANSIT,
    "INTRANSIT": STATUS_IN_TRANSIT,
    "INWAREHOUSE": STATUS_IN_TRANSIT,
    "FORWARDING": STATUS_IN_TRANSIT,
    "INPICKUP": STATUS_IN_TRANSIT,
    "INDELIVERY": STATUS_OUT_FOR_DELIVERY,
    "DELIVERED": STATUS_DELIVERED,
    # Delivered to a ParcelShop = waiting for the customer, not delivered:
    # for ShopDelivery parcels GLS reports the final DELIVERED at handover.
    "DELIVEREDPS": STATUS_READY_FOR_PICKUP,
    "NOTDELIVERED": STATUS_PROBLEM,
    "CANCELED": STATUS_PROBLEM,
    "CANCELLED": STATUS_PROBLEM,
    "RETURNED": STATUS_RETURNED,
    "RETOURE": STATUS_RETURNED,
}


def _gls_key(status: str | None) -> str:
    return (status or "").strip().upper().replace(" ", "").replace("_", "")


def known_gls_status(status: str | None) -> str | None:
    """The canonical status for a GLS status code, or None when it is not one
    we know — so a caller can tell "In Transit" from "no idea"."""
    return _GLS_STATUSES.get(_gls_key(status))


def map_gls_status(status: str | None) -> str:
    """Canonical status of a GLS status code. A code we cannot read still
    means the parcel is somewhere at GLS: "In Transit"."""
    return known_gls_status(status) or STATUS_IN_TRANSIT


#: What a StatusCode cannot express, read from the Description instead.
#:
#: ShipIT has no code for "waiting at a ParcelShop", "delivery failed" or
#: "going back": a deposit at a ParcelShop arrives as DELIVERED, a failed
#: attempt as HUB or DELIVERY_DEPOT. The phrases come from GLS's own list of
#: descriptions per status (tests/fixtures/gls_tracktrace_mapping.json, about
#: 190 texts) and from production histories; ShipIT answers in English.
#:
#: They are fragments on purpose, so the many variants of one situation ("…
#: could not be delivered as the consignee was absent / has moved / …") need
#: no entry each. And they are narrow where it matters: "delivered at the
#: ParcelShop" is a parcel waiting for its recipient, while "Handing over the
#: parcel to the recipient at the GLS ParcelShop" is the collection — a rule
#: on the bare word "ParcelShop" would call a collected parcel waiting again.

#: Deposited somewhere the recipient has to collect it from.
_GLS_WAITING = (
    "delivered at the parcelshop",
    "available at parcelshop",
    "delivered into the parcellocker",
    "delivered at a post office",
    "awaiting consignee pick up",
    "to be picked up by the consignee",
)

#: On its way back to the sender.
_GLS_RETURNING = ("return process", "return shipment")

#: The delivery did not work or cannot proceed.
_GLS_FAILED = (
    "could not be delivered",
    "cannot be delivered",
    "cannot delivered",
    "not out for delivery",
    "where the delivery is not possible",
    "refused",
    "customs clearance is delayed",
    "deleted from",
    "could not be dropped off",
    "delivery in the parcelshop was not possible",
)

#: Reads like a failure but is the recipient's own rescheduling.
_GLS_NOT_A_FAILURE = ("new delivery date",)

#: GLS physically has the parcel, although the code still says DATA_RECEIVED.
_GLS_MOVING = (
    "handed over by the shipper",
    "loaded into the transport vehicle",
    "reached the parcel center",
)


def _has(text: str, phrases: tuple[str, ...]) -> bool:
    return any(phrase in text for phrase in phrases)


def map_gls_event(code: str | None, description: str | None = None) -> str:
    """Canonical status of one GLS history entry: its StatusCode, refined by
    the description where the code says too little."""
    text = " ".join((description or "").lower().split())
    status = known_gls_status(code)

    # A DELIVERED code is final for GLS; the only thing the text can add is
    # that "delivered" meant a shop or locker. The failure phrases are checked
    # before the waiting ones for everything else: "could not be delivered
    # into the ParcelLocker" is a failure, not a parcel waiting in a locker.
    if status != STATUS_DELIVERED:
        if _has(text, _GLS_RETURNING):
            return STATUS_RETURNED
        if _has(text, _GLS_FAILED) and not _has(text, _GLS_NOT_A_FAILURE):
            return STATUS_PROBLEM
    if _has(text, _GLS_WAITING):
        # Handed to a shop or locker, not to the customer.
        return STATUS_READY_FOR_PICKUP
    if status == STATUS_DELIVERED:
        return STATUS_DELIVERED
    if status == STATUS_ANNOUNCED and _has(text, _GLS_MOVING):
        return STATUS_IN_TRANSIT
    return status or known_gls_status(description) or STATUS_IN_TRANSIT
