# Webshop Tracking API

Contract for the shop-facing tracking endpoint. The webshop calls this
**server-side** with its ERP API key (`Authorization: token KEY:SECRET`), the
same way as its other ERP calls — never from the browser.

## Endpoint

```
GET /api/method/erpnext_parcel_station.parcel.tracking.api.get_order_tracking
    ?sales_order=SO-2026-00123
```

## Response

```json
{
  "message": {
    "sales_order": "SO-2026-00123",
    "shipments": [
      {
        "shipment": "SHIPMENT-00158",
        "carrier": "AUSTRIAN_POST",
        "tracking_number": "1019012500386270268443",
        "tracking_status": "Out for Delivery",
        "last_event_at": "2026-09-06 08:25:59",
        "events": [
          {
            "timestamp": "2026-09-06 08:25:59",
            "status": "Out for Delivery",
            "code": "AZT",
            "city": "Silz, Tirol",
            "country": "AT"
          }
        ]
      }
    ]
  }
}
```

* `shipments` is empty while no label exists yet (order not shipped).
* `events` is sorted newest first; `status` values come from the canonical
  vocabulary below — the shop renders its own (translated) texts from them.
* Unknown Sales Order → HTTP 404-style Frappe error.

## Canonical status vocabulary

| Wert | Bedeutung |
|---|---|
| `In Progress` | Label erstellt, noch keine Carrier-Events |
| `Announced` | Sendung avisiert |
| `In Transit` | Unterwegs |
| `Out for Delivery` | In Zustellung |
| `Ready for Pickup` | Hinterlegt / abholbereit (Postfiliale, Abholstation, GLS ParcelShop, FedEx Hold at Location) |
| `Delivered` | Zugestellt |
| `Problem` | Zustellproblem — Kundendienst kontaktieren |
| `Returned` | Retour an Absender |
| `Lost` | Verlust gemeldet |

## Tracking page (by token)

The shipping notification mail links to the customer's tracking page in the
shop. The link carries a secret token per Sales Order
(`Sales Order.custom_tracking_page_token`, created when the first mail for
the order is built). The shop resolves it with:

```
GET /api/method/erpnext_parcel_station.parcel.tracking.api.get_tracking_page
    ?token=Zq3…  (32 url-safe characters)
```

```json
{
  "message": {
    "sales_order": "SO-2026-00123",
    "order_date": "2026-09-04",
    "shipments": [
      {
        "shipment": "SHIPMENT-00158",
        "carrier": "AUSTRIAN_POST",
        "carrier_name": "Österreichische Post",
        "carrier_tracking_url": "https://www.post.at/sv/sendungsdetails?snr=1019012500386270268443",
        "tracking_number": "1019012500386270268443",
        "tracking_status": "Out for Delivery",
        "shipped_at": "2026-09-05 14:12:03",
        "delivery_type": "Home Delivery",
        "last_event_at": "2026-09-06 08:25:59",
        "events": [ … as above … ]
      }
    ]
  }
}
```

* Unknown or malformed token → HTTP 404-style Frappe error. The shop shows
  the same "not found" page for both; it never reveals whether an order exists.
* `shipments` are sorted oldest first.
* `carrier_tracking_url` may be `null` (carrier without a public page).
* The page URL the mail uses is configured in **Parcel Station Settings →
  Shipping Notification → Tracking Page URL**, e.g.
  `https://www.example.com/{language}/tracking/{token}`.

## Deliberately NOT exposed

* **Carrier tracking links in `get_order_tracking` and in mails** — the
  customer journey starts on our own pages. Only the tracking page itself
  (`get_tracking_page`) offers a link out to the carrier, because redirecting
  a parcel or choosing a delivery day is only possible there.
* **Free-text event descriptions / consignee names** — carrier remark fields
  carry recipient and neighbour names (PII).
* **Addresses, items and prices** — the tracking page is reachable by link
  alone, so it shows the parcel's journey and nothing about the customer.
* Raw carrier payloads.
