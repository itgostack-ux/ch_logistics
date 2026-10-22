import frappe
from frappe.model.document import Document


class CHLogisticsSettings(Document):
    def on_update(self):
        self._protect_fcm_key_file()

    def _protect_fcm_key_file(self):
        """The Firebase service-account JSON is a credential.

        Frappe lets anyone who can read the parent document download its
        attachments, and Delivery Manager reads these settings. Keep the file
        private and unattached so only its uploader / System Manager can fetch
        it; fcm.py reads it server-side by file_url.
        """
        file_url = self.get("fcm_service_account_file")
        if not file_url:
            return
        for name in frappe.get_all("File", filters={"file_url": file_url}, pluck="name"):
            file_doc = frappe.get_doc("File", name)
            if file_doc.is_private and not file_doc.attached_to_doctype:
                continue
            # save(), not db_set: File moves a public file into private/files
            # and rewrites file_url when is_private flips.
            file_doc.is_private = 1
            file_doc.attached_to_doctype = None
            file_doc.attached_to_name = None
            file_doc.attached_to_field = None
            file_doc.save(ignore_permissions=True)
            if file_doc.file_url != file_url:
                self.db_set("fcm_service_account_file", file_doc.file_url)


def get_settings():
    """Cached accessor for the single. Falls back to sane defaults if the doc
    has not been initialised yet."""
    return frappe.get_cached_doc("CH Logistics Settings")
