/**
 * Parcel Station – live weight from the local hardware bridge.
 *
 * Principle: connect or fall back. The desk configures nothing; it tries the
 * bridge on ws://localhost:8735 and shows one of four states:
 *
 *   ready           live weight from the scale (stable / moving)
 *   device_offline  bridge runs, scale configured but not reachable
 *   device_missing  bridge runs, no scale set up at this station
 *   no_bridge       no bridge on this PC
 *
 * In every state the operator may type a weight. Which weight a shipment
 * gets is decided by resolveForShipment():
 *
 *   typed-in value         -> used ("manual"), overrides the scale
 *   live + stable reading  -> used ("scale")
 *   live, not stable       -> waits up to 4 s, then refuses (no guessing)
 *   no live scale, nothing typed -> null: the server decides as before
 *                              (server-side scale, then article weights)
 */
window.ParcelStationWeight = class {
	constructor(mount) {
		this.mount = mount;
		this.last = null;
		this._waiters = [];
		this.hw = window.HardwareBridge
			? window.HardwareBridge.shared({ client: "parcel_station" })
			: null;

		ParcelStationWeight.injectStyles();
		this.mount.innerHTML = ParcelStationWeight.template();
		this.el = {
			value: this.mount.querySelector("#ps-weight-value"),
			pill: this.mount.querySelector("#ps-weight-pill"),
			status: this.mount.querySelector("#ps-weight-status"),
			manual: this.mount.querySelector("#ps-weight-manual"),
			tare: this.mount.querySelector("#ps-weight-tare"),
		};

		if (this.hw) {
			this._onWeight = (w) => {
				this.last = Object.assign({ receivedAt: Date.now() }, w);
				if (w.stable) this._waiters.splice(0).forEach((fn) => fn(this.last));
				this.render();
			};
			this._onChange = () => this.render();
			this.hw.on("scale.weight", this._onWeight);
			this.hw.on("state", this._onChange);
			this.hw.on("devices", this._onChange);
			this._unsubscribe = this.hw.subscribe("scale");
			// Ends a standby the Desk's quick print may have put the client in.
			this.hw.retry();
		}

		this.el.tare.addEventListener("click", () => this.tare());
		this.render();
	}

	mode() {
		if (!this.hw) return "no_bridge";
		if (this.hw.state === "connecting") return "connecting";
		return this.hw.deviceState("scale");
	}

	/** Parsed manual weight: number, null (empty) or NaN (not a number). */
	manualValue() {
		const raw = (this.el.manual.value || "").trim().replace(",", ".");
		if (!raw) return null;
		const v = Number(raw);
		return Number.isFinite(v) ? v : NaN;
	}

	clearManual() {
		this.el.manual.value = "";
	}

	/**
	 * Weight for the next shipment: {kg, source} or null (server decides).
	 * Throws an Error with an operator-facing message when no weight may be used.
	 */
	async resolveForShipment() {
		const manual = this.manualValue();
		if (Number.isNaN(manual)) {
			throw new Error("Das von Hand eingegebene Gewicht ist keine Zahl.");
		}
		if (manual !== null) {
			if (manual <= 0) throw new Error("Das von Hand eingegebene Gewicht muss größer als 0 kg sein.");
			return { kg: manual, source: "manual" };
		}
		if (this.mode() !== "ready") return null;

		const reading = await this.waitStable(4000);
		if (!reading) {
			throw new Error(
				"Das Gewicht ist nicht stabil. Paket ruhig halten und erneut versuchen, oder das Gewicht von Hand eingeben."
			);
		}
		if (!(reading.kg > 0)) {
			throw new Error(
				`Die Waage zeigt ${ParcelStationWeight.format(reading.kg)} kg. Paket auf die Waage legen oder das Gewicht von Hand eingeben.`
			);
		}
		return { kg: reading.kg, source: "scale" };
	}

	/** Resolves with a fresh stable reading, or null after timeoutMs. */
	waitStable(timeoutMs) {
		const l = this.last;
		if (l && l.stable && Date.now() - l.receivedAt < 1500) return Promise.resolve(l);
		return new Promise((resolve) => {
			const done = (r) => {
				clearTimeout(timer);
				resolve(r);
			};
			const timer = setTimeout(() => {
				this._waiters = this._waiters.filter((fn) => fn !== done);
				resolve(null);
			}, timeoutMs);
			this._waiters.push(done);
		});
	}

	async tare() {
		this.el.tare.disabled = true;
		try {
			await this.hw.call("scale.tare");
			frappe.show_alert({ message: "Waage tariert", indicator: "green" });
		} catch (e) {
			frappe.show_alert({ message: `Tara fehlgeschlagen: ${e.message || e.code}`, indicator: "red" });
		} finally {
			this.el.tare.disabled = false;
		}
	}

	render() {
		const mode = this.mode();
		const live = mode === "ready";
		const l = this.last;
		const esc = frappe.utils.escape_html;

		this.el.tare.style.display = live ? "" : "none";
		this.mount.classList.toggle("ps-weight-live", live);

		if (live && l) {
			this.el.value.textContent = ParcelStationWeight.format(l.kg) + " kg";
			this.el.value.classList.toggle("ps-weight-moving", !l.stable);
			this.setPill(l.stable ? "ok" : "warn", l.stable ? "stabil" : "in Bewegung");
			this.el.status.textContent = "";
			this.setChip("ok", "Waage");
			return;
		}

		this.el.value.textContent = "–";
		this.el.value.classList.remove("ps-weight-moving");
		// A station without a scale shows this state all day: the explanation
		// lives in the tooltip, the footer only names the state.
		const fallbackHint = "Gewicht von Hand eingeben. Ohne Eingabe gelten Server-Waage oder Artikelgewichte.";
		this.el.status.textContent = "";
		switch (mode) {
			case "ready":
				this.setPill("warn", "wartet", "Warte auf den ersten Messwert …");
				this.setChip("ok", "Waage");
				break;
			case "connecting":
				this.setPill("", "verbindet", "Verbinde mit der Hardware Bridge …");
				this.setChip("idle", "Waage verbindet …");
				break;
			case "device_offline": {
				const d = this.hw.findDevice("scale") || {};
				this.setPill("error", "nicht verbunden", fallbackHint);
				this.el.status.textContent = `Waage nicht verbunden: ${d.message || d.state || ""}`;
				this.setChip("error", "Waage nicht verbunden");
				break;
			}
			case "device_missing":
				this.setPill("", "keine Waage", fallbackHint);
				this.el.status.innerHTML =
					esc("Keine Waage eingerichtet.") +
					' <a href="http://localhost:8735/#config" target="_blank" rel="noopener">' +
					esc("In der Hardware Bridge einrichten") +
					"</a>";
				this.setChip("idle", "Keine Waage");
				break;
			default:
				this.setPill("", "keine Bridge", "Keine Hardware Bridge an diesem PC. " + fallbackHint);
				this.setChip("idle", "Keine Waage", "Keine Hardware Bridge an diesem PC – klicken, um neu zu verbinden");
		}
	}

	/** Mirror the scale state into the header chip. */
	setChip(state, text, title) {
		if (window.ParcelStationUI && ParcelStationUI.setChip) {
			ParcelStationUI.setChip(
				"scale",
				state,
				text,
				title || this.el.status.textContent || this.el.pill.title || text
			);
		}
	}

	/**
	 * The header chip was clicked: without a bridge that means "try again",
	 * otherwise it leads to the manual weight field.
	 */
	chipClicked() {
		if (this.mode() === "no_bridge" && this.hw) {
			this.hw.retry();
			return;
		}
		this.el.manual.focus();
	}

	setPill(tone, text, title) {
		this.el.pill.dataset.tone = tone;
		this.el.pill.textContent = text;
		this.el.pill.title = title || "";
	}

	destroy() {
		if (this.hw) {
			this.hw.off("scale.weight", this._onWeight);
			this.hw.off("state", this._onChange);
			this.hw.off("devices", this._onChange);
		}
		if (this._unsubscribe) this._unsubscribe();
		this._waiters.splice(0).forEach((fn) => fn(null));
	}

	static format(kg) {
		return Number(kg).toLocaleString("de-AT", { minimumFractionDigits: 2, maximumFractionDigits: 3 });
	}

	static template() {
		return `
			<div class="ps-foot-label">
				<span>Gewicht</span>
				<span id="ps-weight-pill" class="ps-pill"></span>
				<button type="button" id="ps-weight-tare" class="ps-link">Tara</button>
			</div>
			<div class="ps-weight-row">
				<div id="ps-weight-value" class="ps-weight-value">–</div>
				<div class="ps-weight-manual">
					<label for="ps-weight-manual">von Hand (kg)</label>
					<input id="ps-weight-manual" type="text" inputmode="decimal" autocomplete="off">
				</div>
			</div>
			<div id="ps-weight-status" class="ps-weight-status"></div>`;
	}

	// Styles live in ParcelStationStyles; kept so older callers do not break.
	static injectStyles() {}
};
