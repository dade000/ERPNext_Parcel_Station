// "Read from scale" action on the Shipment form.
//
// Adds a button in the form's primary action area. Click → server-side
// `read_scale_weight` calls the warehouse scale's HTTP endpoint and returns
// `{status: "success", weight_kg}` or `{status: "error", code, message}`.
//
// On success the live weight is written into the LAST row of the
// `shipment_parcel` child table. If there are no rows yet, one is added.
// This matches the typical pack-and-weigh flow: operator adds a parcel row,
// places the package on the scale, clicks the button, weight populates.
// For multiple parcels: add another row, click again, weight populates the
// new last row.
//
// Errors map to friendly toasts so the operator knows whether to re-plug
// the scale, contact admin about credentials, etc.
//
// Source order: the local hardware bridge first (scale at THIS PC, read by
// the browser); only without a bridge or scale the server-side endpoint.

frappe.ui.form.on("Shipment", {
  refresh(frm) {
    frm.add_custom_button(__("Read from scale"), () => read_scale_into_last_parcel(frm));
    render_tracking_events(frm);
  },
});

// ---------------------------------------------------------------------------
// Sendungsverlauf: the Parcel Tracking Events for this Shipment, rendered as a
// dashboard section so the history is visible without leaving the form. The
// timeline is deliberately NOT used for this (house rule: comments there are
// for label operations; a parcel produces dozens of tracking events).
// ---------------------------------------------------------------------------

const TRACKING_STATUS_COLORS = {
  "In Progress": "gray",
  Announced: "gray",
  "In Transit": "blue",
  "Out for Delivery": "blue",
  "Ready for Pickup": "orange",
  Delivered: "green",
  Problem: "red",
  Returned: "red",
  Lost: "red",
};

function render_tracking_events(frm) {
  if (frm.is_new()) return;

  frappe.db
    .get_list("Parcel Tracking Event", {
      filters: { shipment: frm.doc.name },
      fields: [
        "event_timestamp",
        "canonical_status",
        "description",
        "event_code",
        "reason_code",
        "location_city",
        "location_postal_code",
        "location_country",
        "carrier",
      ],
      order_by: "event_timestamp desc",
      limit: 50,
    })
    .then((events) => {
      if (!events || !events.length) return;

      const rows = events
        .map((ev) => {
          const when = frappe.datetime.str_to_user(ev.event_timestamp);
          const color = TRACKING_STATUS_COLORS[ev.canonical_status] || "gray";
          const code = [ev.event_code, ev.reason_code].filter(Boolean).join("/");
          const text = ev.description || code;
          const where = [ev.location_city || ev.location_postal_code, ev.location_country]
            .filter(Boolean)
            .join(", ");
          return `<tr>
            <td class="text-nowrap">${frappe.utils.escape_html(when)}</td>
            <td class="text-nowrap"><span class="indicator-pill ${color}">${frappe.utils.escape_html(
              __(ev.canonical_status || "")
            )}</span></td>
            <td>${frappe.utils.escape_html(text)}
              ${text !== code && code ? `<span class="text-muted small"> (${frappe.utils.escape_html(code)})</span>` : ""}
            </td>
            <td>${frappe.utils.escape_html(where)}</td>
          </tr>`;
        })
        .join("");

      const more =
        events.length >= 50
          ? `<p class="text-muted small mb-0">${__("Nur die letzten 50 Events — vollständige Liste über den Button.")}</p>`
          : "";

      const html = `
        <div class="tracking-events" style="max-height: 320px; overflow-y: auto;">
          <table class="table table-sm" style="margin-bottom: var(--margin-sm);">
            <thead><tr>
              <th>${__("Zeitpunkt")}</th><th>${__("Status")}</th>
              <th>${__("Ereignis")}</th><th>${__("Ort")}</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
          ${more}
        </div>`;

      frm.dashboard.add_section(html, __("Sendungsverlauf"));
      frm.dashboard.show();

      frm.add_custom_button(__("Tracking Events"), () => {
        frappe.set_route("List", "Parcel Tracking Event", { shipment: frm.doc.name });
      });
    });
}

function write_weight_to_last_parcel(frm, weight_kg, source) {
  const rows = frm.doc.shipment_parcel || [];
  let target = rows[rows.length - 1];
  if (!target) {
    target = frm.add_child("shipment_parcel", { count: 1 });
  }
  frappe.model.set_value(target.doctype, target.name, "weight", weight_kg);
  frm.refresh_field("shipment_parcel");
  frappe.show_alert({
    message: __("Weight from scale: {0} kg", [weight_kg]) + (source ? ` (${source})` : ""),
    indicator: "green",
  });
}

/** Scale via the local hardware bridge. Resolves null when there is none. */
async function read_scale_via_bridge() {
  try {
    await frappe.require("/assets/erpnext_parcel_station/js/hwbridge.js");
  } catch (e) {
    return null;
  }
  if (!window.HardwareBridge) return null;
  const hw = window.HardwareBridge.shared({ client: "shipment_form" });
  if (hw.state === "connecting") {
    await new Promise((resolve) => {
      const done = () => {
        hw.off("state", done);
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(done, 2000);
      hw.on("state", done);
    });
  }
  if (hw.deviceState("scale") !== "ready") return null;
  try {
    return await hw.call("scale.read");
  } catch (e) {
    return { error: e };
  }
}

async function read_scale_into_last_parcel(frm) {
  const bridged = await read_scale_via_bridge();
  if (bridged && !bridged.error) {
    if (!bridged.stable) {
      frappe.show_alert({ message: __("Note: the weight was still moving."), indicator: "orange" });
    }
    write_weight_to_last_parcel(frm, bridged.kg, __("hardware bridge"));
    return;
  }
  if (bridged && bridged.error) {
    frappe.show_alert({
      message: __("Scale error: {0}", [bridged.error.message || bridged.error.code]),
      indicator: "red",
    });
    return;
  }

  frappe.dom.freeze(__("Reading scale..."));
  frappe
    .call({
      method: "erpnext_parcel_station.parcel.api.scale.read_scale_weight",
      // no args
    })
    .then((r) => {
      frappe.dom.unfreeze();
      const result = r && r.message;
      if (!result) {
        frappe.show_alert({ message: __("No response from scale."), indicator: "red" });
        return;
      }

      if (result.status === "success" && typeof result.weight_kg === "number") {
        write_weight_to_last_parcel(frm, result.weight_kg);
        return;
      }

      // Error branches — map server-side `code` to a distinct toast.
      const copy = {
        not_configured: __(
          "Scale isn't configured. Ask admin to set scale_url / username / password in site_config.json."
        ),
        scale_offline: __(
          "Scale isn't connected to the server. Plug it in and try again."
        ),
        auth: __("Scale rejected the credentials. Check site_config.json."),
        network: __("Could not reach the scale server. Check the network."),
        timeout: __("Scale server didn't respond in time. Try again."),
        bad_response: __("Scale returned an unexpected response."),
        unexpected: __("Unexpected error from scale: {0}", [result.message || ""]),
      };
      const msg = copy[result.code] || result.message || __("Scale error.");
      frappe.show_alert({ message: msg, indicator: "red" });
    })
    .catch((err) => {
      frappe.dom.unfreeze();
      console.error("Scale read failed:", err);
      frappe.show_alert({
        message: __("Scale call failed. See browser console."),
        indicator: "red",
      });
    });
}
