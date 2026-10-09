import frappe
from erpnext_parcel_station.parcel.domain.ports import ShippingApiClient
from erpnext_parcel_station.parcel.domain.dtos import ShipmentRequest, ShipmentResponse, LabelResponse
from erpnext_parcel_station.parcel.domain.errors import CarrierApiError

# --- IMPORT GLS CODE ---
# This imports the GLS logic that has been moved into this app.
try:
    from erpnext_parcel_station.parcel.infra.gls_api import GLSAPIClient
    from erpnext_parcel_station.parcel.infra.gls_integration import (
        get_gls_credentials, 
        map_dn_to_gls_parcel,
        get_label_from_gls # Assuming you have a function like this
    )
except ImportError:
    frappe.log_error(
        title="Parcel Station Error",
        message="GLS Client requires GLS integration modules."
    )
    # This will fail loudly if the GLS modules are missing
    raise ImportError("GLS Client requires GLS integration modules.")


class GlsClient(ShippingApiClient):
    """
    Adapter Client that uses the GLS integration logic.
    """
    
    def __init__(self, settings_doc=None):
        # Use the existing function to get credentials from "GLS Settings"
        try:
            self.creds = get_gls_credentials()
            self.api = GLSAPIClient(self.creds)
        except Exception as e:
            frappe.log_error(f"Failed to initialize GlsClient: {e}")
            raise CarrierApiError(f"Failed to get GLS credentials: {e}")

    def create_shipment(self, request: ShipmentRequest) -> ShipmentResponse:
        """
        Called by the Parcel Station API when a DN is scanned.
        """
        try:
            # 1. Get the full Delivery Note document
            dn_doc = frappe.get_doc("Delivery Note", request.delivery_note_name)

            # 2. Use existing logic to map DN to GLS payload
            parcel_payload = map_dn_to_gls_parcel(dn_doc)
            
            # 3. Call GLS API using the existing GlsApi class
            # We assume .create_parcel() returns a dict like {"track_id": "...", "label_data": "..."}
            # You may need to adapt this line based on your gls_api.py
            response = self.api.create_parcel(parcel_payload)

            if not response.get("track_id"):
                raise CarrierApiError(f"GLS API Error: {response.get('error') or 'Unknown error'}")

            tracking_number = response.get("track_id")
            
            # The label_data could be the raw PDF data or a URL to fetch it
            label_data_or_url = response.get("label_data")

            # 4. Return the standardized Parcel Station response
            return ShipmentResponse(
                tracking_number=str(tracking_number),
                label_data=label_data_or_url, # This will be saved in Parcel Shipment
                carrier="GLS"
            )
            
        except Exception as e:
            frappe.log_error(title="GLS Client Error")
            # This ensures the scanning UI gets a clean error message
            raise CarrierApiError(f"GLS Shipment Failed: {e}")

    def get_label(self, tracking_number: str, format: str = "pdf") -> LabelResponse:
        """
        Called if the Parcel Station needs to re-download a label.
        """
        try:
            # Use existing logic to get the label PDF data
            # You might need to change 'get_label_from_gls' to the correct function name
            label_pdf_data = get_label_from_gls(tracking_number, self.creds)
            
            return LabelResponse(
                data=label_pdf_data,
                file_format="pdf"
            )
        except Exception as e:
            frappe.log_error(title="GLS Get Label Error")
            raise CarrierApiError(f"GLS Get Label Failed: {e}")