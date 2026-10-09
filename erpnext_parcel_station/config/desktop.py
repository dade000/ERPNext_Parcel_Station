from __future__ import annotations
def get_data():
    return [
        {
            "label": "Parcel Station",
            "items": [
                {"type": "doctype", "name": "Parcel Shipment", "label": "Parcel Shipment", "description": "Manage parcel shipments"},
                {"type": "doctype", "name": "Parcel Station Settings", "label": "Settings", "description": "General parcel-station configuration"},
                {"type": "doctype", "name": "Austrian Post Settings", "label": "Austrian Post Settings", "description": "Austrian Post SOAP API and tracking SFTP"},
            ],
        }
    ]
