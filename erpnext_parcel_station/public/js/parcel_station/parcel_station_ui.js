/**
 * Parcel Station UI Manager
 * Handles all UI rendering and updates
 */
window.ParcelStationUI = {
	elements: {},

	/**
	 * Initialize DOM element references
	 */
	initializeElements: function () {
		const byId = (id) => document.getElementById(id);
		this.elements = {
			root: byId("parcel-station-root"),
			barcodeInput: byId("barcode-input"),
			itemsList: byId("items-list"),
			packlistHint: byId("ps-packlist-hint"),
			createShipmentBtn: byId("create-shipment-btn"),
			deferBtn: byId("ps-defer-btn"),
			// Result of the last label
			result: byId("ps-result"),
			resultTitle: byId("shipment-status"),
			resultDocuments: byId("ps-result-documents"),
			shipmentCreated: byId("shipment-created"),
			trackingNumber: byId("tracking-number"),
			shipmentLabel: byId("shipment-label"),
			shipmentDocuments: byId("shipment-documents"),
			printLabelBtn: byId("print-label-btn"),
			printDocumentsBtn: byId("print-documents-btn"),
			printerStatus: byId("printer-status"),
			documentPrinterStatus: byId("document-printer-status"),
			// Printer panel (header)
			printerPanel: byId("print-section-card"),
			printerSelect: byId("printer-select"),
			documentPrinterSelect: byId("document-printer-select"),
			refreshPrintersBtn: byId("refresh-printers-btn"),
			chipScale: byId("ps-chip-scale"),
			chipLabelPrinter: byId("ps-chip-label-printer"),
			chipDocumentPrinter: byId("ps-chip-document-printer"),
			// Work list
			ordersToPackBtn: byId("orders-to-pack-btn"),
			progressFill: byId("ps-progress-fill"),
			progressText: byId("ps-progress-text"),
			queueFilters: byId("ps-queue-filters"),
			queueList: byId("ps-queue-list"),
			pendingSection: byId("ps-pending-section"),
			pendingDeliveryNotes: byId("pending-delivery-notes"),
			clearPendingBtn: byId("clear-pending-btn"),
			// Messages
			messageDiv: byId("ps-message"),
			messageText: byId("ps-message-text"),
			messageClose: byId("ps-message-close"),
			// FedEx pickup panel
			fedexBtn: byId("ps-fedex-btn"),
			fedexCount: byId("ps-fedex-count"),
			fedexPickupCard: byId("fedex-pickup-card"),
			fedexPickupStatus: byId("fedex-pickup-status"),
			fedexPickupList: byId("fedex-pickup-list"),
			fedexPickupRequestBtn: byId("fedex-pickup-request-btn"),
			fedexPickupRefreshBtn: byId("fedex-pickup-refresh-btn"),
			// Parcel dimensions
			dims: byId("ps-dims"),
			dimsPill: byId("ps-dims-pill"),
			dimsValue: byId("ps-dims-value"),
			dimsScan: byId("ps-dims-scan"),
			dimsManual: byId("ps-dims-manual"),
			dimsManualBtn: byId("ps-dims-manual-btn"),
			dimsScanBtn: byId("ps-dims-scan-btn"),
			dimsLength: byId("ps-dims-length"),
			dimsWidth: byId("ps-dims-width"),
			dimsHeight: byId("ps-dims-height"),
		};
	},

	_escape: function (value) {
		return String(value == null ? "" : value)
			.replace(/&/g, "&amp;")
			.replace(/</g, "&lt;")
			.replace(/>/g, "&gt;")
			.replace(/"/g, "&quot;");
	},

	_formatPickupDate: function (isoDate) {
		// "2026-09-24" -> "24.09.2026"; today/tomorrow get a word.
		if (!isoDate) return "";
		const [y, m, d] = isoDate.split("-");
		const today = new Date();
		const pad = (n) => String(n).padStart(2, "0");
		const todayIso = `${today.getFullYear()}-${pad(today.getMonth() + 1)}-${pad(today.getDate())}`;
		const tomorrow = new Date(today.getTime() + 86400000);
		const tomorrowIso = `${tomorrow.getFullYear()}-${pad(tomorrow.getMonth() + 1)}-${pad(tomorrow.getDate())}`;
		if (isoDate === todayIso) return "heute";
		if (isoDate === tomorrowIso) return "morgen";
		return `${d}.${m}.${y}`;
	},

	/**
	 * "seit 2 Tagen" — how long a Delivery Note has been waiting.
	 * @param {string} isoDate - posting date, "2026-09-30"
	 */
	_waitingSince: function (isoDate) {
		if (!isoDate) return "";
		const [y, m, d] = isoDate.split("-").map(Number);
		const posted = new Date(y, m - 1, d);
		const today = new Date();
		today.setHours(0, 0, 0, 0);
		const days = Math.round((today - posted) / 86400000);
		if (days <= 0) return "heute";
		return days === 1 ? "seit 1 Tag" : `seit ${days} Tagen`;
	},

	_carrierFilterLabel: function (key) {
		return { AUSTRIAN_POST: "Post", GLS: "GLS", FEDEX: "FedEx" }[key] || key;
	},

	/**
	 * Progress bar in the header: labelled today vs. still to pack.
	 * @param {Object|null} response - list_orders_to_pack(), or null on error
	 */
	renderProgress: function (response) {
		const text = this.elements.progressText;
		const fill = this.elements.progressFill;
		if (!text || !fill) return;
		if (!response) {
			text.innerHTML = '<span class="ps-text-danger">Arbeitsliste konnte nicht geladen werden</span>';
			return;
		}
		const open = (response.data || []).length;
		const packed = response.packed_today || 0;
		const total = open + packed;
		fill.style.width = total ? `${Math.round((packed / total) * 100)}%` : "0";
		text.innerHTML = `<b>${packed} gepackt</b> <span>· ${open} offen</span>`;
	},

	/**
	 * Render the permanent work list ("Zu packen").
	 * @param {Object|null} response - {data, days, packed_today}, null on error
	 * @param {Object} options - {filter, current, onPick(name), onFilter(key)}
	 */
	renderQueue: function (response, options) {
		const list = this.elements.queueList;
		const filters = this.elements.queueFilters;
		if (!list || !filters) return;
		this.renderProgress(response);
		if (!response) {
			filters.innerHTML = "";
			list.innerHTML = '<div class="ps-queue-empty ps-text-danger">Arbeitsliste konnte nicht geladen werden.</div>';
			return;
		}

		const rows = response.data || [];
		const counts = {};
		rows.forEach((r) => {
			const key = r.carrier_key || "OTHER";
			counts[key] = (counts[key] || 0) + 1;
		});
		const keys = ["AUSTRIAN_POST", "GLS", "FEDEX", "OTHER"].filter((key) => counts[key]);
		const active = options.filter && counts[options.filter] ? options.filter : null;

		// Carrier filters only earn their place when there is a choice.
		filters.innerHTML =
			keys.length > 1
				? [`<button type="button" class="ps-filter" data-filter="" aria-pressed="${!active}">Alle ${rows.length}</button>`]
						.concat(
							keys.map(
								(key) =>
									`<button type="button" class="ps-filter" data-filter="${key}" aria-pressed="${active === key}">` +
									`${this._escape(key === "OTHER" ? "Andere" : this._carrierFilterLabel(key))} ${counts[key]}</button>`
							)
						)
						.join("")
				: "";
		filters.querySelectorAll(".ps-filter").forEach((btn) => {
			btn.addEventListener("click", () => options.onFilter(btn.dataset.filter || null));
		});

		const visible = active ? rows.filter((r) => (r.carrier_key || "OTHER") === active) : rows;
		if (!visible.length) {
			list.innerHTML = `<div class="ps-queue-empty">Nichts zu packen (letzte ${this._escape(response.days)} Tage).</div>`;
			return;
		}

		list.innerHTML = visible
			.map((r) => {
				const count = r.goods_count || r.item_count || 0;
				const place = [r.city, r.country_code].filter(Boolean).join(", ");
				const meta = [
					`${count} ${count === 1 ? "Position" : "Positionen"}`,
					place,
					this._waitingSince(r.posting_date),
				]
					.filter(Boolean)
					.join(" · ");
				const flags = [];
				if (r.state === "label_missing") {
					flags.push('<span class="ps-flag ps-flag-danger" title="Sendung angelegt, aber ohne Label. Laden und erneut erstellen.">Label fehlt</span>');
				}
				if (r.customs) flags.push('<span class="ps-flag">Zoll</span>');
				if (r.is_parcel_shop) flags.push('<span class="ps-flag">Paketshop</span>');
				return (
					`<button type="button" class="ps-queue-row" data-delivery-note="${this._escape(r.name)}" ` +
					`aria-current="${options.current === r.name}" title="${this._escape(r.name)}">` +
						`<span class="ps-queue-main">` +
							`<span class="ps-queue-name">${this._escape(r.customer_name || r.name)}</span>` +
							`<span class="ps-queue-meta">${this._escape(meta)}</span>` +
						`</span>` +
						`<span class="ps-queue-side">${flags.join("")}${ParcelStationTemplates.carrierBadge(r.carrier_key, r.carrier)}</span>` +
					`</button>`
				);
			})
			.join("");
		list.querySelectorAll(".ps-queue-row").forEach((row) => {
			row.addEventListener("click", () => options.onPick(row.dataset.deliveryNote));
		});
	},

	/** Mark the Delivery Note in hand in the work list (no re-render). */
	highlightQueueRow: function (deliveryNote) {
		if (!this.elements.root) return;
		this.elements.root.querySelectorAll(".ps-queue-row").forEach((row) => {
			row.setAttribute("aria-current", String(Boolean(deliveryNote) && row.dataset.deliveryNote === deliveryNote));
		});
	},

	/**
	 * Render the FedEx pickup card from get_fedex_pickup_overview().
	 * @param {Object|null} overview - null while loading / on error
	 * @param {string} [errorMessage]
	 */
	renderFedexPickup: function (overview, errorMessage) {
		const status = this.elements.fedexPickupStatus;
		const list = this.elements.fedexPickupList;
		const requestBtn = this.elements.fedexPickupRequestBtn;
		const badge = this.elements.fedexCount;
		if (!status || !list || !requestBtn) return;
		const setBadge = (n) => {
			if (!badge) return;
			badge.textContent = String(n);
			badge.style.display = n ? "" : "none";
		};

		if (errorMessage) {
			status.innerHTML = `<span class="ps-text-danger">${this._escape(errorMessage)}</span>`;
			list.innerHTML = "";
			requestBtn.disabled = true;
			setBadge(0);
			return;
		}
		if (!overview) {
			status.textContent = "Lade …";
			return;
		}
		if (!overview.configured) {
			// No FedEx at this site: the header button would only be noise.
			if (this.elements.fedexBtn) this.elements.fedexBtn.style.display = "none";
			status.textContent = "FedEx ist nicht eingerichtet";
			list.innerHTML = "";
			requestBtn.disabled = true;
			setBadge(0);
			return;
		}
		if (this.elements.fedexBtn) this.elements.fedexBtn.style.display = "";

		const open = overview.open_shipments || [];
		const pickups = overview.pickups || [];
		const win = overview.next_window || {};
		const windowText = `${this._formatPickupDate(win.pickup_date)} ${win.ready_time}–${win.close_time}`;
		setBadge(open.length);

		if (open.length) {
			status.innerHTML = `<span class="ps-text-warn">${open.length} ${
				open.length === 1 ? "Sendung wartet" : "Sendungen warten"
			} auf Abholung</span>`;
			requestBtn.disabled = false;
			requestBtn.textContent = `Abholung anfragen (${open.length}) – ${windowText}`;
		} else {
			status.innerHTML = '<span class="ps-text-ok">Keine FedEx-Sendung wartet auf Abholung</span>';
			requestBtn.disabled = true;
			requestBtn.textContent = "Abholung anfragen";
		}

		const lines = [];
		if (open.length) {
			lines.push(
				`<div>Offen: ${open.map((s) => `<span class="ps-mono">${this._escape(s.name)}</span>`).join(", ")}</div>`
			);
		}
		pickups.forEach((p) => {
			const count = (p.shipments || []).length || p.package_count || 0;
			lines.push(
				`<div style="margin-top: 8px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap;">` +
					`<span>Gebucht ${this._escape(this._formatPickupDate(p.pickup_date))} ` +
					`${this._escape(p.ready_time)}–${this._escape(p.close_time)}, ` +
					`${count} ${count === 1 ? "Paket" : "Pakete"}, Bestätigung ` +
					`<span class="ps-mono">${this._escape(p.confirmation_code || "-")}</span></span>` +
					`<button type="button" class="ps-btn ps-btn-small ps-btn-danger fedex-pickup-cancel" ` +
					`data-pickup="${this._escape(p.name)}" data-confirmation="${this._escape(p.confirmation_code || "")}">Stornieren</button>` +
				`</div>`
			);
		});
		lines.push(
			`<div style="margin-top: 8px">Automatischer 12-Uhr-Lauf: ${overview.auto_enabled ? "ein" : "aus"}</div>`
		);
		list.innerHTML = lines.join("");
	},

	/**
	 * Show message to user
	 * @param {string} text - Message text
	 * @param {string} type - Message type (info, success, warning, danger)
	 */
	showMessage: function (text, type = "info") {
		if (this.elements.messageDiv && this.elements.messageText) {
			this.elements.messageDiv.dataset.type = type;
			// Errors interrupt (assertive); progress notes do not.
			this.elements.messageDiv.setAttribute("role", type === "danger" ? "alert" : "status");
			this.elements.messageText.textContent = text;
			this.elements.messageDiv.style.display = "block";
		} else {
			// Fallback to frappe msgprint if message elements don't exist
			frappe.msgprint(text);
		}
	},

	/**
	 * Hide message
	 */
	hideMessage: function () {
		if (this.elements.messageDiv) {
			this.elements.messageDiv.style.display = "none";
		}
	},

	/**
	 * Format an API / carrier error for the message panel under the Create
	 * Shipment button.
	 *
	 * Transparency-first: the EXACT error returned by the API / carrier is shown
	 * as-is — original wording, carrier error codes and server message details
	 * are preserved. We do NOT translate it into generic or simplified text. The
	 * only normalisation is stripping the HTML wrapper frappe adds around
	 * _server_messages and trimming whitespace, which reveals the real message
	 * text without changing its content. Backend payloads (_server_messages,
	 * exception, raw response) are read, never overwritten.
	 *
	 * @param {Error|string} error - The caught error (or a message string)
	 * @param {string} [context] - Unused; kept for call-site compatibility
	 * @returns {string} The raw API error text
	 */
	formatError: function (error /*, context */) {
		// Pull the raw text out of whatever was thrown. ParcelStationAPI already
		// extracts the carrier/server message from _server_messages / exception,
		// so error.message is the real API response in the common case.
		let raw = "";
		if (error && typeof error === "object") {
			raw = error.message || error.exception || error.exc || "";
		} else if (typeof error === "string") {
			raw = error;
		}

		// Reveal the actual message text: strip only the HTML wrapper frappe puts
		// around server messages and collapse whitespace runs. Wording, error
		// codes and carrier details are left exactly as the API returned them.
		const msg = String(raw || "")
			.replace(/<[^>]*>/g, " ")
			.replace(/\s+/g, " ")
			.trim();

		if (!msg) {
			return "Anfrage fehlgeschlagen (der Server hat keine Fehlermeldung geliefert).";
		}
		return msg;
	},

	/**
	 * Render items list
	 * @param {Array} items - Array of items to render
	 * @param {Object} deliveryNote - Delivery note data
	 * @param {Function} onItemChange - Callback for item selection change
	 */
	renderItems: function (items, deliveryNote, onItemChange) {
		if (!items || items.length === 0) {
			this.elements.itemsList.innerHTML = deliveryNote
				? '<div class="ps-empty">Auf diesem Lieferschein ist nichts mehr zu packen.</div>'
				: '<div class="ps-empty">Lieferschein scannen oder links auswählen.</div>';
			this.elements.createShipmentBtn.disabled = true;
			this.setPacklistHint(0, 0);
			this.updateCustomerInfo(deliveryNote || null);
			if (this.elements.deferBtn) this.elements.deferBtn.disabled = !deliveryNote;
			return;
		}

		// Update customer info display
		this.updateCustomerInfo(deliveryNote);

		// Render items table
		this.elements.itemsList.innerHTML = ParcelStationTemplates.getItemsTableTemplate(items);

		// Add event listeners to individual checkboxes
		this.elements.itemsList
			.querySelectorAll(".form-check-input[data-row-key]")
			.forEach((checkbox) => {
				checkbox.addEventListener("change", onItemChange);
			});

		this.setPacklistHint(items.length, items.length);
		this.elements.createShipmentBtn.disabled = false;
		if (this.elements.deferBtn) this.elements.deferBtn.disabled = false;
	},

	/**
	 * Adapt button and packing list to what can be done with the loaded
	 * Delivery Note (see ParcelStationManager.packMode).
	 * @param {"pack"|"retry"|"done"} mode
	 */
	setPackMode: function (mode) {
		const btn = this.elements.createShipmentBtn;
		if (!btn) return;
		this.packMode = mode;
		// Leave a running button alone; the manager resets it when it is done.
		if (!btn.querySelector(".spinner-border")) {
			btn.textContent = mode === "retry" ? "Label erneut anfordern" : "Label erstellen";
		}
		if (mode === "done") btn.disabled = true;
		// The items of a Shipment that only lacks its label are fixed: the
		// retry finishes that Shipment, it does not repack it.
		const locked = mode !== "pack";
		this.elements.itemsList.querySelectorAll(".form-check-input[data-row-key]").forEach((box) => {
			box.disabled = locked;
		});
		if (mode === "retry" && this.elements.packlistHint) {
			this.elements.packlistHint.textContent = "Diese Positionen liegen schon in der Sendung";
		} else if (mode === "done" && this.elements.packlistHint) {
			this.elements.packlistHint.textContent = "Alles bereits versendet";
		}
	},

	/**
	 * Line next to "Packliste": everything in, or a partial delivery.
	 * @param {number} selected
	 * @param {number} total
	 */
	setPacklistHint: function (selected, total) {
		const el = this.elements.packlistHint;
		if (!el) return;
		if (!total) {
			el.textContent = "";
		} else if (selected === total) {
			el.textContent = "Alles ausgewählt · Haken entfernen für eine Teillieferung";
		} else if (selected === 0) {
			el.textContent = "Nichts ausgewählt";
		} else {
			el.textContent = `Teillieferung: ${selected} von ${total} Positionen in diesem Paket`;
		}
	},

	/**
	 * Update customer information display
	 * @param {Object} deliveryNote - Delivery note data
	 */
	updateCustomerInfo: function (deliveryNote) {
		const customerInfoDiv = document.getElementById("customer-info");
		if (!customerInfoDiv) return;

		if (!deliveryNote) {
			customerInfoDiv.style.display = "none";
			return;
		}

		const customer =
			deliveryNote.customer_name || deliveryNote.delivery_to || deliveryNote.customer || "–";
		const shippingAddress = deliveryNote.delivery_address;
		const addressName =
			deliveryNote.shipping_address_name || deliveryNote.customer_address || null;

		// Prefer shipping address data if it exists and is not empty, otherwise try address lookup
		const displayAddress =
			shippingAddress && shippingAddress.trim()
				? this.formatAddressAsSingleLine(shippingAddress)
				: addressName
				? "Adresse wird geladen …"
				: "keine Lieferadresse am Lieferschein";

		customerInfoDiv.innerHTML = ParcelStationTemplates.getCustomerInfoTemplate(
			deliveryNote,
			customer,
			displayAddress
		);
		customerInfoDiv.style.display = "block";

		// If we don't have shipping address data but have an address reference, try to fetch it
		if (!shippingAddress && addressName) {
			this.fetchAndUpdateAddress(addressName, customer);
		}
	},

	/**
	 * Format address as single line
	 * @param {string} address - Address to format
	 * @returns {string} Formatted address
	 */
	formatAddressAsSingleLine: function (address) {
		if (!address) return "";
		return String(address)
			.replace(/<(?!br\s*\/?>)[^>]*>/gi, "") // drop any markup but line breaks
			.replace(/<br\s*\/?>/gi, ", ") // Replace <br> tags with commas
			.replace(/\n/g, ", ") // Replace newlines with commas
			.replace(/,\s*,/g, ",") // Remove duplicate commas
			.replace(/,\s*$/, "") // Remove trailing commas
			.replace(/^\s*,\s*/, "") // Remove leading commas
			.trim();
	},

	/**
	 * Fetch and update address display
	 * @param {string} addressName - Address reference
	 * @param {string} fallbackDeliveryTo - Fallback address
	 */
	fetchAndUpdateAddress: async function (addressName, fallbackDeliveryTo) {
		try {
			const display = await ParcelStationAPI.fetchAddressDisplay(addressName);
			const el = document.getElementById("delivery-to-text");
			if (el) {
				const singleLineAddress = this.formatAddressAsSingleLine(
					display || fallbackDeliveryTo
				);
				el.textContent = singleLineAddress;
			}
		} catch (err) {
			console.error("Error fetching address display:", err);
			const el = document.getElementById("delivery-to-text");
			if (el) el.textContent = fallbackDeliveryTo;
		}
	},

	/**
	 * Render pending delivery notes
	 * @param {Map} pendingDeliveryNotes - Map of pending delivery notes
	 * @param {Function} onPendingClick - Callback for pending note click
	 */
	renderPendingDeliveryNotes: function (pendingDeliveryNotes, onPendingClick) {
		const section = this.elements.pendingSection;
		const list = this.elements.pendingDeliveryNotes;
		if (!section || !list) return;
		section.hidden = pendingDeliveryNotes.size === 0;
		if (pendingDeliveryNotes.size === 0) {
			list.innerHTML = "";
			return;
		}

		list.innerHTML = Array.from(pendingDeliveryNotes.entries())
			.map(([deliveryNote, data]) => {
				const note = data.delivery_note_data || {};
				const count = data.items.length;
				const names = data.items
					.map((item) => `${item.qty} × ${item.item_name || item.item_code}`)
					.join(", ");
				return (
					`<button type="button" class="ps-queue-row pending-delivery-note" ` +
					`data-delivery-note="${this._escape(deliveryNote)}" title="${this._escape(names)}">` +
						`<span class="ps-queue-main">` +
							`<span class="ps-queue-name">${this._escape(note.customer_name || note.delivery_to || deliveryNote)}</span>` +
							`<span class="ps-queue-meta">${count} ${count === 1 ? "Position" : "Positionen"} noch offen · ` +
							`${this._escape(deliveryNote)}</span>` +
						`</span>` +
						`<span class="ps-queue-side">${ParcelStationTemplates.carrierBadge(note.carrier_key, note.carrier)}</span>` +
					`</button>`
				);
			})
			.join("");

		list.querySelectorAll(".pending-delivery-note").forEach((element) => {
			element.addEventListener("click", () => onPendingClick(element.dataset.deliveryNote));
		});
	},

	/**
	 * Update shipment information display
	 * @param {Object} shipmentData - Shipment data
	 */
	updateShipmentInfo: function (shipmentData) {
		if (this.elements.result) this.elements.result.hidden = false;

		if (shipmentData.shipment) {
			this.elements.shipmentCreated.textContent = shipmentData.shipment;
		}

		if (shipmentData.tracking_codes && shipmentData.tracking_codes.length > 0) {
			this.elements.trackingNumber.textContent = shipmentData.tracking_codes[0];
		}

		if (shipmentData.label_file_url) {
			this.elements.shipmentLabel.innerHTML =
				`<a href="${this._escape(shipmentData.label_file_url)}" target="_blank" rel="noopener">Label herunterladen</a>`;
		}

		// Customs papers only exist for customs destinations, so this block simply
		// stays hidden for domestic/EU shipments.
		if (shipmentData.customs_documents_url || shipmentData.shipment_documents_present) {
			if (this.elements.resultDocuments) this.elements.resultDocuments.hidden = false;
		}
		if (shipmentData.customs_documents_url && this.elements.shipmentDocuments) {
			this.elements.shipmentDocuments.innerHTML =
				`<a href="${this._escape(shipmentData.customs_documents_url)}" target="_blank" rel="noopener">Zolldokumente herunterladen</a>`;
		}
	},

	/**
	 * Headline of the result panel.
	 * @param {"working"|"done"|"problem"|"existing"} state
	 * @param {string} title
	 */
	setResultState: function (state, title) {
		if (!this.elements.result) return;
		this.elements.result.hidden = false;
		this.elements.result.dataset.state = state;
		if (this.elements.resultTitle) this.elements.resultTitle.textContent = title;
	},

	/**
	 * Reset shipment information display
	 */
	resetShipmentInfo: function () {
		if (this.elements.result) {
			this.elements.result.hidden = true;
			delete this.elements.result.dataset.state;
		}
		if (this.elements.resultDocuments) this.elements.resultDocuments.hidden = true;
		this.elements.shipmentCreated.textContent = "";
		this.elements.trackingNumber.textContent = "noch keine";
		this.elements.shipmentLabel.innerHTML = "";
		if (this.elements.shipmentDocuments) this.elements.shipmentDocuments.innerHTML = "";
	},

	/**
	 * Update button state with loading indicator
	 * @param {string} text - Button text
	 * @param {boolean} disabled - Whether button is disabled
	 */
	updateButtonState: function (text, disabled) {
		this.elements.createShipmentBtn.disabled = disabled;
		this.elements.createShipmentBtn.innerHTML = disabled
			? `<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>${text}`
			: text;
	},

	/**
	 * Clear current selection and reset UI
	 */
	clearCurrentSelection: function () {
		this.elements.barcodeInput.value = "";
		this.elements.itemsList.innerHTML =
			'<div class="ps-empty">Nächsten Lieferschein scannen oder links auswählen.</div>';
		this.elements.createShipmentBtn.disabled = true;
		if (this.elements.deferBtn) this.elements.deferBtn.disabled = true;
		this.setPacklistHint(0, 0);
		this.updateCustomerInfo(null);
		this.highlightQueueRow(null);
	},

	/**
	 * Focus on barcode input
	 */
	focusBarcodeInput: function () {
		this.elements.barcodeInput.focus();
	},

	/**
	 * Populate printer dropdown options
	 * @param {Array} printers
	 * @param {string} selectedName
	 */
	setPrinterOptions: function (groups, selectedValue = "", target = "label", absent = null) {
		const select =
			target === "documents"
				? this.elements.documentPrinterSelect
				: this.elements.printerSelect;
		if (!select) return;
		select.innerHTML = "";

		const placeholder = document.createElement("option");
		placeholder.value = "";
		placeholder.textContent = "Drucker wählen";
		select.appendChild(placeholder);

		// The remembered printer is not on offer right now (bridge not
		// running): keep it visible instead of showing "nothing selected".
		if (absent) {
			const option = document.createElement("option");
			option.value = absent.name;
			option.textContent = `${absent.printer_name} (Hardware Bridge nicht verbunden)`;
			select.appendChild(option);
		}

		(groups || []).forEach((group) => {
			const optgroup = document.createElement("optgroup");
			optgroup.label = group.label;
			group.choices.forEach((choice) => {
				const option = document.createElement("option");
				option.value = choice.value;
				option.textContent = choice.label;
				optgroup.appendChild(option);
			});
			select.appendChild(optgroup);
		});

		select.value = selectedValue || "";
	},

	/**
	 * Update printer status helper text
	 * @param {object|null} printer
	 * @param {string|null} message
	 */
	updatePrinterStatus: function (printer, message = null, target = "label") {
		const el =
			target === "documents"
				? this.elements.documentPrinterStatus
				: this.elements.printerStatus;
		if (!el) return;
		if (message) {
			el.textContent = message;
			return;
		}
		if (!printer) {
			el.textContent = "";
			return;
		}
		const kind = printer.type || "cups";
		if (kind === "pdf") {
			el.textContent = "über den Druckdialog des Browsers";
		} else if (kind === "bridge") {
			el.textContent = `${printer.printer_name} an der Hardware Bridge`;
		} else {
			el.textContent = `${printer.printer_name} → ${printer.server_ip}:${printer.port}`;
		}
	},

	/**
	 * Toggle loading state for printer controls
	 * @param {boolean} isLoading
	 */
	setPrinterLoading: function (isLoading) {
		if (this.elements.printerSelect) {
			this.elements.printerSelect.disabled = isLoading;
		}
		if (this.elements.refreshPrintersBtn) {
			if (isLoading) {
				if (!this.elements.refreshPrintersBtn.dataset.originalHtml) {
					this.elements.refreshPrintersBtn.dataset.originalHtml =
						this.elements.refreshPrintersBtn.innerHTML;
				}
				this.elements.refreshPrintersBtn.innerHTML =
					'<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>Lade …';
				this.elements.refreshPrintersBtn.disabled = true;
			} else {
				if (this.elements.refreshPrintersBtn.dataset.originalHtml) {
					this.elements.refreshPrintersBtn.innerHTML =
						this.elements.refreshPrintersBtn.dataset.originalHtml;
					delete this.elements.refreshPrintersBtn.dataset.originalHtml;
				}
				this.elements.refreshPrintersBtn.disabled = false;
			}
		}
	},

	/**
	 * Enable or disable the print button (unless loading)
	 * @param {boolean} enabled
	 */
	setPrintButtonEnabled: function (enabled) {
		const btn = this.elements.printLabelBtn;
		if (!btn) return;
		if (btn.dataset.originalHtml) return; // currently loading
		btn.disabled = !enabled;
	},

	/**
	 * Enable or disable the customs-documents print button (unless loading)
	 * @param {boolean} enabled
	 */
	setDocumentsButtonEnabled: function (enabled) {
		const btn = this.elements.printDocumentsBtn;
		if (!btn) return;
		if (btn.dataset.originalHtml) return; // currently loading
		btn.disabled = !enabled;
	},

	/**
	 * Set loading state for the customs-documents print button
	 * @param {boolean} isLoading
	 * @param {string} [text]
	 */
	setDocumentsButtonLoading: function (isLoading, text = "Drucke …") {
		const btn = this.elements.printDocumentsBtn;
		if (!btn) return;
		if (isLoading) {
			if (!btn.dataset.originalHtml) {
				btn.dataset.originalHtml = btn.innerHTML;
			}
			btn.disabled = true;
			btn.innerHTML = `<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>${text}`;
		} else if (btn.dataset.originalHtml) {
			btn.innerHTML = btn.dataset.originalHtml;
			delete btn.dataset.originalHtml;
		}
	},

	/**
	 * Set loading state for print button
	 * @param {boolean} isLoading
	 * @param {string} [text]
	 */
	setPrintButtonLoading: function (isLoading, text = "Drucke …") {
		const btn = this.elements.printLabelBtn;
		if (!btn) return;
		if (isLoading) {
			if (!btn.dataset.originalHtml) {
				btn.dataset.originalHtml = btn.innerHTML;
			}
			btn.disabled = true;
			btn.innerHTML = `<span class="spinner-border spinner-border-sm me-2" role="status" aria-hidden="true"></span>${text}`;
		} else if (btn.dataset.originalHtml) {
			btn.innerHTML = btn.dataset.originalHtml;
			btn.disabled = false;
			delete btn.dataset.originalHtml;
		}
	},

	/**
	 * Device chip in the header.
	 * @param {"scale"|"label"|"documents"} which
	 * @param {"ok"|"warn"|"error"|"idle"} state
	 * @param {string} text
	 * @param {string} [title]
	 */
	setChip: function (which, state, text, title) {
		const chip = {
			scale: this.elements.chipScale,
			label: this.elements.chipLabelPrinter,
			documents: this.elements.chipDocumentPrinter,
		}[which];
		if (!chip) return;
		chip.dataset.state = state;
		chip.querySelector(".ps-chip-text").textContent = text;
		chip.title = title || text;
	},

	/**
	 * Printer chips follow the selection made in the printer panel.
	 * @param {object|null} labelPrinter
	 * @param {object|null} documentPrinter
	 */
	renderPrinterChips: function (labelPrinter, documentPrinter, bridgeDevice) {
		const kind = labelPrinter ? labelPrinter.type || "cups" : null;
		if (!labelPrinter) {
			this.setChip("label", "warn", "Labeldrucker nicht gewählt", "Labeldrucker wählen");
		} else if (kind === "pdf") {
			// Works, but every label needs the dialog: worth seeing at a glance.
			this.setChip("label", "idle", "Label: PDF (Druckdialog)", "Labels kommen als PDF und werden über den Druckdialog gedruckt");
		} else if (kind === "bridge") {
			const online = bridgeDevice && bridgeDevice.state === "online";
			this.setChip(
				"label",
				online ? "ok" : "error",
				online ? `Label: ${labelPrinter.printer_name}` : `Label: ${labelPrinter.printer_name} nicht erreichbar`,
				online
					? "Labeldrucker an der Hardware Bridge dieses PCs"
					: (bridgeDevice && bridgeDevice.message) || "Hardware Bridge oder Drucker nicht erreichbar"
			);
		} else {
			this.setChip("label", "ok", `Label: ${labelPrinter.printer_name}`, "Labeldrucker am Druckserver");
		}
		// Customs papers are rare; a missing printer is a note, not an alarm.
		const docKind = documentPrinter ? documentPrinter.type || "cups" : null;
		this.setChip(
			"documents",
			documentPrinter && docKind !== "pdf" ? "ok" : "idle",
			!documentPrinter
				? "A4-Drucker nicht gewählt"
				: docKind === "pdf"
				? "A4: Druckdialog"
				: `A4: ${documentPrinter.printer_name}`,
			"Drucker für Zolldokumente wählen"
		);
	},

	/**
	 * Hand a PDF to the browser's print dialog. The document is loaded into a
	 * hidden frame and printed from there; nothing about it is changed.
	 * @param {string} base64 - the PDF as stored on the server
	 */
	printPdf: function (base64) {
		const binary = atob(base64);
		const bytes = new Uint8Array(binary.length);
		for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
		const url = URL.createObjectURL(new Blob([bytes], { type: "application/pdf" }));

		const previous = document.getElementById("ps-print-frame");
		if (previous) previous.remove();
		const frame = document.createElement("iframe");
		frame.id = "ps-print-frame";
		frame.title = "Druckvorschau";
		frame.setAttribute("aria-hidden", "true");
		frame.style.cssText = "position: fixed; right: 0; bottom: 0; width: 0; height: 0; border: 0;";
		frame.addEventListener("load", () => {
			try {
				frame.contentWindow.focus();
				frame.contentWindow.print();
			} catch (error) {
				// A browser that will not print a framed PDF: show it instead,
				// printing is one click away there.
				window.open(url, "_blank", "noopener");
			}
		});
		frame.src = url;
		document.body.appendChild(frame);
		// Long enough for the dialog; the blob must outlive the print job.
		setTimeout(() => URL.revokeObjectURL(url), 10 * 60 * 1000);
	},

	/**
	 * Open or close one of the header panels; opening one closes the other.
	 * @param {"printers"|"fedex"|null} name - null closes both
	 */
	togglePanel: function (name) {
		const panels = { printers: this.elements.printerPanel, fedex: this.elements.fedexPickupCard };
		Object.entries(panels).forEach(([key, panel]) => {
			if (!panel) return;
			panel.hidden = key === name ? !panel.hidden : true;
		});
		if (this.elements.fedexBtn && panels.fedex) {
			this.elements.fedexBtn.setAttribute("aria-expanded", String(!panels.fedex.hidden));
		}
	},

	/**
	 * Parcel dimensions block in the footer.
	 * @param {object|null} dims - {length, width, height} in mm plus source
	 *   "scan" | "manual"; null = nothing yet
	 * @param {boolean} manualOpen - the three input fields are shown
	 */
	renderDimensions: function (dims, manualOpen) {
		const el = this.elements;
		if (!el.dims) return;
		el.dimsManual.hidden = !manualOpen;
		el.dimsScan.hidden = manualOpen;
		el.dimsScanBtn.hidden = !manualOpen;
		el.dimsPill.hidden = !manualOpen && !dims;
		if (manualOpen) {
			el.dims.dataset.state = "manual";
			el.dimsPill.textContent = "von Hand";
			el.dimsPill.dataset.tone = "";
			return;
		}
		if (dims) {
			const cm = (mm) => (mm / 10).toLocaleString("de-AT", { maximumFractionDigits: 1 });
			el.dims.dataset.state = "set";
			el.dimsValue.textContent = `${cm(dims.length)} × ${cm(dims.width)} × ${cm(dims.height)} cm`;
			el.dimsPill.textContent = dims.source === "manual" ? "von Hand" : "gescannt";
			el.dimsPill.dataset.tone = "ok";
			el.dimsManualBtn.textContent = "Ändern";
		} else {
			el.dims.dataset.state = "empty";
			el.dimsValue.textContent = "Karton scannen";
			el.dimsManualBtn.textContent = "Von Hand eingeben";
		}
	},

	/**
	 * Read the three manual dimension fields (centimetres).
	 * @returns {{values: object|null, complete: boolean, invalid: boolean}}
	 *   values in cm when all three are positive numbers
	 */
	readManualDimensions: function () {
		const fields = {
			length: this.elements.dimsLength,
			width: this.elements.dimsWidth,
			height: this.elements.dimsHeight,
		};
		const values = {};
		let filled = 0;
		let invalid = false;
		Object.entries(fields).forEach(([key, input]) => {
			const raw = (input.value || "").trim().replace(",", ".");
			if (!raw) {
				input.removeAttribute("aria-invalid");
				return;
			}
			filled += 1;
			const value = Number(raw);
			if (Number.isFinite(value) && value > 0) {
				values[key] = value;
				input.removeAttribute("aria-invalid");
			} else {
				invalid = true;
				input.setAttribute("aria-invalid", "true");
			}
		});
		const complete = filled === 3 && !invalid;
		return { values: complete ? values : null, complete, invalid, empty: filled === 0 };
	},

	clearManualDimensions: function () {
		[this.elements.dimsLength, this.elements.dimsWidth, this.elements.dimsHeight].forEach((input) => {
			if (!input) return;
			input.value = "";
			input.removeAttribute("aria-invalid");
		});
	},
};
