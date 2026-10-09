/**
 * Parcel Station Storage Manager
 * Handles all local storage operations for pending delivery notes
 */
window.ParcelStationStorage = {
	STORAGE_KEYS: {
		pending: "pendingDeliveryNotes",
		printer: "parcelStationSelectedPrinter",
		documentPrinter: "parcelStationSelectedDocumentPrinter",
	},

	/**
	 * Load pending delivery notes from localStorage
	 * @returns {Map} Map of pending delivery notes
	 */
	loadPendingDeliveryNotes: function () {
		try {
			const stored = localStorage.getItem(this.STORAGE_KEYS.pending);
			if (stored) {
				const data = JSON.parse(stored);
				return new Map(Object.entries(data));
			}
			return new Map();
		} catch (error) {
			console.error("Error loading pending delivery notes from storage:", error);
			return new Map();
		}
	},

	/**
	 * Save pending delivery notes to localStorage
	 * @param {Map} pendingDeliveryNotes - Map of pending delivery notes to save
	 */
	savePendingDeliveryNotes: function (pendingDeliveryNotes) {
		try {
			const data = Object.fromEntries(pendingDeliveryNotes);
			localStorage.setItem(this.STORAGE_KEYS.pending, JSON.stringify(data));
		} catch (error) {
			console.error("Error saving pending delivery notes to storage:", error);
		}
	},

	/**
	 * Clear all pending delivery notes from storage
	 */
	clearPendingDeliveryNotes: function () {
		try {
			localStorage.removeItem(this.STORAGE_KEYS.pending);
		} catch (error) {
			console.error("Error clearing pending delivery notes from storage:", error);
		}
	},

	/**
	 * Load the stored printer selection
	 * @returns {object|null}
	 */
	loadSelectedPrinter: function () {
		try {
			const stored = localStorage.getItem(this.STORAGE_KEYS.printer);
			return stored ? JSON.parse(stored) : null;
		} catch (error) {
			console.error("Error loading stored printer selection:", error);
			return null;
		}
	},

	/**
	 * Persist the selected printer (or clear when null)
	 * @param {object|null} printerData
	 */
	saveSelectedPrinter: function (printerData) {
		try {
			if (!printerData) {
				localStorage.removeItem(this.STORAGE_KEYS.printer);
				return;
			}
			localStorage.setItem(this.STORAGE_KEYS.printer, JSON.stringify(printerData));
		} catch (error) {
			console.error("Error saving printer selection:", error);
		}
	},

	/**
	 * Load the stored A4 printer selection for customs documents
	 * @returns {object|null}
	 */
	loadSelectedDocumentPrinter: function () {
		try {
			const stored = localStorage.getItem(this.STORAGE_KEYS.documentPrinter);
			return stored ? JSON.parse(stored) : null;
		} catch (error) {
			console.error("Error loading stored document printer selection:", error);
			return null;
		}
	},

	/**
	 * Persist the selected A4 printer for customs documents (or clear when null)
	 * @param {object|null} printerData
	 */
	saveSelectedDocumentPrinter: function (printerData) {
		try {
			if (!printerData) {
				localStorage.removeItem(this.STORAGE_KEYS.documentPrinter);
				return;
			}
			localStorage.setItem(
				this.STORAGE_KEYS.documentPrinter,
				JSON.stringify(printerData)
			);
		} catch (error) {
			console.error("Error saving document printer selection:", error);
		}
	},

	/**
	 * Remove any stored printer selection
	 */
	clearSelectedPrinter: function () {
		try {
			localStorage.removeItem(this.STORAGE_KEYS.printer);
		} catch (error) {
			console.error("Error clearing printer selection from storage:", error);
		}
	},
};
