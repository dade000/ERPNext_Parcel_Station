/**
 * Parcel Station API Service
 * Handles all API calls to the backend
 */
window.ParcelStationAPI = {
	_extractError: function (data, fallbackMessage) {
		if (!data) return fallbackMessage;
		if (data._server_messages) {
			try {
				const serverMessages = JSON.parse(data._server_messages);
				if (Array.isArray(serverMessages) && serverMessages.length) {
					const cleaned = serverMessages
						.map((msg) => {
							try {
								const parsed = JSON.parse(msg);
								return parsed.message || parsed;
							} catch (err) {
								return msg;
							}
						})
						.join("\n");
					if (cleaned) {
						return cleaned;
					}
				}
			} catch (err) {
				console.error("Failed to parse server messages:", err);
			}
		}
		return data.message || data.exception || data.exc || fallbackMessage;
	},
	/**
	 * Get CSRF token for API requests
	 * @returns {string} CSRF token
	 */
	getCSRFToken: function () {
		return (window.frappe && frappe.csrf_token) || "";
	},

	/**
	 * Fetch delivery note data for a given barcode
	 * @param {string} barcode - The barcode to lookup
	 * @returns {Promise} Promise resolving to delivery note data
	 */
	fetchDeliveryNote: async function (barcode) {
		const response = await fetch(
			"/api/method/erpnext_parcel_station.parcel.api.shipments_ui.fetch_delivery_note_for_parcel",
			{
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Frappe-CSRF-Token": this.getCSRFToken(),
				},
				credentials: "same-origin",
				body: JSON.stringify({ barcode: barcode }),
			}
		);

		const data = await response.json();

		if (data && data.message) {
			return data.message;
		} else {
			throw new Error(data.exception || data.exc || "Failed to load delivery note");
		}
	},

	/**
	 * Create a shipment from barcode and selected items
	 * @param {string} barcode - The delivery note barcode
	 * @param {Array} items - Array of selected items
	 * @param {object|null} dimensions - {length, width, height} in mm, or null
	 * @param {object|null} weight - {kg, source: "scale"|"manual"}, or null
	 * @returns {Promise} Promise resolving to shipment data
	 */
	createShipment: async function (barcode, items, dimensions, weight, labelFormat) {
		const body = {
			barcode: barcode,
			items: items,
		};
		// "zpl" or "pdf": the format that fits the printer selected at this
		// station. Omitted: the server asks for ZPL.
		if (labelFormat) {
			body.label_format = labelFormat;
		}
		// Weight measured in the desk (hardware bridge) or typed in by the
		// operator. Omitted: the server reads its scale / article weights.
		if (weight) {
			body.client_weight_kg = weight.kg;
			body.client_weight_source = weight.source;
		}
		// Parcel dimensions (from a dimension barcode scan) — forwarded so the
		// backend stores them on the Shipment parcel row. Omitted for the manual
		// flow (dimensions == null).
		if (dimensions) {
			body.length = dimensions.length;
			body.width = dimensions.width;
			body.height = dimensions.height;
		}
		const response = await fetch(
			"/api/method/erpnext_parcel_station.parcel.api.shipments_ui.create_shipment_from_barcode",
			{
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Frappe-CSRF-Token": this.getCSRFToken(),
				},
				credentials: "same-origin",
				body: JSON.stringify(body),
			}
		);

		const data = await response.json();

		if (data && data.message) {
			return data.message;
		} else {
			// Prefer the human-readable frappe.throw message (e.g. "Service Code
			// is required …") over the raw exception class, so validation errors
			// surface cleanly in the UI.
			let msg = data.exception || data.exc || "Failed to create shipment";
			try {
				const serverMessages = JSON.parse(data._server_messages || "[]");
				if (serverMessages.length) {
					const last = JSON.parse(serverMessages[serverMessages.length - 1]);
					if (last && last.message) {
						msg = last.message;
					}
				}
			} catch (e) {
				/* fall back to msg above */
			}
			throw new Error(msg);
		}
	},

	/**
	 * Preview the GLS API payload that would be POSTed for ``shipmentName``.
	 * Read-only — does not touch GLS. Returns ``{shipment, services, payload}``
	 * where ``payload`` is the exact JSON body the backend will send.
	 * Used for browser-side debugging (logs the wire payload before label
	 * dispatch). See ``parcel/infra/gls_api.py:preview_gls_payload``.
	 * @param {string} shipmentName - The shipment name
	 * @returns {Promise<object|null>} Promise resolving to the preview dict or
	 *   ``null`` if the backend rejected (e.g. carrier not configured).
	 */
	previewGlsPayload: async function (shipmentName) {
		const response = await fetch(
			"/api/method/erpnext_parcel_station.parcel.infra.gls_api.preview_gls_payload",
			{
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Frappe-CSRF-Token": this.getCSRFToken(),
				},
				credentials: "same-origin",
				body: JSON.stringify({ shipment: shipmentName }),
			}
		);
		const data = await response.json();
		return data && data.message ? data.message : null;
	},

	/**
	 * Preview the FedEx Ship API payload that would be POSTed for
	 * ``shipmentName``. Read-only — does not touch FedEx, and the account
	 * number is redacted server-side before it reaches the browser console.
	 * See ``parcel/infra/fedex_api.py:preview_fedex_payload``.
	 * @param {string} shipmentName - The shipment name
	 * @returns {Promise<object|null>} Promise resolving to ``{shipment, mode,
	 *   payload}`` or ``null`` if the backend rejected (e.g. FedEx disabled).
	 */
	previewFedexPayload: async function (shipmentName) {
		const response = await fetch(
			"/api/method/erpnext_parcel_station.parcel.infra.fedex_api.preview_fedex_payload",
			{
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Frappe-CSRF-Token": this.getCSRFToken(),
				},
				credentials: "same-origin",
				body: JSON.stringify({ shipment: shipmentName }),
			}
		);
		const data = await response.json();
		return data && data.message ? data.message : null;
	},

	/**
	 * Request shipping label for a shipment
	 * @param {string} shipmentName - The shipment name
	 * @returns {Promise} Promise resolving to label data
	 */
	requestLabel: async function (shipmentName, labelFormat) {
		const response = await fetch(
			"/api/method/erpnext_parcel_station.parcel.api.shipments_ui.request_label",
			{
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Frappe-CSRF-Token": this.getCSRFToken(),
				},
				credentials: "same-origin",
				body: JSON.stringify({
					shipment_name: shipmentName,
					label_format: labelFormat || undefined,
				}),
			}
		);

		const data = await response.json();
		// Surface carrier/server failures (HTTP error or _server_messages) so the
		// caller's catch can show the real reason — mirrors printLabel /
		// fetchNetworkPrinters. A 200 with no message stays a soft null (handled
		// by the caller as "no download link yet"), preserving the success flow.
		if (!response.ok) {
			throw new Error(
				this._extractError(data, `Failed to generate label (HTTP ${response.status})`)
			);
		}
		return data && data.message ? data.message : null;
	},

	_stationFile: async function (method, shipmentName, fallback) {
		const response = await fetch(
			`/api/method/erpnext_parcel_station.parcel.api.shipments_ui.${method}`,
			{
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Frappe-CSRF-Token": this.getCSRFToken(),
				},
				credentials: "same-origin",
				body: JSON.stringify({ shipment_name: shipmentName }),
			}
		);
		const data = await response.json();
		if (!response.ok || !data || !data.message) {
			throw new Error(this._extractError(data, fallback));
		}
		return data.message;
	},

	/**
	 * The stored label of a shipment as the carrier returned it:
	 * {file_name, label_format, content_base64}. For printing through the
	 * hardware bridge (ZPL) or the browser's print dialog (PDF).
	 */
	getShipmentLabel: async function (shipmentName) {
		return this._stationFile("get_shipment_label", shipmentName, "Label konnte nicht geladen werden");
	},

	/** The stored customs documents (PDF) for the browser's print dialog. */
	getShipmentDocuments: async function (shipmentName) {
		return this._stationFile(
			"get_shipment_documents",
			shipmentName,
			"Zolldokumente konnten nicht geladen werden"
		);
	},

	/**
	 * Fetch address display using frappe API
	 * @param {string} addressDictOrName - Address reference
	 * @returns {Promise} Promise resolving to formatted address
	 */
	fetchAddressDisplay: async function (addressDictOrName) {
		// If frappe.xcall is available in the context, prefer it
		if (window.frappe && frappe.xcall) {
			return await frappe.xcall(
				"frappe.contacts.doctype.address.address.get_address_display",
				{ address_dict: addressDictOrName }
			);
		}

		// Fallback to REST call
		const response = await fetch(
			"/api/method/frappe.contacts.doctype.address.address.get_address_display",
			{
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Frappe-CSRF-Token": this.getCSRFToken(),
				},
				credentials: "same-origin",
				body: JSON.stringify({ address_dict: addressDictOrName }),
			}
		);

		const data = await response.json();
		return data && data.message ? data.message : null;
	},

	/**
	 * Fetch list of configured network printers
	 * @returns {Promise<Array>} Array of printer records
	 */
	fetchNetworkPrinters: async function () {
		const response = await fetch(
			"/api/method/erpnext_parcel_station.parcel.api.shipments_ui.list_network_printers",
			{
				method: "GET",
				credentials: "same-origin",
				headers: {
					"X-Frappe-CSRF-Token": this.getCSRFToken(),
				},
			}
		);

		const data = await response.json();
		if (!response.ok) {
			throw new Error(
				this._extractError(data, `Failed to load printers (HTTP ${response.status})`)
			);
		}

		const payload =
			data && data.message !== undefined ? data.message : data && data.data ? data.data : data;
		if (Array.isArray(payload)) {
			return payload;
		}
		if (payload && Array.isArray(payload.printers)) {
			return payload.printers;
		}
		return [];
	},

	/**
	 * Send a print job for the shipment label to the selected printer
	 * @param {string} shipmentName
	 * @param {string} printerName - Network Printer Settings docname
	 * @returns {Promise<object>} Print job data
	 */
	/**
	 * Send a print job for the shipment's customs documents (A4 PDF) to the
	 * selected sheet printer. Separate queue from the label on purpose.
	 * @param {string} shipmentName
	 * @param {string} printerName - Network Printer Settings docname
	 * @param {string} trigger - "auto" or "manual"
	 * @returns {Promise<object>} Print job data
	 */
	printCustomsDocuments: async function (shipmentName, printerName, trigger) {
		const response = await fetch(
			"/api/method/erpnext_parcel_station.parcel.api.shipments_ui.print_shipment_documents",
			{
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Frappe-CSRF-Token": this.getCSRFToken(),
				},
				credentials: "same-origin",
				body: JSON.stringify({
					shipment_name: shipmentName,
					printer: printerName,
					trigger: trigger || "manual",
				}),
			}
		);

		const data = await response.json();
		if (!response.ok) {
			throw new Error(
				this._extractError(
					data,
					`Failed to print customs documents (HTTP ${response.status})`
				)
			);
		}

		if (data && data.message) {
			return data.message;
		}
		throw new Error(this._extractError(data, "Failed to print customs documents"));
	},

	_fedexPickupCall: async function (method, body, fallback) {
		const url = `/api/method/erpnext_parcel_station.parcel.infra.fedex_pickup.${method}`;
		const init = {
			method: body ? "POST" : "GET",
			headers: {
				"Content-Type": "application/json",
				"X-Frappe-CSRF-Token": this.getCSRFToken(),
			},
			credentials: "same-origin",
		};
		if (body) init.body = JSON.stringify(body);
		const response = await fetch(url, init);
		const data = await response.json();
		if (!response.ok) {
			throw new Error(this._extractError(data, `${fallback} (HTTP ${response.status})`));
		}
		if (data && data.message !== undefined) {
			return data.message;
		}
		throw new Error(this._extractError(data, fallback));
	},

	listOrdersToPack: async function (days) {
		const url =
			"/api/method/erpnext_parcel_station.parcel.api.shipments_ui.list_orders_to_pack" +
			(days ? `?days=${encodeURIComponent(days)}` : "");
		const response = await fetch(url, {
			method: "GET",
			headers: { "X-Frappe-CSRF-Token": this.getCSRFToken() },
			credentials: "same-origin",
		});
		const data = await response.json();
		if (!response.ok) {
			throw new Error(
				this._extractError(data, `Failed to load orders to pack (HTTP ${response.status})`)
			);
		}
		if (data && data.message) {
			return data.message;
		}
		throw new Error(this._extractError(data, "Failed to load orders to pack"));
	},

	getFedexPickupOverview: async function () {
		return this._fedexPickupCall("get_fedex_pickup_overview", null, "Failed to load FedEx pickup status");
	},

	/** shipments: array of Shipment names, or null for every open FedEx shipment */
	requestFedexPickup: async function (shipments) {
		return this._fedexPickupCall(
			"request_fedex_pickup",
			{ shipments: shipments ? JSON.stringify(shipments) : null },
			"Failed to request FedEx pickup"
		);
	},

	cancelFedexPickup: async function (pickupName, reason) {
		return this._fedexPickupCall(
			"cancel_fedex_pickup",
			{ pickup: pickupName, reason: reason || "" },
			"Failed to cancel FedEx pickup"
		);
	},

	printLabel: async function (shipmentName, printerName, trigger) {
		const response = await fetch(
			"/api/method/erpnext_parcel_station.parcel.api.shipments_ui.print_shipment_label",
			{
				method: "POST",
				headers: {
					"Content-Type": "application/json",
					"X-Frappe-CSRF-Token": this.getCSRFToken(),
				},
				credentials: "same-origin",
				body: JSON.stringify({
					shipment_name: shipmentName,
					printer: printerName,
					// "auto" (Auto Shipment flow) or "manual" (Print button) — logged
					// server-side so auto-print is verifiable in docker logs.
					trigger: trigger || "manual",
				}),
			}
		);

		const data = await response.json();
		if (!response.ok) {
			throw new Error(
				this._extractError(data, `Failed to print label (HTTP ${response.status})`)
			);
		}

		if (data && data.message) {
			return data.message;
		}
		throw new Error(this._extractError(data, "Failed to print label"));
	},
};
