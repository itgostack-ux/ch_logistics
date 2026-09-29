"""Say "Manifest Created" where a manifest's status says "Packed".

"Packed" means two different things one document apart: on a Stock Entry it
is the boxes being packed, and on a manifest it only means the paperwork
exists and is waiting for a trip. The Logistics Control Tower has always
renamed the manifest one; the list and the form did not, so the same 109
documents read two different ways depending on the screen.

The stored value does not change — filters, reports, the API and every
status check keep working off "Packed". Only the word on screen does, and it
is scoped to this doctype by the translation's context, so "Packed"
everywhere else (the Stock Entry above all) is untouched.

frappe.get_indicator renders a Document State through
``__(doc.status, null, doctype)``, which is exactly this lookup — which is
why a translation, and not a client-side label map, is what the list view
actually honours.
"""

import frappe

_LABELS = {"Packed": "Manifest Created"}
_CONTEXT = "CH Transfer Manifest"


def execute():
    language = frappe.db.get_single_value("System Settings", "language") or "en"
    for source, translated in _LABELS.items():
        existing = frappe.db.exists(
            "Translation",
            {"source_text": source, "context": _CONTEXT, "language": language},
        )
        if existing:
            frappe.db.set_value("Translation", existing, "translated_text", translated)
            continue
        doc = frappe.new_doc("Translation")
        doc.language = language
        doc.source_text = source
        doc.context = _CONTEXT
        doc.translated_text = translated
        doc.insert(ignore_permissions=True)
    frappe.db.commit()
    frappe.clear_cache()
