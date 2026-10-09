/**
 * Parcel Station Scanner Capture
 * Routes marker-wrapped scanner keyboard input (@:...:@) into the barcode field
 * even when another page element has focus.
 */
(function () {
	const SCAN_PREFIX_START = "@";
	const SCAN_PREFIX_CONFIRM = ":";
	const SCAN_PREFIX = "@:";
	const SCAN_SUFFIX = ":@";
	const PREFIX_TIMEOUT_MS = 500;
	const CAPTURE_TIMEOUT_MS = 2000;
	const COMPLETE_TOKEN_RE = /^@:.+?:@$/;

	window.ParcelStationScannerCapture = class {
		constructor({ input, isActive, onToken }) {
			this.input = input;
			this.isActive = isActive || (() => true);
			this.onToken = onToken;
			this.state = this._initialState();
			this._boundKeydown = (event) => this.handleKeydown(event);
		}

		bind() {
			document.addEventListener("keydown", this._boundKeydown);
		}

		destroy() {
			document.removeEventListener("keydown", this._boundKeydown);
			this._reset();
		}

		handleKeydown(event) {
			if (this._shouldIgnore(event)) {
				return;
			}

			const active = document.activeElement;
			if (active === this.input) {
				return;
			}

			const now = Date.now();
			this._expireCapture(now);

			if (!this.state.buffer) {
				this._handlePrefixKey(event, active, now);
				return;
			}

			this._handleCaptureKey(event);
		}

		_shouldIgnore(event) {
			// German-layout scanners/keyboards can emit "@" via AltGr, which
			// browsers expose as Ctrl+Alt. Allow printable keys through so the
			// marker prefix can still be captured outside the barcode input.
			const printableOrEnter = event.key && (event.key.length === 1 || event.key === "Enter");
			return (
				!this.input ||
				!this.input.isConnected ||
				!this.isActive() ||
				event.metaKey ||
				((event.ctrlKey || event.altKey) && !printableOrEnter)
			);
		}

		_handlePrefixKey(event, active, now) {
			if (event.key === SCAN_PREFIX_START) {
				this.state.pendingAt = {
					target: active,
					timestamp: now,
				};
				return;
			}

			if (event.key !== SCAN_PREFIX_CONFIRM || !this._hasFreshPendingAt(active, now)) {
				this.state.pendingAt = null;
				return;
			}

			event.preventDefault();
			this._removePendingAtFromFocusedField(active);
			this.state = {
				...this._initialState(),
				buffer: SCAN_PREFIX,
				startedAt: now,
			};
		}

		_handleCaptureKey(event) {
			if (event.key === "Enter") {
				event.preventDefault();
				this._flush();
				return;
			}

			if (event.key.length !== 1) {
				return;
			}

			event.preventDefault();
			this.state.buffer += event.key;

			if (this.state.buffer.endsWith(SCAN_SUFFIX)) {
				this._flush();
			}
		}

		_hasFreshPendingAt(active, now) {
			return Boolean(
				this.state.pendingAt &&
				this.state.pendingAt.target === active &&
				now - this.state.pendingAt.timestamp < PREFIX_TIMEOUT_MS
			);
		}

		_expireCapture(now) {
			if (this.state.buffer && now - this.state.startedAt > CAPTURE_TIMEOUT_MS) {
				this._reset();
			}
		}

		_removePendingAtFromFocusedField(active) {
			if (!active || !("value" in active) || typeof active.value !== "string") {
				return;
			}

			const start = active.selectionStart;
			const end = active.selectionEnd;
			if (typeof start !== "number" || typeof end !== "number") {
				return;
			}
			if (start !== end || start < 1 || active.value[start - 1] !== SCAN_PREFIX_START) {
				return;
			}

			active.value = active.value.slice(0, start - 1) + active.value.slice(start);
			active.setSelectionRange(start - 1, start - 1);
		}

		_flush() {
			const token = this.state.buffer;
			this._reset();
			if (!COMPLETE_TOKEN_RE.test(token)) {
				return;
			}
			this.onToken(token);
		}

		_reset() {
			this.state = this._initialState();
		}

		_initialState() {
			return {
				buffer: "",
				startedAt: 0,
				pendingAt: null,
			};
		}
	};
})();
