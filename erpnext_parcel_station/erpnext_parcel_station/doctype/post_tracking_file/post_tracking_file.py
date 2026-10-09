# Copyright (c) 2026, Devich Daniel and Contributors
# See license.txt
from frappe.model.document import Document


class PostTrackingFile(Document):
    """Ingestion log for one POSTTRACK XML fetched from the Post SFTP server.

    Mirrors the Bank Statement File pattern from the banking import: one row
    per remote file, deduplicated by file name (autoname) and content hash,
    with the raw XML attached as a private File. Created exclusively by
    ``parcel/tracking/post_sftp.fetch_post_tracking_files``.
    """
