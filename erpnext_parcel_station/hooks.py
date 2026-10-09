app_name = "erpnext_parcel_station"
app_title = "ERPNext Parcel Station"
app_publisher = "Daniel Devich"
app_description = (
    "Parcel station integration for ERPNext: multi-carrier label "
    "generation (GLS, Austrian Post, FedEx), Shipping Rule routing via "
    "Carrier Service, and a barcode-driven parcel-station scanning UI."
)
app_email = "dade000@users.noreply.github.com"
app_license = "MIT"
app_version = "0.3.7"

fixtures = [
    {
        "dt": "Custom Field",
        "filters": [
            [
                "name",
                "in",
                {
                    "Sales Order-custom_carrier_service",
                    "Sales Order-custom_tracking_number",
                    "Sales Order-delivery_type",
                    "Sales Order-custom_shipping",
                    "Shipment-custom_carrier_service",
                    "Shipment-delivery_type",
                    "Shipment-pickup_reminder_sent_at",
                    "Shipment-shipping_notification_sent_at",
                    "Sales Order-custom_tracking_page_token",
                    "Shipment-fedex_pickup",
                    "Delivery Note-custom_carrier_service",
                    "Address-is_parcel_shop",
                    "Address-parcel_shop_id",
                    "Address-parcel_shop_carrier"
                }
            ]
        ]
    }
]


after_install = "erpnext_parcel_station.install.after_install"


doctype_js = {
    "Shipment": "public/js/shipment.js",
}


doc_events = {
    "Parcel Shipment": {
        "on_submit": "erpnext_parcel_station.parcel.app.controllers.on_submit_shipment"
    },
    "Shipment": {
        "before_insert": "erpnext_parcel_station.parcel.infra.gls_integration.copy_shipping_details_to_shipment",
        "on_submit": "erpnext_parcel_station.parcel.infra.gls_integration.create_carrier_label_on_submit",
        "on_cancel": [
            # GLS cancel first: if it fails it throws and aborts the whole
            # cancellation (keeping ERPNext and GLS in sync), so we don't want
            # the parcel-item cleanup to have run beforehand.
            "erpnext_parcel_station.parcel.infra.gls_integration.cancel_gls_label_on_cancel",
            "erpnext_parcel_station.parcel.api.carrier.cancel_austrian_post_on_cancel",
            "erpnext_parcel_station.parcel.infra.fedex_api.cancel_fedex_label_on_cancel",
            "erpnext_parcel_station.parcel.api.shipments_ui.clear_parcel_items_for_shipment",
            "erpnext_parcel_station.parcel.api.shipments_ui.log_cancellation_activity",
        ],
    },
    "Delivery Note": {
        "before_validate": "erpnext_parcel_station.parcel.events.delivery_note.before_validate"
    },
    "Carrier Service": {
        "validate": "erpnext_parcel_station.parcel.events.carrier_service.validate"
    },
    "Sales Order": {
        "before_validate": "erpnext_parcel_station.parcel.events.sales_order.before_validate"
    },
}


override_whitelisted_methods = {

}


scheduler_events = {
    "cron": {
        # Hourly 06:00-22:00. The Post drops a POSTTRACK file every hour
        # (until 2026-10 only at 08/12/17, always around *:04); each fetch
        # imports every file not yet logged, so the night's files arrive with
        # the 06:15 run. The fetch runs at :15, the FedEx/GLS polls at :22, and the 08:30
        # digest then reports on data all three carriers refreshed minutes
        # earlier — including the night's Post events.
        "15 6-22 * * *": [
            "erpnext_parcel_station.parcel.tracking.post_sftp.fetch_post_tracking_files",
        ],
        "22 6-22 * * *": [
            "erpnext_parcel_station.parcel.tracking.fedex_track.poll_fedex_tracking",
            "erpnext_parcel_station.parcel.tracking.gls_track.poll_gls_tracking",
        ],
        "30 8 * * *": [
            "erpnext_parcel_station.parcel.tracking.monitor.send_tracking_digest",
        ],
        # 09:00: one mail to each customer whose parcel has been waiting at a
        # post office / ParcelShop longer than the configured days — after the
        # 08:xx ingestion, so a parcel collected yesterday is already
        # Delivered and no longer a candidate.
        "0 9 * * *": [
            "erpnext_parcel_station.parcel.tracking.pickup_reminder.send_pickup_reminders",
        ],
        # 12:30 and 17:30: one mail per customer for every parcel labelled
        # since the last run. A batch rather than a hook on the label call, so
        # a label cancelled or reprinted right away never reaches the customer;
        # the times follow the 12:22 / 17:22 tracking polls. Off by default;
        # Parcel Station Settings enables it.
        "30 12,17 * * *": [
            "erpnext_parcel_station.parcel.tracking.shipping_notification.send_shipping_notifications",
        ],
        # 12:00 Mon-Fri: book one FedEx Express courier pickup for every FedEx
        # shipment labelled since the last pickup (window from FedEx Settings,
        # default 12:30-16:00). Off by default; FedEx Settings enables it.
        "0 12 * * 1-5": [
            "erpnext_parcel_station.parcel.infra.fedex_pickup.request_pending_pickups",
        ],
    },
}
