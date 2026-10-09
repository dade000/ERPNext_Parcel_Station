/**
 * Parcel Station HTML Templates
 * Contains all HTML templates for the parcel station interface.
 * The station's texts are German: it is a workplace screen for the packing
 * team, independent of the Desk language of the logged-in user.
 */
window.ParcelStationTemplates = {
	ICONS: {
		barcode:
			'<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><line x1="4" y1="6" x2="4" y2="18"></line><line x1="8" y1="6" x2="8" y2="18"></line><line x1="11" y1="6" x2="11" y2="18"></line><line x1="15" y1="6" x2="15" y2="18"></line><line x1="18" y1="6" x2="18" y2="18"></line><line x1="20" y1="6" x2="20" y2="18"></line></svg>',
		check:
			'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="5 12 10 17 19 7"></polyline></svg>',
		refresh:
			'<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.3-4.3L4 9"></path><polyline points="4 4 4 9 9 9"></polyline><path d="M4 13a8 8 0 0 0 14.3 4.3L20 15"></path><polyline points="20 20 20 15 15 15"></polyline></svg>',
	},

	/**
	 * Layout: a permanent work list on the left ("Zu packen"), the parcel in
	 * hand on the right. The header carries what is not part of packing a
	 * single parcel — devices and the FedEx pickup — and opens those as
	 * panels, so the working area stays about the parcel.
	 *
	 * The element ids are the contract with ParcelStationUI / the manager.
	 */
	getMainTemplate: function () {
		const icons = this.ICONS;
		return `
	            <div id="parcel-station-root" class="ps">
                <header class="ps-top">
                    <div class="ps-progress" id="orders-to-pack-count" aria-live="polite">
                        <div class="ps-progress-bar"><div class="ps-progress-fill" id="ps-progress-fill"></div></div>
                        <div class="ps-progress-text" id="ps-progress-text">Lade Arbeitsliste …</div>
                    </div>
                    <div class="ps-top-actions">
                        <button type="button" class="ps-chip" id="ps-chip-scale" title="Waage">
                            <span class="ps-dot"></span><span class="ps-chip-text">Waage</span>
                        </button>
                        <button type="button" class="ps-chip" id="ps-chip-label-printer" title="Labeldrucker wählen">
                            <span class="ps-dot"></span><span class="ps-chip-text">Labeldrucker</span>
                        </button>
                        <button type="button" class="ps-chip" id="ps-chip-document-printer" title="A4-Drucker für Zolldokumente wählen">
                            <span class="ps-dot"></span><span class="ps-chip-text">A4-Drucker</span>
                        </button>
                        <button type="button" class="ps-btn" id="ps-fedex-btn" aria-expanded="false" aria-controls="fedex-pickup-card">
                            FedEx-Abholung <span class="ps-count ps-count-warn" id="ps-fedex-count" style="display: none">0</span>
                        </button>
                    </div>

                    <!-- Printers: chosen once per station, remembered in the browser. -->
                    <div class="ps-panel" id="print-section-card" hidden>
                        <div class="ps-panel-title">Drucker an diesem Platz</div>
                        <label for="printer-select" class="ps-label">Labeldrucker</label>
                        <select id="printer-select" class="ps-select">
                            <option value="">Drucker wählen</option>
                        </select>
                        <div class="ps-hint">Mit einem Labeldrucker kommen die Labels als ZPL. Ein PDF-Drucker oder „PDF (Druckdialog)“ ist für Plätze ohne Labeldrucker: Das Label kommt dann als PDF vom Carrier.</div>
                        <label for="document-printer-select" class="ps-label">A4-Drucker (Zolldokumente)</label>
                        <select id="document-printer-select" class="ps-select">
                            <option value="">Drucker wählen</option>
                        </select>
                        <div class="ps-panel-foot">
                            <button type="button" class="ps-btn ps-btn-small" id="refresh-printers-btn">Druckerliste neu laden</button>
                        </div>
                    </div>

                    <!-- FedEx pickup: the courier only comes when booked. The 12:00
                         run books automatically; this panel covers late shipments
                         and cancellations. -->
                    <div class="ps-panel" id="fedex-pickup-card" hidden>
                        <div class="ps-panel-title">FedEx-Abholung</div>
                        <div id="fedex-pickup-status" class="ps-panel-status">Lade …</div>
                        <div id="fedex-pickup-list" class="ps-panel-list"></div>
                        <div class="ps-panel-foot">
                            <button type="button" id="fedex-pickup-request-btn" class="ps-btn ps-btn-primary ps-btn-small" disabled>Abholung anfragen</button>
                            <button type="button" id="fedex-pickup-refresh-btn" class="ps-btn ps-btn-small">Aktualisieren</button>
                        </div>
                    </div>
                </header>

                <div class="ps-body">
                    <aside class="ps-queue" aria-label="Zu packen">
                        <div class="ps-queue-head">
                            <div class="ps-queue-title">
                                <h2>Zu packen</h2>
                                <button type="button" class="ps-icon-btn" id="orders-to-pack-btn" aria-label="Arbeitsliste aktualisieren" title="Arbeitsliste aktualisieren">${icons.refresh}</button>
                            </div>
                            <div class="ps-filters" id="ps-queue-filters"></div>
                        </div>
                        <div class="ps-queue-scroll">
                            <div id="ps-pending-section" hidden>
                                <div class="ps-queue-section ps-queue-section-warn">
                                    <span>Angefangen · Rest noch offen</span>
                                    <button type="button" class="ps-link" id="clear-pending-btn">Alle verwerfen</button>
                                </div>
                                <div id="pending-delivery-notes"></div>
                            </div>
                            <div class="ps-queue-section" id="ps-queue-section-open">Offen</div>
                            <div id="ps-queue-list"><div class="ps-queue-empty">Lade …</div></div>
                        </div>
                    </aside>

                    <main class="ps-work">
                        <div class="ps-scan">
                            ${icons.barcode}
                            <label for="barcode-input">Scan</label>
                            <input type="text" id="barcode-input" placeholder="Lieferschein oder Karton scannen" autocomplete="off" />
                        </div>

                        <!-- Status and errors sit right under the scan field so a
                             failed label is never below the fold. -->
                        <div id="ps-message" class="ps-message" style="display: none" role="status">
                            <span id="ps-message-text"></span>
                            <button type="button" id="ps-message-close" aria-label="Meldung schließen" title="Schließen">&times;</button>
                        </div>

                        <section class="ps-card">
                            <div id="customer-info" style="display: none"></div>

                            <!-- Result of the last label: stays until the next
                                 delivery note is loaded, so a label can be reprinted. -->
                            <div id="ps-result" class="ps-result" hidden>
                                <div class="ps-result-head">
                                    <div class="ps-result-title" id="shipment-status">Sendung</div>
                                    <div class="ps-result-id" id="shipment-created"></div>
                                </div>
                                <div class="ps-result-grid">
                                    <div>
                                        <div class="ps-label">Sendungsnummer</div>
                                        <div id="tracking-number" class="ps-mono ps-result-tracking"></div>
                                    </div>
                                    <div>
                                        <div class="ps-label">Label</div>
                                        <div class="ps-result-actions">
                                            <button type="button" id="print-label-btn" class="ps-btn ps-btn-small" disabled>Label drucken</button>
                                            <span id="shipment-label"></span>
                                        </div>
                                        <div id="printer-status" class="ps-hint"></div>
                                    </div>
                                    <div id="ps-result-documents" hidden>
                                        <div class="ps-label">Zolldokumente</div>
                                        <div class="ps-result-actions">
                                            <button type="button" id="print-documents-btn" class="ps-btn ps-btn-small" disabled>Zolldokumente drucken</button>
                                            <span id="shipment-documents"></span>
                                        </div>
                                        <div id="document-printer-status" class="ps-hint"></div>
                                    </div>
                                </div>
                            </div>

                            <div class="ps-packlist">
                                <div class="ps-packlist-head">
                                    <h2>Packliste</h2>
                                    <span class="ps-hint" id="ps-packlist-hint"></span>
                                </div>
                                <div id="items-list" class="ps-items">
                                    <div class="ps-empty">Lieferschein scannen oder links auswählen.</div>
                                </div>
                            </div>

                            <div class="ps-foot">
                                <div class="ps-foot-inputs">
                                <!-- Live weight from the local hardware bridge
                                     (rendered by ParcelStationWeight) -->
                                <div id="ps-weight-card" class="ps-foot-block"></div>

                                <div id="ps-dims" class="ps-foot-block ps-dims">
                                    <div class="ps-foot-label">
                                        <span>Kartonmaße</span>
                                        <span class="ps-pill" id="ps-dims-pill" hidden></span>
                                        <button type="button" class="ps-link" id="ps-dims-scan-btn" hidden>doch scannen</button>
                                    </div>
                                    <div id="ps-dims-scan" class="ps-dims-scan">
                                        <span class="ps-dims-prompt" id="ps-dims-value">Karton scannen</span>
                                        <button type="button" class="ps-btn ps-btn-small" id="ps-dims-manual-btn">Von Hand eingeben</button>
                                    </div>
                                    <div id="ps-dims-manual" class="ps-dims-manual" hidden>
                                        <div class="ps-dims-field">
                                            <label for="ps-dims-length">Länge</label>
                                            <input id="ps-dims-length" type="text" inputmode="decimal" autocomplete="off" />
                                        </div>
                                        <div class="ps-dims-field">
                                            <label for="ps-dims-width">Breite</label>
                                            <input id="ps-dims-width" type="text" inputmode="decimal" autocomplete="off" />
                                        </div>
                                        <div class="ps-dims-field">
                                            <label for="ps-dims-height">Höhe</label>
                                            <input id="ps-dims-height" type="text" inputmode="decimal" autocomplete="off" />
                                        </div>
                                        <span class="ps-dims-unit">cm</span>
                                    </div>
                                </div>
                                </div>

                                <div class="ps-foot-actions">
                                    <button type="button" id="ps-defer-btn" class="ps-btn" disabled>Zurückstellen</button>
                                    <button type="button" id="create-shipment-btn" class="ps-btn ps-btn-primary ps-btn-large" disabled>Label erstellen</button>
                                </div>
                            </div>
                        </section>
                    </main>
                </div>
            </div>
        `;
	},

	/**
	 * Identity of a packing-list row for selection tracking.
	 *
	 * `name` is the Delivery Note Item — or, for a Product Bundle component,
	 * the Packed Item row — and is unique per row. The item code is not: a
	 * bundle of socks and a single extra pair of the same socks share it.
	 */
	rowKey: function (item) {
		return item.name || item.item_code;
	},

	getItemsTableTemplate: function (items) {
		if (!items || items.length === 0) {
			return '<div class="ps-empty">Keine Positionen gefunden.</div>';
		}
		const esc = ParcelStationUI._escape.bind(ParcelStationUI);
		const weight = (item) => {
			if (!item.weight_per_unit) return "–";
			const kg = parseFloat(item.weight_per_unit) * parseFloat(item.qty || 0);
			return `${kg.toLocaleString("de-AT", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} kg`;
		};
		const qty = (item) => {
			const value = parseFloat(item.qty || item.available_qty || 0);
			return value.toLocaleString("de-AT", { maximumFractionDigits: 3 });
		};

		return `
            <div class="ps-items-head">
                <span>Dabei</span>
                <span>Artikel</span>
                <span class="ps-right">Menge</span>
                <span class="ps-right">Gewicht</span>
            </div>
            ${items
				.map(
					(item, index) => `
                <label class="ps-item" for="item-${index}">
                    <span class="ps-item-check">
                        <input
                            class="form-check-input"
                            type="checkbox"
                            id="item-${index}"
                            data-row-key="${esc(ParcelStationTemplates.rowKey(item))}"
                            data-item-code="${esc(item.item_code || "")}"
                            checked
                        >
                    </span>
                    <span class="ps-item-name">
                        <span>${esc(item.item_name || item.item_code || `Position ${index + 1}`)}</span>
                        <small>${
							item.packed_item
								? `aus Bundle „${esc(item.bundle_item_name || item.bundle_item_code || "")}“`
								: esc(item.item_code || "")
						}</small>
                    </span>
                    <span class="ps-right ps-mono ps-item-qty">${qty(item)}</span>
                    <span class="ps-right ps-mono ps-item-weight">${weight(item)}</span>
                </label>
            `
				)
				.join("")}
        `;
	},

	/** "POST" / "GLS" / "FEDEX" — the short carrier mark used across the page. */
	carrierBadge: function (carrierKey, carrierName) {
		const marks = { AUSTRIAN_POST: "POST", GLS: "GLS", FEDEX: "FEDEX" };
		const text = marks[carrierKey] || carrierName || "";
		if (!text) return "";
		return `<span class="ps-carrier" title="${ParcelStationUI._escape(carrierName || text)}">${ParcelStationUI._escape(text)}</span>`;
	},

	/**
	 * Header of the parcel in hand: delivery note, address, carrier.
	 * `displayAddress` is plain text; it is escaped here.
	 */
	getCustomerInfoTemplate: function (deliveryNote, customer, displayAddress) {
		const esc = ParcelStationUI._escape.bind(ParcelStationUI);
		const orders = (deliveryNote.sales_orders || []).join(", ");
		const deliveryKind = deliveryNote.is_parcel_shop ? "Paketshop" : "Hauszustellung";
		const service = [deliveryNote.service_name, deliveryKind].filter(Boolean).join(" · ");
		return `
            <div class="ps-head">
                <div>
                    <div class="ps-label">Lieferschein</div>
                    <div class="ps-mono ps-head-strong">${esc(deliveryNote.delivery_note || "")}</div>
                    <div class="ps-hint">${orders ? `Auftrag ${esc(orders)}` : "ohne Auftrag"}</div>
                </div>
                <div>
                    <div class="ps-label">Lieferadresse</div>
                    <div class="ps-head-strong">${esc(customer)}</div>
                    <div id="delivery-to-text" class="ps-head-text">${esc(displayAddress)}</div>
                </div>
                <div class="ps-head-carrier">
                    <div>
                        <div class="ps-label">Versandart</div>
                        <div class="ps-head-strong">${esc(deliveryNote.carrier || "nicht festgelegt")}</div>
                        <div class="ps-head-text">${esc(service)}${
			deliveryNote.customs ? ' · <span class="ps-flag">Zoll</span>' : ""
		}</div>
                    </div>
                    ${ParcelStationTemplates.carrierBadge(deliveryNote.carrier_key, deliveryNote.carrier)}
                </div>
            </div>
        `;
	},
};
