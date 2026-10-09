// Copyright (c) 2026, Devich Daniel and Contributors
// See license.txt

frappe.ui.form.on('Austrian Post Settings', {
    refresh(frm) {
        if (frm.doc.enable_post_tracking) {
            frm.add_custom_button(__('Tracking-Dateien jetzt abholen'), () => {
                frappe.call({
                    method: 'erpnext_parcel_station.parcel.tracking.post_sftp.fetch_now',
                    freeze: true,
                    freeze_message: __('Hole Tracking-Dateien vom Post-SFTP...'),
                    callback(r) {
                        const s = r.message || {};
                        const failed = s.failed || [];
                        let msg = __(
                            'Neue Dateien: {0}, importiert: {1}, Duplikate: {2}, aktualisierte Sendungen: {3}',
                            [s.files || 0, s.imported || 0, s.duplicates || 0, (s.shipments || []).length]
                        );
                        if (failed.length) {
                            msg += '<br><b>' + __('Fehlgeschlagen: {0}', [failed.join(', ')]) + '</b><br>'
                                + __('Details im Error Log und in den Post Tracking File Dokumenten.');
                        }
                        frappe.msgprint({
                            message: msg,
                            indicator: failed.length ? 'orange' : 'green',
                            title: __('Post-Tracking-Import'),
                        });
                    },
                });
            });
        }
    },
});
