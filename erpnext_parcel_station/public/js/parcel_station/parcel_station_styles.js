/**
 * Parcel Station Styles
 * Contains all CSS styles for the parcel station interface
 */
window.ParcelStationStyles = {
	getStyles: function () {
		return `
	            <style id="parcel-station-styles">
	                /* The station is a workplace screen: it uses the full width of
	                   the Desk page and its own height, so the work list scrolls
	                   on its own while scan field and buttons stay in place. */
	                .page-body.ps-full-width,
	                .ps-full-width .page-content,
	                .ps-full-width .layout-main-section-wrapper {
	                    max-width: none;
	                    width: 100%;
	                }
	                .ps-full-width .layout-main-section {
	                    padding: 0;
	                    border: 0;
	                    background: transparent;
	                    box-shadow: none;
	                }

	                #parcel-station-root {
	                    --ps-ground: #eef0ee;
	                    --ps-surface: #ffffff;
	                    --ps-line: #d9dedb;
	                    --ps-line-strong: #a9b2ad;
	                    --ps-ink: #16201c;
	                    --ps-muted: #56605b;
	                    --ps-accent: #0b6b5d;
	                    --ps-accent-dark: #084f45;
	                    --ps-accent-soft: #e3f1ee;
	                    --ps-warn: #6b4700;
	                    --ps-warn-soft: #fff1cc;
	                    --ps-danger: #a4262c;
	                    --ps-danger-soft: #fdecec;
	                    --ps-mono: var(--font-stack-monospace, ui-monospace, "SFMono-Regular", Menlo, Consolas, monospace);

	                    /* The whole station is drawn at 90 %: at full size a
	                       loaded delivery note with its result did not fit a
	                       1080p screen without scrolling. The height is divided
	                       by the same factor so the station still fills the
	                       window. */
	                    --ps-zoom: 0.9;
	                    zoom: var(--ps-zoom);

	                    display: flex;
	                    flex-direction: column;
	                    height: calc((100vh - 150px) / var(--ps-zoom));
	                    min-height: 620px;
	                    background: var(--ps-ground);
	                    color: var(--ps-ink);
	                    border: 1px solid var(--ps-line);
	                    border-radius: 12px;
	                    overflow: hidden;
	                    font-size: 15px;
	                    line-height: 1.35;
	                }
	                #parcel-station-root [hidden] { display: none !important; }
	                #parcel-station-root h2 { margin: 0; font-size: 18px; font-weight: 700; color: var(--ps-ink); }
	                #parcel-station-root .ps-mono { font-family: var(--ps-mono); font-variant-numeric: tabular-nums; }
	                #parcel-station-root .ps-right { text-align: right; }
	                #parcel-station-root .ps-label { font-size: 13px; color: var(--ps-muted); margin: 0; font-weight: 400; }
	                #parcel-station-root .ps-hint { font-size: 13px; color: var(--ps-muted); }

	                /* ---- buttons ------------------------------------------------ */
	                #parcel-station-root .ps-btn {
	                    display: inline-flex; align-items: center; justify-content: center; gap: 8px;
	                    min-height: 44px; padding: 0 16px;
	                    border: 1px solid var(--ps-line-strong); border-radius: 8px;
	                    background: var(--ps-surface); color: var(--ps-ink);
	                    font: inherit; font-size: 15px; cursor: pointer; white-space: nowrap;
	                }
	                #parcel-station-root .ps-btn:hover:not(:disabled) { border-color: var(--ps-ink); }
	                #parcel-station-root .ps-btn:disabled { color: var(--ps-muted); background: var(--ps-ground); cursor: not-allowed; border-color: var(--ps-line); }
	                #parcel-station-root .ps-btn:focus-visible,
	                #parcel-station-root .ps-chip:focus-visible,
	                #parcel-station-root .ps-icon-btn:focus-visible,
	                #parcel-station-root .ps-queue-row:focus-visible { outline: 3px solid var(--ps-accent); outline-offset: 2px; }
	                #parcel-station-root .ps-btn-small { min-height: 40px; padding: 0 12px; font-size: 14px; }
	                #parcel-station-root .ps-btn-large { min-height: 56px; padding: 0 28px; font-size: 18px; font-weight: 600; }
	                #parcel-station-root .ps-btn-primary { background: var(--ps-accent); border-color: var(--ps-accent); color: #fff; font-weight: 600; }
	                #parcel-station-root .ps-btn-primary:hover:not(:disabled) { background: var(--ps-accent-dark); border-color: var(--ps-accent-dark); }
	                #parcel-station-root .ps-btn-primary:disabled { background: var(--ps-ground); border-color: var(--ps-line); color: var(--ps-muted); }
	                #parcel-station-root .ps-btn-danger { border-color: var(--ps-danger); color: var(--ps-danger); }
	                #parcel-station-root .ps-link { border: 0; background: none; padding: 4px 2px; font: inherit; font-size: 13px; color: inherit; text-decoration: underline; cursor: pointer; }
	                #parcel-station-root .ps-icon-btn {
	                    width: 44px; height: 44px; display: inline-flex; align-items: center; justify-content: center;
	                    border: 0; border-radius: 8px; background: transparent; color: var(--ps-ink); cursor: pointer;
	                }
	                #parcel-station-root .ps-icon-btn:hover { background: var(--ps-ground); }
	                #parcel-station-root .ps-count { font-family: var(--ps-mono); font-weight: 600; }
	                #parcel-station-root .ps-count-warn { padding: 1px 8px; border-radius: 999px; background: var(--ps-warn-soft); color: var(--ps-warn); }

	                /* ---- header ------------------------------------------------- */
	                #parcel-station-root .ps-top {
	                    position: relative; flex-shrink: 0;
	                    display: flex; align-items: center; justify-content: space-between; gap: 16px 24px; flex-wrap: wrap;
	                    padding: 10px 20px; background: var(--ps-surface); border-bottom: 1px solid var(--ps-line);
	                }
	                #parcel-station-root .ps-progress { display: flex; align-items: center; gap: 12px; }
	                #parcel-station-root .ps-progress-bar { width: 220px; height: 8px; border-radius: 999px; background: var(--ps-line); overflow: hidden; }
	                #parcel-station-root .ps-progress-fill { width: 0; height: 100%; background: var(--ps-accent); transition: width .3s ease; }
	                #parcel-station-root .ps-progress-text b { font-weight: 600; }
	                #parcel-station-root .ps-progress-text span { color: var(--ps-muted); }
	                #parcel-station-root .ps-top-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
	                #parcel-station-root .ps-chip {
	                    display: inline-flex; align-items: center; gap: 8px; min-height: 36px; padding: 0 12px;
	                    border: 1px solid var(--ps-line); border-radius: 999px; background: var(--ps-surface);
	                    color: var(--ps-ink); font: inherit; font-size: 14px; cursor: pointer; max-width: 260px;
	                }
	                #parcel-station-root .ps-chip-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	                #parcel-station-root .ps-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ps-line-strong); flex-shrink: 0; }
	                #parcel-station-root .ps-chip[data-state="ok"] .ps-dot { background: var(--ps-accent); }
	                #parcel-station-root .ps-chip[data-state="warn"] { background: var(--ps-warn-soft); border-color: #e0b34a; color: var(--ps-warn); }
	                #parcel-station-root .ps-chip[data-state="warn"] .ps-dot { background: var(--ps-warn); border-radius: 2px; }
	                #parcel-station-root .ps-chip[data-state="error"] { background: var(--ps-danger-soft); border-color: var(--ps-danger); color: var(--ps-danger); }
	                #parcel-station-root .ps-chip[data-state="error"] .ps-dot { background: var(--ps-danger); border-radius: 2px; }

	                #parcel-station-root .ps-panel {
	                    position: absolute; top: calc(100% + 6px); right: 20px; z-index: 20;
	                    width: 420px; max-width: calc(100% - 40px); padding: 16px;
	                    background: var(--ps-surface); border: 1px solid var(--ps-line-strong); border-radius: 10px;
	                    box-shadow: 0 8px 24px rgba(22, 32, 28, 0.18);
	                    display: flex; flex-direction: column; gap: 6px;
	                }
	                #parcel-station-root .ps-panel-title { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
	                #parcel-station-root .ps-panel .ps-label { margin-top: 6px; }
	                #parcel-station-root .ps-select {
	                    width: 100%; min-height: 44px; padding: 0 10px; border: 1px solid var(--ps-line-strong); border-radius: 8px;
	                    background: var(--ps-surface); color: var(--ps-ink); font: inherit;
	                }
	                #parcel-station-root .ps-panel-status { font-weight: 600; }
	                #parcel-station-root .ps-panel-list { font-size: 14px; color: var(--ps-muted); }
	                #parcel-station-root .ps-panel-foot { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
	                #parcel-station-root .ps-text-warn { color: var(--ps-warn); }
	                #parcel-station-root .ps-text-ok { color: var(--ps-accent-dark); }
	                #parcel-station-root .ps-text-danger { color: var(--ps-danger); }

	                /* ---- body: work list + parcel ------------------------------- */
	                #parcel-station-root .ps-body { flex: 1 1 auto; min-height: 0; display: flex; }
	                #parcel-station-root .ps-queue {
	                    width: 400px; flex-shrink: 0; display: flex; flex-direction: column; min-height: 0;
	                    background: var(--ps-surface); border-right: 1px solid var(--ps-line);
	                }
	                #parcel-station-root .ps-queue-head { padding: 14px 16px 12px 20px; display: flex; flex-direction: column; gap: 8px; }
	                #parcel-station-root .ps-queue-title { display: flex; align-items: center; justify-content: space-between; }
	                #parcel-station-root .ps-queue-title h2 { font-size: 20px; }
	                #parcel-station-root .ps-filters { display: flex; gap: 8px; flex-wrap: wrap; }
	                #parcel-station-root .ps-filter {
	                    min-height: 40px; padding: 0 14px; border: 1px solid var(--ps-line); border-radius: 999px;
	                    background: var(--ps-surface); color: var(--ps-ink); font: inherit; font-size: 14px; cursor: pointer;
	                }
	                #parcel-station-root .ps-filter[aria-pressed="true"] { background: var(--ps-ink); border-color: var(--ps-ink); color: #fff; font-weight: 600; }
	                #parcel-station-root .ps-queue-scroll { flex: 1 1 auto; min-height: 0; overflow-y: auto; }
	                #parcel-station-root .ps-queue-section {
	                    display: flex; align-items: center; justify-content: space-between; gap: 8px;
	                    padding: 7px 20px 6px; font-size: 12px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase;
	                    color: var(--ps-muted); background: var(--ps-ground);
	                }
	                #parcel-station-root .ps-queue-section-warn { color: var(--ps-warn); background: var(--ps-warn-soft); }
	                #parcel-station-root .ps-queue-section-warn .ps-link { text-transform: none; letter-spacing: 0; font-weight: 400; }
	                #parcel-station-root .ps-queue-row {
	                    display: flex; align-items: center; justify-content: space-between; gap: 12px; width: 100%;
	                    min-height: 66px; padding: 9px 20px 9px 16px; text-align: left;
	                    border: 0; border-left: 4px solid transparent; border-bottom: 1px solid var(--ps-line);
	                    background: var(--ps-surface); color: var(--ps-ink); font: inherit; cursor: pointer;
	                }
	                #parcel-station-root .ps-queue-row:hover { background: #f7f8f7; }
	                #parcel-station-root .ps-queue-row[aria-current="true"] { background: var(--ps-accent-soft); border-left-color: var(--ps-accent); }
	                #parcel-station-root .ps-queue-main { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
	                #parcel-station-root .ps-queue-name { font-size: 16px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
	                #parcel-station-root .ps-queue-meta { font-size: 13px; color: var(--ps-muted); }
	                #parcel-station-root .ps-queue-side { display: flex; align-items: center; gap: 6px; flex-shrink: 0; flex-wrap: wrap; justify-content: flex-end; max-width: 150px; }
	                #parcel-station-root .ps-queue-empty { padding: 20px; color: var(--ps-muted); }
	                #parcel-station-root .ps-flag { padding: 2px 8px; border-radius: 999px; background: var(--ps-warn-soft); color: var(--ps-warn); font-size: 12px; font-weight: 600; white-space: nowrap; }
	                #parcel-station-root .ps-flag-danger { background: var(--ps-danger-soft); color: var(--ps-danger); }
	                #parcel-station-root .ps-carrier {
	                    padding: 3px 9px; border: 1px solid var(--ps-ink); border-radius: 6px;
	                    font-family: var(--ps-mono); font-weight: 600; font-size: 12px; white-space: nowrap;
	                }

	                /* On a low screen the column scrolls as a whole rather than
	                   squeezing the packing list to a single row. */
	                #parcel-station-root .ps-work { flex: 1 1 auto; min-width: 0; min-height: 0; padding: 20px; display: flex; flex-direction: column; gap: 12px; overflow-y: auto; }
	                #parcel-station-root .ps-scan {
	                    flex-shrink: 0; display: flex; align-items: center; gap: 12px; min-height: 56px; padding: 0 16px;
	                    border: 2px solid var(--ps-accent); border-radius: 10px; background: var(--ps-surface);
	                }
	                #parcel-station-root .ps-scan label { margin: 0; font-size: 14px; font-weight: 400; color: var(--ps-muted); }
	                #parcel-station-root .ps-scan input {
	                    flex: 1 1 auto; min-width: 0; height: 44px; border: 0; outline: 0; background: transparent;
	                    font: inherit; font-size: 17px; color: var(--ps-ink); box-shadow: none;
	                }
	                #parcel-station-root .ps-scan:focus-within { box-shadow: 0 0 0 3px var(--ps-accent-soft); }

	                #parcel-station-root .ps-message {
	                    flex-shrink: 0; position: relative; padding: 10px 44px 10px 14px; border-radius: 8px;
	                    border: 1px solid var(--ps-line-strong); background: var(--ps-surface); font-size: 15px;
	                }
	                #parcel-station-root .ps-message[data-type="success"] { background: var(--ps-accent-soft); border-color: var(--ps-accent); color: var(--ps-accent-dark); }
	                #parcel-station-root .ps-message[data-type="warning"] { background: var(--ps-warn-soft); border-color: #e0b34a; color: var(--ps-warn); }
	                #parcel-station-root .ps-message[data-type="danger"] { background: var(--ps-danger-soft); border-color: var(--ps-danger); color: var(--ps-danger); font-weight: 600; }
	                #parcel-station-root #ps-message-close {
	                    position: absolute; top: 0; right: 0; width: 44px; height: 100%; max-height: 44px;
	                    border: 0; background: transparent; color: inherit; font-size: 22px; line-height: 1; cursor: pointer;
	                }

	                #parcel-station-root .ps-card {
	                    flex: 1 0 auto; display: flex; flex-direction: column;
	                    background: var(--ps-surface); border: 1px solid var(--ps-line); border-radius: 12px; overflow: hidden;
	                }
	                #parcel-station-root .ps-head {
	                    display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px;
	                    padding: 16px 24px; border-bottom: 1px solid var(--ps-line);
	                }
	                #parcel-station-root .ps-head-strong { font-size: 17px; font-weight: 600; overflow-wrap: anywhere; }
	                #parcel-station-root .ps-head-text { font-size: 14px; }
	                #parcel-station-root .ps-head-carrier { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }

	                #parcel-station-root .ps-result { padding: 14px 24px 16px; border-bottom: 1px solid var(--ps-line); background: #f7f8f7; }
	                #parcel-station-root .ps-result-head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
	                #parcel-station-root .ps-result-title { font-size: 17px; font-weight: 700; }
	                #parcel-station-root .ps-result[data-state="done"] .ps-result-title { color: var(--ps-accent-dark); }
	                #parcel-station-root .ps-result[data-state="problem"] .ps-result-title { color: var(--ps-danger); }
	                #parcel-station-root .ps-result-id { font-family: var(--ps-mono); font-size: 14px; color: var(--ps-muted); }
	                #parcel-station-root .ps-result-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 20px; margin-top: 10px; }
	                #parcel-station-root .ps-result-tracking { font-size: 17px; font-weight: 600; overflow-wrap: anywhere; }
	                #parcel-station-root .ps-result-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 2px; }
	                #parcel-station-root .ps-result-actions a { color: var(--ps-accent-dark); text-decoration: underline; font-size: 14px; }

	                #parcel-station-root .ps-packlist { flex: 1 1 0; min-height: 190px; display: flex; flex-direction: column; }
	                #parcel-station-root .ps-packlist-head { display: flex; align-items: baseline; justify-content: space-between; gap: 16px; padding: 14px 24px 6px; }
	                #parcel-station-root .ps-items { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 0 24px; }
	                #parcel-station-root .ps-empty { padding: 28px 0; font-size: 17px; color: var(--ps-muted); }
	                #parcel-station-root .ps-items-head,
	                #parcel-station-root .ps-item { display: grid; grid-template-columns: 56px minmax(0, 1fr) 90px 110px; gap: 12px; align-items: center; }
	                #parcel-station-root .ps-items-head {
	                    position: sticky; top: 0; background: var(--ps-surface); padding: 8px 0; border-bottom: 2px solid var(--ps-ink);
	                    font-size: 12px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--ps-muted);
	                }
	                #parcel-station-root .ps-item { min-height: 60px; margin: 0; border-bottom: 1px solid var(--ps-line); cursor: pointer; font-weight: 400; }
	                #parcel-station-root .ps-item-check { display: flex; align-items: center; justify-content: center; min-height: 44px; }
	                #parcel-station-root .ps-item .form-check-input { width: 26px; height: 26px; margin: 0; position: static; cursor: pointer; accent-color: var(--ps-accent); }
	                #parcel-station-root .ps-item-name { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
	                #parcel-station-root .ps-item-name > span { font-size: 16px; }
	                #parcel-station-root .ps-item-name small { font-size: 13px; color: var(--ps-muted); }
	                #parcel-station-root .ps-item-qty { font-size: 18px; font-weight: 600; }
	                #parcel-station-root .ps-item-weight { font-size: 15px; color: var(--ps-muted); }

	                /* Inputs on the left may wrap; the two buttons keep their place
	                   on the right, whatever the scale and the dimensions show. */
	                #parcel-station-root .ps-foot {
	                    flex-shrink: 0; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 12px 24px;
	                    padding: 12px 24px; border-top: 1px solid var(--ps-line); background: #f7f8f7;
	                }
	                #parcel-station-root .ps-foot-inputs { display: flex; align-items: flex-start; gap: 10px 28px; flex-wrap: wrap; }
	                #parcel-station-root .ps-foot-block { display: flex; flex-direction: column; gap: 4px; }
	                #parcel-station-root .ps-foot-label { display: flex; align-items: center; gap: 8px; min-height: 28px; font-size: 13px; color: var(--ps-muted); }
	                #parcel-station-root .ps-foot-actions { display: flex; align-items: center; gap: 12px; }
	                #parcel-station-root .ps-pill { padding: 1px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; background: var(--ps-ground); color: var(--ps-ink); border: 1px solid var(--ps-line); }
	                #parcel-station-root .ps-pill[data-tone="ok"] { background: var(--ps-accent-soft); color: var(--ps-accent-dark); border-color: transparent; }
	                #parcel-station-root .ps-pill[data-tone="warn"] { background: var(--ps-warn-soft); color: var(--ps-warn); border-color: transparent; }
	                #parcel-station-root .ps-pill[data-tone="error"] { background: var(--ps-danger-soft); color: var(--ps-danger); border-color: transparent; }

	                #parcel-station-root .ps-weight-row { display: flex; align-items: flex-end; gap: 12px; }
	                #parcel-station-root .ps-weight-value { font-family: var(--ps-mono); font-variant-numeric: tabular-nums; font-size: 32px; font-weight: 600; line-height: 1.1; min-width: 96px; align-self: center; }
	                #parcel-station-root .ps-weight-value.ps-weight-moving { opacity: .55; }
	                #parcel-station-root .ps-weight-manual { display: flex; flex-direction: column; gap: 2px; }
	                #parcel-station-root .ps-weight-manual label,
	                #parcel-station-root .ps-dims-field label { margin: 0; font-size: 12px; font-weight: 400; color: var(--ps-muted); }
	                #parcel-station-root .ps-weight-manual input,
	                #parcel-station-root .ps-dims-field input {
	                    width: 76px; height: 44px; padding: 0 10px; border: 1px solid var(--ps-line-strong); border-radius: 8px;
	                    background: var(--ps-surface); color: var(--ps-ink); font-family: var(--ps-mono); font-size: 18px; font-weight: 600; box-shadow: none;
	                }
	                #parcel-station-root .ps-weight-manual input:focus,
	                #parcel-station-root .ps-dims-field input:focus { outline: 0; border: 2px solid var(--ps-accent); }
	                #parcel-station-root .ps-dims-field input[aria-invalid="true"] { border: 2px solid var(--ps-danger); }
	                #parcel-station-root .ps-weight-status { font-size: 12px; color: var(--ps-muted); max-width: 260px; }
	                #parcel-station-root .ps-weight-status:empty { display: none; }
	                #parcel-station-root .ps-weight-status a { color: inherit; text-decoration: underline; }

	                #parcel-station-root .ps-dims-scan { display: flex; align-items: center; gap: 14px; min-height: 44px; }
	                #parcel-station-root .ps-dims-prompt { font-size: 18px; font-weight: 600; color: var(--ps-warn); }
	                #parcel-station-root .ps-dims[data-state="set"] .ps-dims-prompt { color: var(--ps-ink); font-family: var(--ps-mono); }
	                #parcel-station-root .ps-dims-manual { display: flex; align-items: flex-end; gap: 8px; }
	                #parcel-station-root .ps-dims-field { display: flex; flex-direction: column; gap: 2px; }
	                #parcel-station-root .ps-dims-unit { align-self: flex-end; padding-bottom: 12px; font-size: 14px; color: var(--ps-muted); }

	                @media (max-width: 1100px) {
	                    #parcel-station-root { height: auto; }
	                    #parcel-station-root .ps-body { flex-direction: column; }
	                    #parcel-station-root .ps-queue { width: auto; border-right: 0; border-bottom: 1px solid var(--ps-line); max-height: 320px; }
	                    #parcel-station-root .ps-head,
	                    #parcel-station-root .ps-result-grid { grid-template-columns: minmax(0, 1fr); gap: 12px; }
	                    #parcel-station-root .ps-foot { grid-template-columns: minmax(0, 1fr); }
	                    #parcel-station-root .ps-items { overflow: visible; }
	                }
	            </style>
	        `;
	},
};
