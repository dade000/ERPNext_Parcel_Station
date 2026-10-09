from __future__ import annotations
import logging, os
import frappe

def get_logger(name: str = "parcel_station") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    try:
        site_path = frappe.utils.get_site_path("logs")
        os.makedirs(site_path, exist_ok=True)
        logfile = os.path.join(site_path, "parcel_station.log")
        fh = logging.handlers.RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=3)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        # Fallback to stderr if site path not ready
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        logger.addHandler(sh)
    return logger
