frappe.pages["parcel-station"].on_page_load = function (wrapper) {
	let page = frappe.ui.make_app_page({
		parent: wrapper,
		title: "Parcel Station",
		single_column: true,
	});

	// Load all required modules from public assets
	frappe.require(
		[
			"/assets/erpnext_parcel_station/js/parcel_station/parcel_station_styles.js",
			"/assets/erpnext_parcel_station/js/parcel_station/parcel_station_templates.js",
			"/assets/erpnext_parcel_station/js/parcel_station/parcel_station_storage.js",
			"/assets/erpnext_parcel_station/js/parcel_station/parcel_station_api.js",
			"/assets/erpnext_parcel_station/js/parcel_station/parcel_station_ui.js",
			"/assets/erpnext_parcel_station/js/parcel_station/parcel_station_scanner_capture.js",
			"/assets/erpnext_parcel_station/js/hwbridge.js",
			"/assets/erpnext_parcel_station/js/parcel_station/parcel_station_weight.js",
			"/assets/erpnext_parcel_station/js/parcel_station/parcel_station_manager.js",
		],
		function () {
			// Initialize once modules are loaded
			if (
				window.ParcelStationManager &&
				typeof window.ParcelStationManager.bootstrap === "function"
			) {
				let container = wrapper;
				if (page && page.body && page.body.length) {
					container = page.body[0];
				} else if (page && page.main && page.main.length) {
					container = page.main[0];
				}
				window.ParcelStationManager.bootstrap(container);
			} else {
				console.error("Parcel Station modules failed to load", {
					styles: typeof window.ParcelStationStyles,
					templates: typeof window.ParcelStationTemplates,
					storage: typeof window.ParcelStationStorage,
					api: typeof window.ParcelStationAPI,
					ui: typeof window.ParcelStationUI,
					scannerCapture: typeof window.ParcelStationScannerCapture,
					manager: typeof window.ParcelStationManager,
				});
				frappe.msgprint(
					"Failed to load Parcel Station modules. Please clear cache and reload."
				);
			}
		}
	);
};
