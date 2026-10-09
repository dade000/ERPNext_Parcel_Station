/**
 * Parcel Station Manager
 * Main orchestration class that coordinates all modules
 */
window.ParcelStationManager = class {
	constructor() {
		this.state = {
			currentDeliveryNote: null,
			items: [],
			selectedItems: new Set(),
			lastShipment: null,
			pendingDeliveryNotes: new Map(),
			printers: [],
			selectedPrinter: null,
			selectedDocumentPrinter: null,
			// Parcel dimensions captured from a dimension barcode scan
			// (@:L:350W:250H:150:@). Set by onDimensionScan, consumed by
			// createShipment, then cleared so they never bleed into a later
			// manual/auto shipment. Typed-in dimensions land here too, converted
			// to the scanner's millimetres and marked source: "manual".
			parcelDimensions: null,
			// The three manual dimension fields are open.
			manualDimensionsOpen: false,
			// Carrier filter of the work list (carrier key or null = all).
			queueFilter: null,
			// Name of a live Shipment of the loaded Delivery Note that never
			// got a label. While set, the button asks for that label again
			// instead of packing a new parcel.
			retryShipment: null,
		};

		this.initialize();
	}

	/**
	 * Initialize the manager
	 */
	initialize() {
		ParcelStationUI.initializeElements();
		const weightCard = document.getElementById("ps-weight-card");
		if (weightCard && window.ParcelStationWeight) {
			this.weight = new ParcelStationWeight(weightCard);
		}
		this.loadPendingFromStorage();
		this.bindEvents();
		this.renderPendingDeliveryNotes();
		this.initializePrinters();
		this.refreshOrdersToPack();
		this.refreshFedexPickup();
		this.renderDimensions();
		// The work list is shared: other stations and the Desk change it too.
		this._queueTimer = setInterval(() => {
			if (!document.hidden && !this._createInFlight) this.refreshOrdersToPack({ silent: true });
		}, 120000);
		ParcelStationUI.focusBarcodeInput();
	}

	/**
	 * Reload the work list (server-side: Delivery Notes with a Carrier Service
	 * and no live, labelled Shipment) and the progress counter.
	 * @param {object} [options] - {silent: true} keeps a failed background
	 *   refresh out of the message line
	 */
	async refreshOrdersToPack(options = {}) {
		if (!ParcelStationUI.elements.queueList) return null;
		try {
			const response = await ParcelStationAPI.listOrdersToPack();
			this.state.ordersToPack = response;
			this.renderQueue();
			return response;
		} catch (error) {
			console.error("Orders to pack failed:", error);
			if (!options.silent) {
				this.state.ordersToPack = null;
				this.renderQueue();
				ParcelStationUI.showMessage(`Arbeitsliste: ${error.message}`, "danger");
			}
			return null;
		}
	}

	/**
	 * Draw the work list from the last response, with the current filter and
	 * the Delivery Note in hand highlighted.
	 */
	renderQueue() {
		const current = this.state.currentDeliveryNote;
		ParcelStationUI.renderQueue(this.state.ordersToPack || null, {
			filter: this.state.queueFilter,
			current: current ? current.delivery_note : null,
			onFilter: (key) => {
				this.state.queueFilter = key;
				this.renderQueue();
			},
			onPick: (deliveryNote) => this.pickFromQueue(deliveryNote),
		});
	}

	/**
	 * A row of the work list was chosen: load it like a typed-in number (no
	 * automation — only a scan may create a label on its own).
	 */
	pickFromQueue(deliveryNote) {
		if (!deliveryNote || this._createInFlight) return;
		ParcelStationUI.elements.barcodeInput.value = deliveryNote;
		this.fetchDeliveryNote(deliveryNote);
		ParcelStationUI.focusBarcodeInput();
	}

	/**
	 * Reload the FedEx pickup card (open shipments + booked pickups).
	 */
	async refreshFedexPickup() {
		if (!ParcelStationUI.elements.fedexPickupCard) return;
		try {
			const overview = await ParcelStationAPI.getFedexPickupOverview();
			this.state.fedexPickup = overview;
			ParcelStationUI.renderFedexPickup(overview);
		} catch (error) {
			console.error("FedEx pickup overview failed:", error);
			ParcelStationUI.renderFedexPickup(null, error.message);
		}
	}

	/**
	 * Book a courier for every open FedEx shipment (the same set the 12:00
	 * run would take). The backend rolls over to the next business day when
	 * it is too late for today.
	 */
	async requestFedexPickup() {
		const btn = ParcelStationUI.elements.fedexPickupRequestBtn;
		const open = (this.state.fedexPickup && this.state.fedexPickup.open_shipments) || [];
		if (!open.length) {
			ParcelStationUI.showMessage("Keine FedEx-Sendung wartet auf Abholung.", "info");
			return;
		}
		if (btn) btn.disabled = true;
		try {
			const result = await ParcelStationAPI.requestFedexPickup(null);
			const pickups = result.pickups || [];
			const failed = pickups.filter((p) => p.status !== "Requested");
			const booked = pickups.filter((p) => p.status === "Requested");
			if (booked.length) {
				const w = result.window || {};
				const codes = booked.map((p) => p.confirmation_code || p.pickup).join(", ");
				ParcelStationUI.showMessage(
					`FedEx-Abholung gebucht für ${w.pickup_date} ${w.ready_time}–${w.close_time} ` +
						`(${result.shipments.length} Sendung(en)). Bestätigung: ${codes}`,
					"success"
				);
			}
			if (failed.length) {
				ParcelStationUI.showMessage(
					`FedEx-Abholung fehlgeschlagen: ${failed.map((p) => p.error).join(" | ")}`,
					"danger"
				);
			}
		} catch (error) {
			console.error("FedEx pickup request failed:", error);
			ParcelStationUI.showMessage(`FedEx-Abholung fehlgeschlagen: ${error.message}`, "danger");
		} finally {
			await this.refreshFedexPickup();
		}
	}

	async cancelFedexPickup(pickupName, confirmation) {
		const label = confirmation ? `${pickupName} (Bestätigung ${confirmation})` : pickupName;
		if (!window.confirm(`FedEx-Abholung ${label} stornieren? Der Kurier kommt dann nicht.`)) return;
		try {
			const result = await ParcelStationAPI.cancelFedexPickup(pickupName, "Cancelled in Parcel Station");
			ParcelStationUI.showMessage(
				`FedEx-Abholung ${pickupName} storniert${result.message ? `: ${result.message}` : "."}`,
				"success"
			);
		} catch (error) {
			console.error("FedEx pickup cancel failed:", error);
			ParcelStationUI.showMessage(`Storno fehlgeschlagen: ${error.message}`, "danger");
		} finally {
			await this.refreshFedexPickup();
		}
	}

	/**
	 * Load pending delivery notes from storage
	 */
	loadPendingFromStorage() {
		this.state.pendingDeliveryNotes = ParcelStationStorage.loadPendingDeliveryNotes();
	}

	/**
	 * Save pending delivery notes to storage
	 */
	savePendingToStorage() {
		ParcelStationStorage.savePendingDeliveryNotes(this.state.pendingDeliveryNotes);
	}

	/**
	 * Route a barcode-input value. Only marker-wrapped values (@: ... :@) are
	 * treated as hardware SCANS and may drive automation. Anything else is a
	 * manual entry and keeps the legacy behaviour (Delivery Note lookup only,
	 * never auto-creates a shipment) — satisfying the "manual flow unchanged"
	 * rule.
	 *
	 * Scan routing:
	 *   @:MAT-DN-2026-00156:@      -> Delivery Note scan  -> fetch + show items
	 *   @:L:350W:250H:150:@        -> Dimension scan      -> parse + AUTO create
	 *
	 * @param {string} raw - the raw input/scan value
	 * @param {boolean} fromScan - true when fired from the scan path
	 */
	handleInput(raw, fromScan) {
		const value = (raw || "").trim();
		if (!value) return;

		// A complete scan is wrapped in @: :@. Extract every such token and take
		// the LAST one (a scanner appends a new scan to whatever is already in
		// the field). IMPORTANT: we NEVER write back to the input here — the
		// field stays a normal, fully-editable text box; typing/backspace are
		// untouched. The scanned values remain visible simply because we don't
		// clear them until the shipment is created (clearCurrentSelection).
		const tokens = value.match(/@:.*?:@/g);

		if (!tokens || !tokens.length) {
			// No markers => manual entry. Legacy behaviour: Delivery Note lookup
			// only, never auto-creates. Dedup so keypress + debounce don't double-run.
			if (this._isDuplicateScan(value)) return;
			this.fetchDeliveryNote(value);
			return;
		}

		const token = tokens[tokens.length - 1];
		const inner = token.slice(2, -2).trim(); // strip leading @: and trailing :@
		const dims = this.parseDimensions(inner);

		// De-duplicate the SAME scan within a short window: the input is bound to
		// BOTH a keypress(Enter) and a debounced input handler (and a scanner
		// queues several input timers), so without this one scan would be
		// processed twice. This also means re-editing the field (backspace,
		// typing) won't re-fire the same scan.
		if (this._isDuplicateScan(token)) return;

		if (dims) {
			this.onDimensionScan(dims);
		} else {
			// New DN scan = fresh shipment session: drop any leftover dimension
			// from a previous (e.g. failed) attempt so it can't auto-create with
			// the new DN. Then resolve the DN id (markers already stripped).
			this.state.parcelDimensions = null;
			this.fetchDeliveryNote(inner);
		}
	}

	/**
	 * Returns true if this exact value was already handled within the last
	 * 1.5s (i.e. it's the twin handler / a queued scanner timer firing again).
	 */
	_isDuplicateScan(value) {
		const now = Date.now();
		if (this._lastHandledValue === value && now - (this._lastHandledTs || 0) < 1500) {
			return true;
		}
		this._lastHandledValue = value;
		this._lastHandledTs = now;
		return false;
	}

	/**
	 * Parse a dimension barcode payload like "L:350W:250H:150" (markers already
	 * stripped). Returns {length, width, height} or null if it isn't a
	 * dimension barcode.
	 * @param {string} inner
	 */
	parseDimensions(inner) {
		const m = (inner || "").match(
			/L\s*:\s*(\d+(?:\.\d+)?)\s*W\s*:\s*(\d+(?:\.\d+)?)\s*H\s*:\s*(\d+(?:\.\d+)?)/i
		);
		if (!m) return null;
		return {
			length: parseFloat(m[1]),
			width: parseFloat(m[2]),
			height: parseFloat(m[3]),
		};
	}

	/**
	 * Handle a dimension scan: store the parcel dimensions and, if a Delivery
	 * Note is already loaded, AUTOMATICALLY create the shipment (no manual
	 * click). If no DN is loaded yet, keep the dimensions and prompt the
	 * operator to scan the Delivery Note.
	 * @param {{length:number,width:number,height:number}} dims
	 */
	async onDimensionScan(dims) {
		// A scan beats whatever was typed: the carton's own barcode is the
		// better source, and the fields would otherwise disagree with it.
		this.setDimensions({ ...dims, source: "scan" });
		ParcelStationUI.showMessage(
			// Raw scanner values are millimetres; the backend converts them to the
			// centimetres that ERPNext and the carrier expect.
			`Kartonmaße gescannt: ${dims.length} × ${dims.width} × ${dims.height} mm`,
			"info"
		);

		if (!this.state.currentDeliveryNote) {
			ParcelStationUI.showMessage(
				"Kartonmaße gemerkt. Jetzt den Lieferschein scannen.",
				"warning"
			);
			return;
		}

		// Auto-trigger shipment creation (no manual click required).
		await this.createShipment({ auto: true });
	}

	/**
	 * Set or clear the parcel dimensions and redraw the block. Clearing also
	 * closes and empties the manual fields.
	 * @param {object|null} dims - {length, width, height} in mm, source
	 */
	setDimensions(dims) {
		this.state.parcelDimensions = dims;
		if (!dims || dims.source !== "manual") {
			this.state.manualDimensionsOpen = false;
			ParcelStationUI.clearManualDimensions();
		}
		this.renderDimensions();
	}

	renderDimensions() {
		ParcelStationUI.renderDimensions(this.state.parcelDimensions, this.state.manualDimensionsOpen);
	}

	/** "Von Hand eingeben" / "Ändern": show the three fields. */
	openManualDimensions() {
		const dims = this.state.parcelDimensions;
		const el = ParcelStationUI.elements;
		if (dims) {
			// Start from what is there (a scan the operator wants to correct).
			const cm = (mm) => String(mm / 10).replace(".", ",");
			el.dimsLength.value = cm(dims.length);
			el.dimsWidth.value = cm(dims.width);
			el.dimsHeight.value = cm(dims.height);
		}
		this.state.manualDimensionsOpen = true;
		this.syncManualDimensions();
		el.dimsLength.focus();
		el.dimsLength.select();
	}

	/**
	 * Take the typed centimetres over as the parcel's dimensions. The server
	 * expects the scanner's millimetres, so the values are converted here and
	 * the scan path stays the only one the backend knows.
	 */
	syncManualDimensions() {
		const read = ParcelStationUI.readManualDimensions();
		this.state.parcelDimensions = read.values
			? {
					length: Math.round(read.values.length * 10),
					width: Math.round(read.values.width * 10),
					height: Math.round(read.values.height * 10),
					source: "manual",
			  }
			: null;
		this.renderDimensions();
		return read;
	}

	/**
	 * Select every loaded item (used by the automatic flow so a dimension scan
	 * can create a shipment for the whole Delivery Note without manual ticking).
	 */
	selectAllItems() {
		this.state.items.forEach((item) => {
			this.state.selectedItems.add(ParcelStationTemplates.rowKey(item));
		});
		this.renderItems();
	}

	/**
	 * Fetch delivery note data
	 * @param {string} barcode - The barcode to lookup
	 */
	async fetchDeliveryNote(barcode) {
		// Guard against duplicate triggers for a single scan. The barcode input
		// is bound to BOTH an Enter (keypress) handler and a debounced (input)
		// handler, and hardware scanners fire both — without this guard one scan
		// would fetch and render the items twice (the duplicate-rows symptom).
		if (this._fetchInFlight) return;
		this._fetchInFlight = true;

		// Clear any previously-loaded delivery note IMMEDIATELY (before the async
		// fetch) so the items table is reset on every new scan and never shows
		// stale rows from the prior delivery note while the new one loads.
		this.state.currentDeliveryNote = null;
		this.state.items = [];
		this.state.selectedItems.clear();
		this.state.lastShipment = null;
		this.setRetryShipment(null);
		// Dimensions belong to one parcel: a new Delivery Note starts without.
		this.setDimensions(null);
		ParcelStationUI.resetShipmentInfo();
		this.updatePrintButtonState();
		this.renderItems(); // re-render with empty state -> clears the table

		try {
			ParcelStationUI.showMessage("Lieferschein wird geladen …", "info");

			const deliveryNoteData = await ParcelStationAPI.fetchDeliveryNote(barcode);

			// Debug: surface the loaded DN data so QA/devs can trace what the
			// backend handed back before the operator picks items / clicks
			// Create Shipment. Pretty-print as JSON so the whole structure is
			// visible inline in the console — no need to expand chevrons.
			console.log("[Parcel Station] Delivery Note selected:", deliveryNoteData.delivery_note);
			console.log("[Parcel Station] Customer:", deliveryNoteData.delivery_to || "N/A");
			console.log(
				"[Parcel Station] Parcel Shop:",
				deliveryNoteData.parcel_shop_id || "(none — home delivery)"
			);
			console.log(
				"[Parcel Station] Delivery Note payload:\n" +
				 JSON.stringify(deliveryNoteData, null, 2)
			);

			this.state.currentDeliveryNote = deliveryNoteData;
			this.state.items = deliveryNoteData.items || [];
			this.state.lastShipment = null;
			ParcelStationUI.resetShipmentInfo();
			this.updatePrintButtonState();

			// If this DN already has Shipments with tracking/label data, surface
			// the most recent one in the Tracking + Label cards so the operator
			// doesn't have to re-run Create Shipment to see what's already on
			// file. Backend (fetch_delivery_note_for_parcel) now returns
			// awb_number + label_file_url per shipment.
			const existingShipments = deliveryNoteData.shipments || [];
			if (existingShipments.length > 0) {
				const latest = existingShipments[0];
				const tracking = latest.tracking_number || latest.awb_number || null;
				const labelUrl = latest.label_file_url || null;
				if (!tracking && latest.docstatus === 1) {
					// The label call failed last time. The Shipment is kept on
					// purpose; the button now finishes it.
					this.setRetryShipment(latest.name);
					this.state.lastShipment = { shipment: latest.name, tracking_codes: [] };
					ParcelStationUI.updateShipmentInfo(this.state.lastShipment);
					ParcelStationUI.setResultState("problem", "Sendung ohne Label");
				} else if (tracking || labelUrl) {
					this.state.lastShipment = {
						shipment: latest.name,
						tracking_codes: tracking ? [tracking] : [],
						label_file_url: labelUrl,
						label_format: latest.label_format || null,
					};
					ParcelStationUI.updateShipmentInfo(this.state.lastShipment);
					ParcelStationUI.setResultState("existing", "Zu diesem Lieferschein gibt es schon eine Sendung");
					this.updatePrintButtonState();
				}
			}

			this.renderItems();
			ParcelStationUI.highlightQueueRow(deliveryNoteData.delivery_note);
			if (this.state.retryShipment) {
				ParcelStationUI.showMessage(
					`${this.state.retryShipment} hat noch kein Label. Ursache beheben, dann „Label erneut anfordern“.`,
					"warning"
				);
			} else if (deliveryNoteData.all_items_allocated) {
				ParcelStationUI.showMessage(
					`${deliveryNoteData.delivery_note}: alle Positionen sind bereits versendet.`,
					"warning"
				);
			} else {
				ParcelStationUI.hideMessage();
			}
		} catch (error) {
			ParcelStationUI.showMessage(`Lieferschein konnte nicht geladen werden: ${error.message}`, "danger");
			console.error("Error:", error);
		} finally {
			this._fetchInFlight = false;
		}
	}

	/**
	 * Render items list
	 */
	renderItems() {
		ParcelStationUI.renderItems(this.state.items, this.state.currentDeliveryNote, () => {
			this.updateSelectedItems();
			this.updateSelectAllCheckbox();
		});
		ParcelStationUI.setPackMode(this.packMode());

		// Initialize selected items (all checked by default)
		this.state.selectedItems.clear();
		this.state.items.forEach((item) => {
			this.state.selectedItems.add(ParcelStationTemplates.rowKey(item));
		});
	}

	/**
	 * What the button does for the loaded Delivery Note:
	 *   "retry"   a Shipment without label exists — ask for the label again
	 *   "done"    everything is shipped and labelled — nothing to create
	 *   "pack"    the normal case
	 */
	packMode() {
		const dn = this.state.currentDeliveryNote;
		if (!dn) return "pack";
		if (this.state.retryShipment) return "retry";
		return dn.all_items_allocated ? "done" : "pack";
	}

	setRetryShipment(name) {
		this.state.retryShipment = name || null;
		ParcelStationUI.setPackMode(this.packMode());
	}

	/** Button back to its resting state after a run, in the current mode. */
	resetCreateButton() {
		ParcelStationUI.updateButtonState("", false);
		ParcelStationUI.setPackMode(this.packMode());
	}

	/**
	 * Update selected items from checkboxes
	 */
	updateSelectedItems() {
		this.state.selectedItems.clear();
		ParcelStationUI.elements.itemsList
			.querySelectorAll(".form-check-input:checked")
			.forEach((checkbox) => {
				this.state.selectedItems.add(checkbox.dataset.rowKey);
			});
		ParcelStationUI.elements.createShipmentBtn.disabled =
			this.packMode() === "done" || this.state.selectedItems.size === 0;
		ParcelStationUI.setPacklistHint(this.state.selectedItems.size, this.state.items.length);
	}

	/**
	 * Update select all checkbox (placeholder for future implementation)
	 */
	updateSelectAllCheckbox() {
		// Implementation for select all checkbox if needed
	}

	/**
	 * Create shipment from selected items
	 */
	async createShipment(options = {}) {
		const auto = options.auto === true;
		console.log(`[Parcel Station] Create Shipment triggered (auto=${auto})`);

		if (!this.state.currentDeliveryNote) {
			ParcelStationUI.showMessage("Bitte zuerst einen Lieferschein scannen.", "warning");
			return;
		}

		const retry = this.packMode() === "retry";
		if (this.packMode() === "done") {
			// Nothing left to pack. Creating again would only hand back the
			// existing Shipment and print its label a second time.
			ParcelStationUI.showMessage(
				"Alle Positionen dieses Lieferscheins sind bereits versendet. Zum Nachdrucken „Label drucken“.",
				"warning"
			);
			return;
		}

		// In the automatic (scan-driven) flow, no manual ticking happens — create
		// the shipment for the whole Delivery Note by selecting all items.
		if (auto && this.state.selectedItems.size === 0) {
			this.selectAllItems();
		}

		if (this.state.selectedItems.size === 0) {
			ParcelStationUI.showMessage("Bitte mindestens eine Position auswählen.", "warning");
			return;
		}

		// Re-entrancy guard: a hardware scanner fires both keypress + debounced
		// input handlers, so without this one dimension scan could trigger two
		// concurrent create calls (duplicate shipments).
		if (this._createInFlight) return;

		// Half-typed dimensions must not be dropped silently: the parcel would
		// ship without, and the carrier bills by what it measures.
		if (!retry && this.state.manualDimensionsOpen) {
			const read = this.syncManualDimensions();
			if (!read.complete && !read.empty) {
				ParcelStationUI.showMessage(
					"Kartonmaße unvollständig: Länge, Breite und Höhe in cm eingeben – oder die Felder leeren.",
					"warning"
				);
				return;
			}
		}
		this._createInFlight = true;

		try {
			// Step 0: Weight. Live scale (stable), typed-in value, or null
			// (server decides). Refuses rather than guessing — see
			// ParcelStationWeight.resolveForShipment.
			// A retry finishes the Shipment as it was packed: its weight and
			// dimensions are on record, nothing is measured again.
			let weight = null;
			if (this.weight && !retry) {
				ParcelStationUI.updateButtonState("Lese Gewicht …", true);
				try {
					weight = await this.weight.resolveForShipment();
				} catch (weightErr) {
					ParcelStationUI.showMessage(weightErr.message, "warning");
					this.resetCreateButton();
					return;
				}
			}

			// Step 1: Creating Shipment
			ParcelStationUI.updateButtonState(retry ? "Fordere Label an …" : "Lege Sendung an …", true);
			ParcelStationUI.showMessage(
				retry ? "Label wird erneut angefordert …" : "Sendung wird angelegt …",
				"info"
			);

			// Prepare selected items for API
			const selectedItemsData = this.state.items
				.filter((item) => this.state.selectedItems.has(ParcelStationTemplates.rowKey(item)))
				.map((item) => ({
					// A bundle component is addressed by its Packed Item row;
					// delivery_note_item is then the bundle line it belongs to.
					delivery_note_item: item.delivery_note_item || item.name,
					packed_item: item.packed_item || undefined,
					qty: parseFloat(item.available_qty || item.qty || 0),
				}));

			// Debug: dump the exact wire payload going to the backend
			// create_shipment_from_barcode RPC, so a failed Shipment can be
			// reproduced from the console output alone. The summary block
			// below is for human eyeballs; the wire payload is what the
			// backend actually receives.
			// Use the LOADED Delivery Note id as the barcode — never the raw
			// input field, which after a dimension scan holds the dimension
			// barcode, not the DN.
			const barcodeArg =
				this.state.currentDeliveryNote.delivery_note ||
				ParcelStationUI.elements.barcodeInput.value;
			const dimensions = this.state.parcelDimensions || null;
			const wirePayload = { barcode: barcodeArg, items: selectedItemsData, dimensions, weight };
			const summary = {
				delivery_note: this.state.currentDeliveryNote.delivery_note,
				customer: this.state.currentDeliveryNote.delivery_to,
				parcel_shop_id: this.state.currentDeliveryNote.parcel_shop_id || null,
				selected_count: selectedItemsData.length,
				dimensions,
				weight,
			};
			console.log("[Parcel Station] Summary:\n" + JSON.stringify(summary, null, 2));
			console.log(
				"[Parcel Station] Payload (POST -> create_shipment_from_barcode):\n" +
				JSON.stringify(wirePayload, null, 2)
			);

			// The format follows the printer selected here: a label is issued
			// once, so it has to fit what this station can print.
			const labelFormat = this.labelFormatForPrinter();
			const shipmentData = await ParcelStationAPI.createShipment(
				barcodeArg,
				selectedItemsData,
				dimensions,
				weight,
				labelFormat
			);
			// A typed-in weight belongs to this parcel only.
			if (this.weight) this.weight.clearManual();

			console.log(
				"[Parcel Station] Backend response Scale API:\n" + JSON.stringify(shipmentData, null, 2)
			);

			// Debug: ask the backend for the exact GLS API JSON body that
			// will be POSTed to /backend/rs/shipments. preview_gls_payload
			// is read-only — it builds the payload via the same _build_payload
			// helper the real call uses, but does not hit GLS. Failure here
			// (carrier not configured, GLS Settings missing, etc.) must NOT
			// break the actual shipment flow — we log and move on.
			if (shipmentData && shipmentData.shipment) {
				try {
					const preview = await ParcelStationAPI.previewGlsPayload(shipmentData.shipment);
					if (preview && preview.payload) {
						console.log(
							"[Parcel Station] GLS API payload (POST -> " +
							"/backend/rs/shipments):\n" +
							JSON.stringify(preview.payload, null, 2)
						);
						if (preview.services && preview.services.length) {
							console.log(
								"[Parcel Station] GLS services:",
								preview.services.join(", ")
							);
						}
					} else {
						console.warn(
							"[Parcel Station] GLS payload preview unavailable " +
							"(backend returned no payload)."
						);
					}
				} catch (previewErr) {
					console.warn(
						"[Parcel Station] GLS payload preview failed:",
						previewErr && previewErr.message ? previewErr.message : previewErr
					);
				}

				// Austrian Post: mirror the GLS debug logging. The SOAP payload is
				// assembled server-side, so there is no client payload to intercept;
				// instead we log the values being transmitted — chiefly the Service
				// Code, which the backend sends verbatim as DeliveryServiceThirdPartyID.
				// Read-only, client-side only, uses the built-in frappe data API (no
				// new backend API), and never alters the request.
				try {
					await this.logAustrianPostPayload(shipmentData);
				} catch (apErr) {
					console.warn(
						"[Parcel Station] Austrian Post payload log failed:",
						apErr && apErr.message ? apErr.message : apErr
					);
				}

				// FedEx: same read-only payload preview as GLS. The backend
				// redacts the account number before returning it, so the exact
				// wire body can be logged without leaking billing data.
				try {
					const fedexPreview = await ParcelStationAPI.previewFedexPayload(
						shipmentData.shipment
					);
					if (fedexPreview && fedexPreview.payload) {
						console.log(
							"[Parcel Station] FedEx Ship API payload (" +
							fedexPreview.mode + ", POST -> /ship/v1/shipments):\n" +
							JSON.stringify(fedexPreview.payload, null, 2)
						);
					}
				} catch (fedexErr) {
					console.warn(
						"[Parcel Station] FedEx payload preview failed:",
						fedexErr && fedexErr.message ? fedexErr.message : fedexErr
					);
				}
			}

			// Step 2: Shipment Created Successfully
			this.state.lastShipment = shipmentData;
			ParcelStationUI.updateShipmentInfo(shipmentData);
			ParcelStationUI.setResultState("working", "Sendung angelegt – Label wird angefordert …");
			this.refreshFedexPickup();
			this.refreshOrdersToPack({ silent: true });
			ParcelStationUI.updateButtonState("Fordere Label an …", true);

			// Scale read happened server-side as part of shipment creation.
			// Tell the operator which weight source was used so a silently
			// offline scale doesn't ship parcels at fallback weight.
			const scale = shipmentData && shipmentData.scale;
			const weightKg = shipmentData && shipmentData.weight_kg;
			const weightSource = shipmentData && shipmentData.weight && shipmentData.weight.source;
			let weightNote = "";
			if (weightSource === "client_manual") {
				weightNote = ` (Gewicht von Hand: ${weightKg} kg)`;
			} else if (scale && scale.status === "success") {
				weightNote = ` (Waage: ${scale.weight_kg} kg)`;
			} else if (scale && scale.code) {
				weightNote = ` (Achtung: Waage ${scale.code} – es gelten ${weightKg} kg als Ersatzwert)`;
				console.warn("[Parcel Station] Scale read failed:", scale);
			}

			ParcelStationUI.showMessage(
				retry
					? `Label für ${shipmentData.shipment} wird erneut angefordert …`
					: `Sendung ${shipmentData.shipment} angelegt${weightNote}. Label wird angefordert …`,
				"success"
			);
			this.updatePrintButtonState();

			// Handle pending items. A retry packs nothing new, so the list of
			// started Delivery Notes stays as it is.
			if (!retry) this.handlePendingItems();

			// Step 3: Request Label
			await this.requestLabel(shipmentData.shipment);

			// The label prints by itself, whether the parcel was finished by a
			// carton scan or by the button. Best-effort: never blocks shipment
			// completion, and at most once per generated label (autoPrintLabel).
			const autoPrintOutcome = await this.autoPrintLabel();
			// Customs papers follow the label on the A4 printer. Only customs
			// destinations return them, so this is a no-op for AT/EU parcels.
			const autoDocumentOutcome = await this.autoPrintCustomsDocuments();

			// Step 4: Complete — only declare full success when the carrier
			// pipeline actually produced BOTH a tracking number AND a
			// downloadable/printable label. A shipment can be created while the
			// label/tracking call partially fails; in that case we must NOT show
			// the green "completed successfully" state (it would be a false
			// success). Use the merged lastShipment (create response + label
			// response) as the source of truth.
			this.resetCreateButton();

			const finalShipment = this.state.lastShipment || shipmentData;
			const trackingOk = Boolean(
				(finalShipment.tracking_codes && finalShipment.tracking_codes.length) ||
				finalShipment.tracking_number ||
				finalShipment.awb_number
			);
			const labelOk = this.hasLabelAvailable();
			const problem = (text, type) => {
				ParcelStationUI.setResultState("problem", "Sendung angelegt, aber nicht fertig");
				ParcelStationUI.showMessage(text, type || "warning");
			};
			// Without a label the Delivery Note stays in hand: the cause can be
			// fixed and the label requested again, without scanning anew.
			const keepInHand = !labelOk || !trackingOk;

			if (!labelOk && this.state.lastLabelError) {
				// A real carrier/API error was captured during label generation —
				// show it verbatim. Do NOT overwrite it with a generic summary.
				problem(this.state.lastLabelError);
			} else if (!trackingOk && !labelOk) {
				problem("Sendung angelegt, aber der Carrier hat weder Sendungsnummer noch Label geliefert.");
			} else if (!labelOk) {
				problem("Sendung angelegt, aber der Carrier hat kein Label geliefert.");
			} else if (!trackingOk) {
				problem("Sendung angelegt, aber der Carrier hat keine Sendungsnummer geliefert.");
			} else if (autoDocumentOutcome === "failed") {
				ParcelStationUI.setResultState("problem", "Label erstellt – Zolldokumente nicht gedruckt");
				ParcelStationUI.showMessage(
					"Der automatische Druck der Zolldokumente ist fehlgeschlagen. Mit „Zolldokumente drucken“ erneut versuchen.",
					"danger"
				);
			} else if (autoPrintOutcome === "failed") {
				// Full pipeline OK, but don't bury an auto-print failure under a
				// generic success note — the detailed reason is also persisted in
				// the printer-status line.
				ParcelStationUI.setResultState("problem", "Label erstellt – nicht gedruckt");
				ParcelStationUI.showMessage(
					"Der automatische Druck ist fehlgeschlagen. Mit „Label drucken“ erneut versuchen.",
					"danger"
				);
			} else if (autoPrintOutcome === "printed") {
				ParcelStationUI.setResultState("done", "Fertig – Label ist gedruckt");
				ParcelStationUI.showMessage("Fertig. Nächsten Lieferschein scannen.", "success");
			} else if (autoPrintOutcome === "dialog") {
				// printLabel has set headline and note: the dialog is open.
			} else {
				// No printer chosen at this station (autoPrintLabel has said so).
				ParcelStationUI.setResultState("done", "Label erstellt – noch nicht gedruckt");
			}

			// Dimensions were consumed by this shipment — clear so they never
			// bleed into the next one.
			this.setDimensions(null);

			if (keepInHand) {
				this.setRetryShipment(finalShipment.shipment);
			} else {
				this.setRetryShipment(null);
				// Clear current selection
				this.clearCurrentSelection();
			}
		} catch (error) {
			// Surface a user-friendly reason in the panel below the Create
			// Shipment button; full technical detail stays in the console.
			ParcelStationUI.showMessage(ParcelStationUI.formatError(error, "shipment"), "danger");
			console.error("Error creating shipment:", error);
			this.resetCreateButton();
			this.updatePrintButtonState();
		} finally {
			this._createInFlight = false;
		}
	}

	/**
	 * Client-side debug log of the Austrian Post shipment payload values.
	 *
	 * Mirrors the GLS payload logging for visibility/debugging. The Austrian Post
	 * SOAP request is assembled on the backend, so this does NOT reconstruct the
	 * raw XML — it reads (read-only, via the built-in frappe data API; no new
	 * backend API) the carrier service + Service Code and logs the values being
	 * transmitted, including the Service Code that the backend sends verbatim as
	 * DeliveryServiceThirdPartyID. Skips GLS shipments. Never alters the request.
	 *
	 * @param {object} shipmentData - The create_shipment_from_barcode response
	 */
	async logAustrianPostPayload(shipmentData) {
		const shipmentName = shipmentData && shipmentData.shipment;
		if (!shipmentName) return;
		if (!(window.frappe && frappe.db && frappe.db.get_value)) {
			console.warn(
				"[Parcel Station] Austrian Post payload log skipped: frappe data API unavailable."
			);
			return;
		}

		// Read the shipment's carrier service + delivery type (read-only).
		// The carrier service can live on EITHER field — mirror the backend's
		// `custom_carrier_service or carrier_service` resolution.
		const shRes = await frappe.db.get_value("Shipment", shipmentName, [
			"custom_carrier_service",
			"carrier_service",
			"delivery_type",
			"awb_number",
		]);
		const sh = (shRes && shRes.message) || {};
		const carrierService = sh.custom_carrier_service || sh.carrier_service;

		// Resolve the carrier + Service Code (this is what becomes the
		// DeliveryServiceThirdPartyID sent to Austrian Post).
		let carrier = null;
		let serviceCode = null;
		if (carrierService) {
			const csRes = await frappe.db.get_value("Carrier Service", carrierService, [
				"carrier",
				"service_code",
			]);
			const cs = (csRes && csRes.message) || {};
			carrier = cs.carrier;
			serviceCode = cs.service_code;
		}

		// Austrian Post only — GLS has its own payload logging above.
		const isGls =
			sh.delivery_type === "Parcelshop Delivery" ||
			(carrier || "").toUpperCase() === "GLS";
		if (isGls) return;

		const dn = this.state.currentDeliveryNote || {};
		const payload = {
			carrier: "Austrian Post (ImportShipment)",
			shipment: shipmentName,
			delivery_note: dn.delivery_note || null,
			carrier_service: carrierService || null,
			// Sent verbatim by the backend as <DeliveryServiceThirdPartyID>.
			DeliveryServiceThirdPartyID: serviceCode,
			recipient: dn.delivery_to || null,
			weight_kg: shipmentData.weight_kg,
		};

		console.log(
			"[Parcel Station] Austrian Post payload (sent to ImportShipment):\n" +
			JSON.stringify(payload, null, 2)
		);
		if (!serviceCode) {
			console.warn(
				"[Parcel Station] Austrian Post: no Service Code configured on Carrier " +
				"Service '" +
				(carrierService || "(none)") +
				"' — DeliveryServiceThirdPartyID would be empty (shipment creation is blocked)."
			);
		}
	}

	/**
	 * Request shipping label
	 * @param {string} shipmentName - The shipment name
	 */
	async requestLabel(shipmentName) {
		// Reset any carrier/API error captured from a previous label attempt so
		// the completion check reflects only this attempt.
		this.state.lastLabelError = null;
		try {
			ParcelStationUI.showMessage("Label wird angefordert …", "info");

			const labelData = await ParcelStationAPI.requestLabel(
				shipmentName,
				this.labelFormatForPrinter()
			);

			if (labelData) {
				this.state.lastShipment = {
					...(this.state.lastShipment || {}),
					...labelData,
				};
				ParcelStationUI.updateShipmentInfo(labelData);
				this.refreshFedexPickup();
				this.refreshOrdersToPack({ silent: true });
				// A label response can come back without an actual downloadable
				// label (carrier returned an error/placeholder). Only call it
				// "ready" when a real label is present; otherwise flag it in the
				// log panel so it isn't mistaken for success.
				if (this.hasLabelAvailable()) {
					ParcelStationUI.showMessage("Label ist da.", "success");
				} else {
					// No downloadable label in the response — surface whatever the
					// carrier actually returned (error / status field), as-is.
					const detail =
						(labelData &&
							(labelData.error || labelData.label_status || labelData.message)) ||
						"";
					const text = detail
						? (typeof detail === "string" ? detail : JSON.stringify(detail))
						: "Der Carrier hat geantwortet, aber kein Label geliefert.";
					this.state.lastLabelError = text;
					ParcelStationUI.showMessage(text, "warning");
				}
			} else {
				ParcelStationUI.showMessage(
					"Label angefordert, aber es liegt noch kein Label vor.",
					"warning"
				);
			}
		} catch (error) {
			console.error("Error requesting label:", error);
			// The shipment itself was already created — keep this non-blocking
			// ("warning") but show the EXACT carrier/API error in the panel below
			// the Create Shipment button, and remember it so the completion check
			// re-displays the real reason rather than overwriting it with a
			// generic summary.
			const text = ParcelStationUI.formatError(error, "label");
			this.state.lastLabelError = text;
			ParcelStationUI.showMessage(text, "warning");
		} finally {
			this.updatePrintButtonState();
		}
	}

	/**
	 * Handle pending items after shipment creation
	 */
	handlePendingItems() {
		// Check for pending items and update pending delivery notes
		const unselectedItems = this.state.items.filter(
			(item) => !this.state.selectedItems.has(ParcelStationTemplates.rowKey(item))
		);

		if (unselectedItems.length > 0) {
			// Add/update pending delivery note
			const deliveryNoteId = this.state.currentDeliveryNote.delivery_note;
			this.state.pendingDeliveryNotes.set(deliveryNoteId, {
				items: unselectedItems,
				delivery_note_data: this.state.currentDeliveryNote,
			});
			this.savePendingToStorage();
			ParcelStationUI.showMessage(
				`${unselectedItems.length} Position(en) von ${deliveryNoteId} sind noch offen.`,
				"warning"
			);
		} else {
			// Remove from pending if all items are now shipped
			const deliveryNoteId = this.state.currentDeliveryNote.delivery_note;
			this.state.pendingDeliveryNotes.delete(deliveryNoteId);
			this.savePendingToStorage();
			ParcelStationUI.showMessage("Alle Positionen versendet – Lieferschein abgeschlossen.", "success");
		}

		// Update pending delivery notes display
		this.renderPendingDeliveryNotes();
	}

	/**
	 * Render pending delivery notes
	 */
	renderPendingDeliveryNotes() {
		ParcelStationUI.renderPendingDeliveryNotes(
			this.state.pendingDeliveryNotes,
			(deliveryNote) => this.loadPendingDeliveryNote(deliveryNote)
		);
	}

	/**
	 * Load a pending delivery note
	 * @param {string} deliveryNote - The delivery note ID
	 */
	loadPendingDeliveryNote(deliveryNote) {
		const pendingData = this.state.pendingDeliveryNotes.get(deliveryNote);
		if (!pendingData) {
			ParcelStationUI.showMessage("Angefangener Lieferschein nicht gefunden.", "warning");
			return;
		}

		// Set the barcode input to the delivery note
		ParcelStationUI.elements.barcodeInput.value = deliveryNote;

		// Load the delivery note data
		this.state.currentDeliveryNote = pendingData.delivery_note_data;
		this.state.items = pendingData.items;
		this.state.lastShipment = null;
		this.state.retryShipment = null;
		this.setDimensions(null);
		ParcelStationUI.resetShipmentInfo();
		this.updatePrintButtonState();

		// Render only the pending items
		this.renderItems();
		ParcelStationUI.highlightQueueRow(null);

		ParcelStationUI.showMessage(`Restliche Positionen von ${deliveryNote} geladen.`, "info");
		ParcelStationUI.focusBarcodeInput();
	}

	/**
	 * Clear current selection
	 */
	clearCurrentSelection() {
		ParcelStationUI.clearCurrentSelection();
		this.state.currentDeliveryNote = null;
		this.state.items = [];
		this.state.selectedItems.clear();
		this.state.retryShipment = null;
		ParcelStationUI.setPackMode("pack");
		this.updatePrintButtonState();
	}

	/**
	 * "Zurückstellen": put the Delivery Note in hand back without creating
	 * anything. It stays in the work list.
	 */
	deferCurrent() {
		if (this._createInFlight || !this.state.currentDeliveryNote) return;
		const name = this.state.currentDeliveryNote.delivery_note;
		this.state.lastShipment = null;
		this.state.retryShipment = null;
		this.setDimensions(null);
		if (this.weight) this.weight.clearManual();
		ParcelStationUI.resetShipmentInfo();
		this.clearCurrentSelection();
		ParcelStationUI.showMessage(`${name} zurückgestellt.`, "info");
		ParcelStationUI.focusBarcodeInput();
	}

	/**
	 * Clear all pending delivery notes
	 */
	clearAllPending() {
		if (confirm("Alle angefangenen Lieferscheine aus dieser Liste verwerfen? Die Lieferscheine selbst bleiben unverändert.")) {
			this.state.pendingDeliveryNotes.clear();
			this.savePendingToStorage();
			this.renderPendingDeliveryNotes();
			ParcelStationUI.showMessage("Liste der angefangenen Lieferscheine geleert.", "info");
		}
	}

	/**
	 * Bind marker-wrapped scanner capture outside the barcode input. The capture
	 * module owns the global keydown listener and feeds completed @:...:@ tokens
	 * back through the existing barcode input pipeline.
	 */
	bindScannerCapture() {
		if (!window.ParcelStationScannerCapture || !ParcelStationUI.elements.barcodeInput) {
			return;
		}

		const root = document.getElementById("parcel-station-root");
		this.scannerCapture = new window.ParcelStationScannerCapture({
			input: ParcelStationUI.elements.barcodeInput,
			isActive: () => Boolean(root && root.isConnected),
			onToken: (token) => {
				ParcelStationUI.elements.barcodeInput.focus();
				ParcelStationUI.elements.barcodeInput.value += token;
				ParcelStationUI.elements.barcodeInput.dispatchEvent(
					new Event("input", { bubbles: true })
				);
			},
		});
		this.scannerCapture.bind();
	}

	/**
	 * Release global listeners before the Parcel Station page is re-mounted.
	 */
	destroy() {
		if (this._queueTimer) {
			clearInterval(this._queueTimer);
			this._queueTimer = null;
		}
		if (this._onDocumentClick) {
			document.removeEventListener("click", this._onDocumentClick);
			document.removeEventListener("keydown", this._onDocumentKeydown);
			this._onDocumentClick = null;
		}
		if (this.hw && this._onBridgeChange) {
			this.hw.off("devices", this._onBridgeChange);
			this.hw.off("state", this._onBridgeChange);
			this._onBridgeChange = null;
		}
		if (this.weight) {
			this.weight.destroy();
			this.weight = null;
		}
		if (this.scannerCapture) {
			this.scannerCapture.destroy();
			this.scannerCapture = null;
		}
	}

	/**
	 * Bind event handlers
	 */
	bindEvents() {
		// Barcode input events - essential, should exist
		if (ParcelStationUI.elements.barcodeInput) {
			ParcelStationUI.elements.barcodeInput.addEventListener("keypress", (e) => {
				if (e.key === "Enter") {
					e.preventDefault();
					const barcode = ParcelStationUI.elements.barcodeInput.value.trim();
					if (barcode) {
						this.handleInput(barcode, true);
					}
				}
			});

			ParcelStationUI.elements.barcodeInput.addEventListener("input", () => {
				const raw = ParcelStationUI.elements.barcodeInput.value;
				// Any complete @: :@ token means a scan landed — route it
				// immediately (covers scanners that don't emit Enter, and the
				// case where a new scan is appended to the kept previous value).
				if (/@:.*?:@/.test(raw)) {
					this.handleInput(raw, true);
					return;
				}
				// A marker barcode is mid-entry ("@:…" without the closing ":@") —
				// the operator is still typing/scanning it. Do nothing: don't run a
				// manual DN lookup on the partial value (that would fight typing).
				if (raw.includes("@:")) {
					return;
				}
				// Unmarked input >= 8 chars: legacy manual DN auto-lookup (debounced).
				const barcode = raw.trim();
				if (barcode.length >= 8) {
					setTimeout(() => {
						if (ParcelStationUI.elements.barcodeInput.value.trim() === barcode) {
							this.handleInput(barcode, false);
						}
					}, 500);
				}
			});
		}

		this.bindScannerCapture();

		const el = ParcelStationUI.elements;
		const on = (element, event, handler) => {
			if (element) element.addEventListener(event, handler);
		};

		// Button events
		on(el.createShipmentBtn, "click", () => this.createShipment());
		on(el.deferBtn, "click", () => this.deferCurrent());
		on(el.messageClose, "click", () => ParcelStationUI.hideMessage());
		on(el.clearPendingBtn, "click", () => this.clearAllPending());
		on(el.ordersToPackBtn, "click", () => this.refreshOrdersToPack());

		// Printing
		on(el.printLabelBtn, "click", () => this.printLabel());
		on(el.printDocumentsBtn, "click", () => this.printCustomsDocuments());
		on(el.refreshPrintersBtn, "click", () => this.loadPrinters(true));
		on(el.printerSelect, "change", (event) => this.handlePrinterChange(event.target.value));
		on(el.documentPrinterSelect, "change", (event) =>
			this.handleDocumentPrinterChange(event.target.value)
		);

		// Header panels: the printer chips open the printer panel, the FedEx
		// button its pickup panel. A click elsewhere or Escape closes them.
		const openPanel = (name) => (event) => {
			event.stopPropagation();
			ParcelStationUI.togglePanel(name);
		};
		on(el.chipLabelPrinter, "click", openPanel("printers"));
		on(el.chipDocumentPrinter, "click", openPanel("printers"));
		on(el.fedexBtn, "click", openPanel("fedex"));
		on(el.chipScale, "click", () => {
			if (this.weight) this.weight.chipClicked();
		});
		this._onDocumentClick = (event) => {
			if (!event.target.closest || event.target.closest("#parcel-station-root .ps-panel")) return;
			ParcelStationUI.togglePanel(null);
		};
		this._onDocumentKeydown = (event) => {
			if (event.key === "Escape") ParcelStationUI.togglePanel(null);
		};
		document.addEventListener("click", this._onDocumentClick);
		document.addEventListener("keydown", this._onDocumentKeydown);

		// FedEx pickup
		on(el.fedexPickupRequestBtn, "click", () => this.requestFedexPickup());
		on(el.fedexPickupRefreshBtn, "click", () => this.refreshFedexPickup());
		// Cancel buttons are re-rendered with the list, so delegate.
		on(el.fedexPickupCard, "click", (event) => {
			const btn = event.target.closest(".fedex-pickup-cancel");
			if (btn) this.cancelFedexPickup(btn.dataset.pickup, btn.dataset.confirmation);
		});

		// Parcel dimensions by hand
		on(el.dimsManualBtn, "click", () => this.openManualDimensions());
		on(el.dimsScanBtn, "click", () => {
			this.setDimensions(null);
			ParcelStationUI.focusBarcodeInput();
		});
		const dimensionInputs = [el.dimsLength, el.dimsWidth, el.dimsHeight];
		dimensionInputs.forEach((input, index) => {
			on(input, "input", () => this.syncManualDimensions());
			on(input, "keydown", (event) => {
				if (event.key !== "Enter") return;
				event.preventDefault();
				// Enter walks through the fields and ends on the button — it
				// never creates the label by itself.
				const next = dimensionInputs[index + 1];
				if (next) {
					next.focus();
					next.select();
				} else if (el.createShipmentBtn && !el.createShipmentBtn.disabled) {
					el.createShipmentBtn.focus();
				}
			});
		});
	}

	/**
	 * Printers at this station.
	 *
	 * The label printer is one of three kinds, and its kind decides in which
	 * format a label is requested from the carrier (a label is issued once):
	 *
	 *   bridge  a printer on the local hardware bridge: a raw (ZPL)
	 *           label printer, or an IPP printer that takes PDF    -> ZPL / PDF
	 *   cups    a queue on the print server, sent by the ERP server -> ZPL
	 *   pdf     no printer at all: the carrier renders a PDF and the
	 *           browser prints it through its dialog               -> PDF
	 *
	 * The label reaches the printer exactly as the carrier returned it; it is
	 * never converted between the two formats. The choice is remembered in
	 * this browser.
	 */
	initializePrinters() {
		this.state.savedPrinter = ParcelStationStorage.loadSelectedPrinter();
		this.state.savedDocumentPrinter = ParcelStationStorage.loadSelectedDocumentPrinter();
		this.state.selectedPrinter = this.state.savedPrinter;
		this.state.selectedDocumentPrinter = this.state.savedDocumentPrinter;

		this.hw = window.HardwareBridge
			? window.HardwareBridge.shared({ client: "parcel_station" })
			: null;
		if (this.hw) {
			// Bridge printers appear when the bridge connects and change state
			// when a printer is switched off.
			this._onBridgeChange = () => this.applyPrinterChoices();
			this.hw.on("devices", this._onBridgeChange);
			this.hw.on("state", this._onBridgeChange);
			// The Desk's quick print lets the client rest on a PC without a
			// bridge; the station wants it connected.
			this.hw.retry();
		}
		this.loadPrinters(false);
	}

	/** Printers configured in the hardware bridge of this PC. */
	bridgePrinters() {
		if (!this.hw || this.hw.state !== "ready") return [];
		return this.hw.devicesOfKind("printer");
	}

	/**
	 * Bridge printers that take `format`, as select choices. A bridge that
	 * does not report formats yet (before v0.1.3) only has raw printers.
	 */
	bridgeChoices(format) {
		return this.bridgePrinters()
			.filter((device) => (device.formats || ["zpl"]).includes(format))
			.map((device) => {
				const name = format === "pdf" ? `${device.id} (PDF)` : device.id;
				return {
					value: `bridge:${device.id}`,
					label: device.state === "online" ? name : `${name} – nicht erreichbar`,
					printer: {
						type: "bridge",
						name: `bridge:${device.id}`,
						id: device.id,
						printer_name: device.id,
						format,
					},
				};
			});
	}

	static printerKind(printer) {
		return (printer && printer.type) || (printer ? "cups" : null);
	}

	/**
	 * Fetch printers from backend
	 * @param {boolean} showFeedback
	 */
	async loadPrinters(showFeedback = false) {
		ParcelStationUI.setPrinterLoading(true);
		try {
			const printers = await ParcelStationAPI.fetchNetworkPrinters();
			this.state.printers = Array.isArray(printers)
				? printers.map((printer) => ({
					...printer,
					type: "cups",
					port: typeof printer.port === "number" ? printer.port : parseInt(printer.port, 10) || 0,
				}))
				: [];
			this.state.printersLoaded = true;
			if (showFeedback) {
				ParcelStationUI.showMessage("Druckerliste neu geladen.", "success");
			}
		} catch (error) {
			console.error("Unable to load printers:", error);
			this.state.printers = [];
			// The server's queues are unknown, but bridge and PDF still work.
			this.state.printersLoaded = true;
			ParcelStationUI.showMessage(
				`Drucker des Servers konnten nicht geladen werden: ${error.message}`,
				"danger"
			);
		} finally {
			ParcelStationUI.setPrinterLoading(false);
			this.applyPrinterChoices();
		}
	}

	/**
	 * Everything the label printer select offers, grouped.
	 * @param {"label"|"documents"} target
	 */
	printerChoices(target) {
		const groups = [];
		if (target === "label") {
			// A label printer first; a printer that takes both prints ZPL.
			const raw = this.bridgeChoices("zpl");
			const taken = new Set(raw.map((choice) => choice.value));
			const pdf = this.bridgeChoices("pdf").filter((choice) => !taken.has(choice.value));
			if (raw.length || pdf.length) {
				groups.push({ label: "Hardware Bridge an diesem PC", choices: raw.concat(pdf) });
			}
		} else {
			const pdf = this.bridgeChoices("pdf");
			if (pdf.length) groups.push({ label: "Hardware Bridge an diesem PC", choices: pdf });
		}
		const cups = (this.state.printers || []).map((printer) => ({
			value: printer.name,
			label: `${printer.printer_name} (${printer.server_ip}:${printer.port})`,
			printer,
		}));
		if (cups.length) {
			groups.push({ label: target === "label" ? "Druckserver (ZPL)" : "Druckserver", choices: cups });
		}
		groups.push({
			label: target === "label" ? "Ohne Labeldrucker" : "Ohne Druckserver",
			choices: [
				{
					value: "pdf",
					label: target === "label" ? "PDF (Druckdialog)" : "Druckdialog des Browsers",
					printer: { type: "pdf", name: "pdf", printer_name: "PDF (Druckdialog)" },
				},
			],
		});
		return groups;
	}

	_findChoice(target, value) {
		for (const group of this.printerChoices(target)) {
			const hit = group.choices.find((choice) => choice.value === value);
			if (hit) return hit.printer;
		}
		return null;
	}

	/**
	 * Settle which printers are in use and redraw selects, chips and buttons.
	 *
	 * A saved choice wins. Without one the station is print-ready by itself:
	 * a bridge printer if this PC has one, else the first queue of the print
	 * server. PDF is never a default — it is the fallback someone picks.
	 */
	applyPrinterChoices() {
		if (!this.state.printersLoaded) return;

		const saved = this.state.savedPrinter;
		let label = null;
		if (saved) {
			label = this._findChoice("label", saved.name);
			// A bridge printer stays selected while the bridge is away: the
			// chip says so, and nobody's choice silently turns into another.
			if (!label && ParcelStationManager.printerKind(saved) === "bridge") label = saved;
		}
		if (!label) {
			// A PDF printer on the bridge is usually the office printer set up
			// for the Desk's quick print — never the default for labels.
			const raw = this.bridgeChoices("zpl");
			if (raw.length) label = raw[0].printer;
			else if (this.state.printers.length) label = this.state.printers[0];
		}
		this.state.selectedPrinter = label;

		// Customs papers: no auto-default. The A4 printer is a different
		// device than the label printer, so guessing would send CN23 papers to
		// the label roll.
		const savedDocs = this.state.savedDocumentPrinter;
		let documents = savedDocs ? this._findChoice("documents", savedDocs.name) : null;
		if (!documents && savedDocs && ParcelStationManager.printerKind(savedDocs) === "bridge") {
			documents = savedDocs;
		}
		this.state.selectedDocumentPrinter = documents;

		ParcelStationUI.setPrinterOptions(
			this.printerChoices("label"),
			label ? label.name : "",
			"label",
			// Keep an absent bridge printer visible as the current value.
			label && !this._findChoice("label", label.name) ? label : null
		);
		ParcelStationUI.setPrinterOptions(
			this.printerChoices("documents"),
			documents ? documents.name : "",
			"documents",
			documents && !this._findChoice("documents", documents.name) ? documents : null
		);
		ParcelStationUI.updatePrinterStatus(label, label ? null : "Kein Labeldrucker gewählt");
		ParcelStationUI.updatePrinterStatus(
			this.state.selectedDocumentPrinter,
			this.state.selectedDocumentPrinter ? null : "Kein Drucker für Zolldokumente gewählt (oben in der Kopfzeile)",
			"documents"
		);
		this.updatePrintButtonState();
	}

	/** "zpl" or "pdf": the format a printer takes. */
	static printerFormat(printer) {
		const kind = ParcelStationManager.printerKind(printer);
		if (kind === "pdf") return "pdf";
		if (kind === "bridge") return printer.format || "zpl";
		return "zpl";
	}

	/** "zpl" or "pdf": what a label created now is requested as. */
	labelFormatForPrinter() {
		return ParcelStationManager.printerFormat(this.state.selectedPrinter);
	}

	/** Format of the label on record for the current shipment, if known. */
	currentLabelFormat() {
		const shipment = this.state.lastShipment || {};
		if (shipment.label_format) return shipment.label_format;
		if (shipment.pdf_present) return "pdf";
		const url = String(shipment.label_file_url || "").toLowerCase();
		if (url.endsWith(".pdf")) return "pdf";
		return shipment.zpl_present || url.endsWith(".zpl") ? "zpl" : null;
	}

	/**
	 * Why the selected printer cannot take the current label, or null.
	 * Formats are never converted, so a PDF label needs the dialog and a ZPL
	 * label needs a label printer.
	 */
	labelPrinterMismatch() {
		const printer = this.state.selectedPrinter;
		const format = this.currentLabelFormat();
		if (!printer || !format) return null;
		const takes = ParcelStationManager.printerFormat(printer);
		if (format === takes) return null;
		return format === "pdf"
			? "Dieses Label wurde als PDF ausgestellt. Oben einen PDF-Drucker oder „PDF (Druckdialog)“ wählen, um es zu drucken."
			: "Dieses Label wurde für einen Labeldrucker (ZPL) ausgestellt. Oben einen Labeldrucker wählen, um es zu drucken.";
	}

	/**
	 * Handle change event from printer dropdown
	 * @param {string} value
	 */
	handlePrinterChange(value) {
		const printer = value ? this._findChoice("label", value) : null;
		this.state.savedPrinter = printer;
		ParcelStationStorage.saveSelectedPrinter(printer);
		this.applyPrinterChoices();
		if (printer) {
			const kind = ParcelStationManager.printerKind(printer);
			const format = ParcelStationManager.printerFormat(printer).toUpperCase();
			ParcelStationUI.showMessage(
				kind === "pdf"
					? "Labels werden ab jetzt als PDF angefordert und über den Druckdialog gedruckt."
					: `Labeldrucker: ${printer.printer_name}. Labels werden als ${format} angefordert.`,
				"info"
			);
		}
	}

	/**
	 * Check whether a label is available for printing
	 * @returns {boolean}
	 */
	hasLabelAvailable() {
		return Boolean(
			this.state.lastShipment &&
			(
				this.state.lastShipment.label_file_url ||
				this.state.lastShipment.zpl_present ||
				this.state.lastShipment.pdf_present
			)
		);
	}

	/**
	 * Update print button based on label/printer availability
	 */
	updatePrintButtonState() {
		const canPrint = this.hasLabelAvailable() && !!this.state.selectedPrinter;
		ParcelStationUI.setPrintButtonEnabled(canPrint);

		const canPrintDocuments =
			this.hasCustomsDocumentsAvailable() && !!this.state.selectedDocumentPrinter;
		ParcelStationUI.setDocumentsButtonEnabled(canPrintDocuments);

		// Every printer change ends here, so the header chips follow from here.
		const selected = this.state.selectedPrinter;
		const bridgeDevice =
			ParcelStationManager.printerKind(selected) === "bridge"
				? this.bridgePrinters().find((device) => device.id === selected.id) || null
				: null;
		ParcelStationUI.renderPrinterChips(selected, this.state.selectedDocumentPrinter, bridgeDevice);
	}

	/**
	 * Send the label of the current shipment to the selected printer.
	 * Returns {ok, dialog?}: `dialog` means the browser's print dialog was
	 * opened — whether paper came out is not known then.
	 */
	async printLabel(options = {}) {
		// "auto" when invoked by the shipment flow (autoPrintLabel), else
		// "manual" (the Print button). Forwarded to the backend for logging.
		const trigger = options.auto ? "auto" : "manual";
		const shipment = this.state.lastShipment && this.state.lastShipment.shipment;
		const printer = this.state.selectedPrinter;

		if (!shipment) {
			ParcelStationUI.showMessage("Keine Sendung zum Drucken vorhanden.", "warning");
			return { ok: false, error: "no shipment" };
		}
		if (!this.hasLabelAvailable()) {
			ParcelStationUI.showMessage("Für diese Sendung liegt noch kein Label vor.", "warning");
			return { ok: false, error: "no label" };
		}
		if (!printer) {
			ParcelStationUI.showMessage("Bitte zuerst oben einen Labeldrucker wählen.", "warning");
			return { ok: false, error: "no printer" };
		}
		const mismatch = this.labelPrinterMismatch();
		if (mismatch) {
			ParcelStationUI.showMessage(mismatch, "warning");
			ParcelStationUI.updatePrinterStatus(printer, mismatch);
			return { ok: false, error: "format" };
		}

		const kind = ParcelStationManager.printerKind(printer);
		try {
			ParcelStationUI.setPrintButtonLoading(true);
			if (kind === "pdf") {
				const label = await ParcelStationAPI.getShipmentLabel(shipment);
				ParcelStationUI.printPdf(label.content_base64);
				ParcelStationUI.showMessage("Druckdialog geöffnet – dort den Drucker wählen und drucken.", "success");
				ParcelStationUI.updatePrinterStatus(printer, "Druckdialog geöffnet");
				ParcelStationUI.setResultState("done", "Label erstellt – Druckdialog geöffnet");
				return { ok: true, dialog: true };
			}

			let jobInfo = "Druckauftrag";
			let target = printer.printer_name;
			if (kind === "bridge") {
				if (!this.hw || this.hw.state !== "ready") {
					throw new Error("Keine Verbindung zur Hardware Bridge an diesem PC.");
				}
				// The label goes from the server through this browser to the
				// local printer, byte for byte.
				const label = await ParcelStationAPI.getShipmentLabel(shipment);
				try {
					await this.hw.print(
						printer.id,
						label.content_base64,
						shipment,
						ParcelStationManager.printerFormat(printer)
					);
				} catch (bridgeError) {
					throw new Error(bridgeError.message || bridgeError.code || "Bridge-Fehler");
				}
				target = `${printer.printer_name} (Hardware Bridge)`;
			} else {
				const result = await ParcelStationAPI.printLabel(shipment, printer.name, trigger);
				if (result && result.job_id) jobInfo = `Druckauftrag ${result.job_id}`;
				target = `${result.printer} (${result.server}:${result.port})`;
			}
			ParcelStationUI.showMessage(`${jobInfo} an ${target} gesendet.`, "success");
			// Reflect success in the persistent printer-status line too.
			ParcelStationUI.updatePrinterStatus(printer, `${jobInfo} gesendet`);
			ParcelStationUI.setResultState("done", "Fertig – Label ist gedruckt");
			return { ok: true };
		} catch (error) {
			console.error("Print error:", error);
			ParcelStationUI.showMessage(`Label konnte nicht gedruckt werden: ${error.message}`, "danger");
			// Persist the failure in the printer-status line so it isn't lost when a
			// later note replaces the transient one — this is what makes an
			// auto-print failure visible in the UI.
			ParcelStationUI.updatePrinterStatus(printer, `Druck fehlgeschlagen: ${error.message}`);
			return { ok: false, error: error.message };
		} finally {
			ParcelStationUI.setPrintButtonLoading(false);
			this.updatePrintButtonState();
		}
	}

	/**
	 * Handle change event from the customs-documents printer dropdown
	 * @param {string} value
	 */
	handleDocumentPrinterChange(value) {
		const printer = value ? this._findChoice("documents", value) : null;
		this.state.savedDocumentPrinter = printer;
		ParcelStationStorage.saveSelectedDocumentPrinter(printer);
		this.applyPrinterChoices();
		if (printer) {
			const kind = ParcelStationManager.printerKind(printer);
			ParcelStationUI.showMessage(
				kind === "pdf"
					? "Zolldokumente werden über den Druckdialog gedruckt."
					: kind === "bridge"
					? `Zolldokumente drucken auf ${printer.printer_name} (Hardware Bridge).`
					: `Zolldokumente drucken auf ${printer.printer_name} (${printer.server_ip}:${printer.port}).`,
				"info"
			);
		}
	}

	/**
	 * Whether the current shipment has customs documents. Only customs
	 * destinations produce them, so this is false for domestic/EU parcels.
	 * @returns {boolean}
	 */
	hasCustomsDocumentsAvailable() {
		return Boolean(
			this.state.lastShipment &&
			(
				this.state.lastShipment.customs_documents_url ||
				this.state.lastShipment.shipment_documents_present
			)
		);
	}

	/**
	 * Send the customs documents (A4 PDF) to the selected sheet printer, or
	 * open them in the browser's print dialog.
	 * @param {object} options - {auto: boolean}
	 */
	async printCustomsDocuments(options = {}) {
		const trigger = options.auto ? "auto" : "manual";
		const shipment = this.state.lastShipment && this.state.lastShipment.shipment;
		const printer = this.state.selectedDocumentPrinter;

		if (!shipment) {
			ParcelStationUI.showMessage("Keine Sendung zum Drucken vorhanden.", "warning");
			return { ok: false, error: "no shipment" };
		}
		if (!this.hasCustomsDocumentsAvailable()) {
			ParcelStationUI.showMessage("Diese Sendung hat keine Zolldokumente.", "warning");
			return { ok: false, error: "no documents" };
		}
		if (!printer) {
			ParcelStationUI.showMessage(
				"Bitte zuerst oben einen Drucker für Zolldokumente wählen.",
				"warning"
			);
			return { ok: false, error: "no printer" };
		}

		try {
			ParcelStationUI.setDocumentsButtonLoading(true);
			if (ParcelStationManager.printerKind(printer) === "pdf") {
				const documents = await ParcelStationAPI.getShipmentDocuments(shipment);
				ParcelStationUI.printPdf(documents.content_base64);
				ParcelStationUI.showMessage("Druckdialog für die Zolldokumente geöffnet.", "success");
				ParcelStationUI.updatePrinterStatus(printer, "Druckdialog geöffnet", "documents");
				return { ok: true, dialog: true };
			}
			if (ParcelStationManager.printerKind(printer) === "bridge") {
				if (!this.hw || this.hw.state !== "ready") {
					throw new Error("Keine Verbindung zur Hardware Bridge an diesem PC.");
				}
				const documents = await ParcelStationAPI.getShipmentDocuments(shipment);
				try {
					await this.hw.print(printer.id, documents.content_base64, `${shipment} Zoll`, "pdf");
				} catch (bridgeError) {
					throw new Error(bridgeError.message || bridgeError.code || "Bridge-Fehler");
				}
				ParcelStationUI.showMessage(
					`Zolldokumente an ${printer.printer_name} (Hardware Bridge) gesendet.`,
					"success"
				);
				ParcelStationUI.updatePrinterStatus(printer, "Druckauftrag gesendet", "documents");
				return { ok: true };
			}
			const result = await ParcelStationAPI.printCustomsDocuments(shipment, printer.name, trigger);
			const jobInfo = result && result.job_id ? `Druckauftrag ${result.job_id}` : "Druckauftrag";
			ParcelStationUI.showMessage(
				`${jobInfo} (Zolldokumente) an ${result.printer} gesendet (${result.server}:${result.port}).`,
				"success"
			);
			ParcelStationUI.updatePrinterStatus(printer, `${jobInfo} gesendet`, "documents");
			return { ok: true, result };
		} catch (error) {
			console.error("Customs document print error:", error);
			ParcelStationUI.showMessage(
				`Zolldokumente konnten nicht gedruckt werden: ${error.message}`,
				"danger"
			);
			// Persist in the status line so an auto-print failure survives later notes.
			ParcelStationUI.updatePrinterStatus(printer, `Druck fehlgeschlagen: ${error.message}`, "documents");
			return { ok: false, error: error.message };
		} finally {
			ParcelStationUI.setDocumentsButtonLoading(false);
			this.updatePrintButtonState();
		}
	}

	/**
	 * Print the customs documents right after the label. Same idempotency
	 * guard as autoPrintLabel (once per generated label) and the same
	 * never-block contract: a missing printer only hints, it does not
	 * interrupt the shipment flow.
	 * @returns {Promise<string>} outcome tag
	 */
	async autoPrintCustomsDocuments() {
		const sh = this.state.lastShipment || {};
		const tracking = sh.tracking_number || (sh.tracking_codes || [])[0];

		if (!this.hasCustomsDocumentsAvailable() || !tracking) {
			return "no-documents";
		}

		const key = `${sh.shipment || ""}::${tracking}`;
		if (this._lastAutoDocumentPrintKey === key) {
			return "skipped";
		}

		const printer = this.state.selectedDocumentPrinter;
		if (!printer) {
			ParcelStationUI.showMessage(
				"Zolldokumente liegen vor – oben einen Drucker dafür wählen, dann werden sie automatisch gedruckt.",
				"info"
			);
			return "no-printer";
		}
		// Two print dialogs on top of each other would hide the label's: with
		// the dialog the papers wait for a click on their button.
		if (ParcelStationManager.printerKind(printer) === "pdf") {
			return "no-printer";
		}

		this._lastAutoDocumentPrintKey = key;
		let res = null;
		try {
			res = await this.printCustomsDocuments({ auto: true });
		} catch (error) {
			console.error("[Parcel Station] Auto-print of customs documents failed:", error);
		}
		return res && res.ok ? "printed" : "failed";
	}

	/**
	 * Print the just-created label by itself, for the carton scan and for the
	 * button alike.
	 *
	 * Safety:
	 *  - Prints only a SUCCESSFUL label (a tracking number is present), so an
	 *    error/placeholder label is never printed.
	 *  - Fires at most ONCE per generated label, guarded by shipment+tracking.
	 *  - No printer selected, or one that cannot take this label's format →
	 *    skip with a hint, never block.
	 *  - Best-effort: a print failure is logged + surfaced but never blocks
	 *    shipment creation.
	 *
	 * @returns {Promise<string>} "printed" | "dialog" | "failed" | "no-printer"
	 *   | "no-label" | "skipped"
	 */
	async autoPrintLabel() {
		const sh = this.state.lastShipment || {};
		const tracking = sh.tracking_number || (sh.tracking_codes || [])[0];

		// Label must exist AND be a real (successful) label — tracking present.
		if (!this.hasLabelAvailable() || !tracking) {
			return "no-label";
		}

		// Trigger at most once per generated label (idempotency guard).
		const key = `${sh.shipment || ""}::${tracking}`;
		if (this._lastAutoPrintKey === key) {
			return "skipped";
		}

		// No printer selected → don't block; hint and let the operator click Print.
		if (!this.state.selectedPrinter) {
			ParcelStationUI.showMessage(
				"Label erstellt, aber nicht gedruckt: oben einen Labeldrucker wählen, dann „Label drucken“.",
				"info"
			);
			return "no-printer";
		}
		const mismatch = this.labelPrinterMismatch();
		if (mismatch) {
			ParcelStationUI.showMessage(mismatch, "warning");
			return "no-printer";
		}

		// Mark BEFORE awaiting so a re-entrant call can't double-fire; printLabel()
		// shows its own success/error note + printer status, and returns {ok,...}.
		this._lastAutoPrintKey = key;
		let res = null;
		try {
			res = await this.printLabel({ auto: true });
		} catch (error) {
			// Defensive — printLabel() already handles its own errors. A print
			// failure must never break the shipment flow.
			console.error("[Parcel Station] Auto-print failed:", error);
		}
		if (res && res.ok) return res.dialog ? "dialog" : "printed";
		return "failed";
	}

	/**
	   * Static bootstrap method to initialize the Parcel Station
	   * @param {HTMLElement|JQuery} containerElement - Target element to mount into
	   */
	static bootstrap(containerElement) {
		const target = ParcelStationManager.resolveContainer(containerElement);
		if (!target) {
			console.error("Parcel Station: unable to determine mount target", {
				containerElement,
			});
			frappe.msgprint(
				"Die Parcel Station konnte nicht geladen werden. Bitte die Seite neu laden."
			);
			return;
		}

		ParcelStationManager.removeLegacyInjection(target);

		// Replace rather than keep: after an update the cached page would
		// otherwise combine the new markup with the previous styles.
		const oldStyles = document.getElementById("parcel-station-styles");
		if (oldStyles) oldStyles.remove();
		document.head.insertAdjacentHTML("beforeend", ParcelStationStyles.getStyles());

		// The station is a full-width workplace screen, whatever the user's
		// "Full Width" setting for the rest of the Desk.
		const pageBody = target.closest(".page-body");
		if (pageBody) pageBody.classList.add("ps-full-width");

		const strayRoot = document.getElementById("parcel-station-root");
		if (strayRoot && strayRoot.parentElement !== target) {
			strayRoot.parentElement.removeChild(strayRoot);
		}

		const existingRoot = target.querySelector("#parcel-station-root");
		if (existingRoot) {
			existingRoot.remove();
		}

		target.insertAdjacentHTML(
			"beforeend",
			ParcelStationTemplates.getMainTemplate()
		);

		if (window.parcelStationManagerInstance?.destroy) {
			window.parcelStationManagerInstance.destroy();
		}
		window.parcelStationManagerInstance = new ParcelStationManager();

		console.log("Parcel Station initialized successfully");
	}

	static resolveContainer(element) {
		if (!element) {
			return null;
		}

		if (element instanceof HTMLElement) {
			return element;
		}

		if (element[0] && element[0] instanceof HTMLElement) {
			return element[0];
		}

		return null;
	}

	static removeLegacyInjection(target) {
		const legacyStyles = target.querySelectorAll("style");
		legacyStyles.forEach((styleEl) => {
			const css = (styleEl.textContent || "").toLowerCase();
			if (css.includes("enhanced shipment info cards")) {
				styleEl.remove();
			}
		});

		Array.from(document.querySelectorAll("style")).forEach((styleEl) => {
			if (styleEl.id === "parcel-station-styles") {
				return;
			}
			const css = (styleEl.textContent || "").toLowerCase();
			if (css.includes("enhanced shipment info cards")) {
				const parent = styleEl.parentElement;
				if (parent && parent !== document.head) {
					parent.removeChild(styleEl);
				}
			}
		});
	}
};
