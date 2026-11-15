"""Passwords, permissions, redaction, sanitising, watermarks and digital signatures."""
import io
import secrets
import tempfile
from pathlib import Path
from typing import Annotated

import pymupdf
from fastapi import Form

from ..core import PDF, PERMS, Color, Opacity, Pages, Secret, ToolError, Upload, find_text, parse_pages, put, rgb, tool
from .other import scripts, strip_scripts


@tool("Add password", "Security")
def protect(doc: PDF, user_password: Secret = "", owner_password: Secret = "", allow_print: bool = True,
            allow_copy: bool = True, allow_modify: bool = True, allow_annotate: bool = True, allow_fill_forms: bool = True):
    """Encrypt with AES-256. The user password is needed to open the file. The owner password lifts the
    restrictions below - set only that one to restrict a file anyone can open."""
    if not (user_password or owner_password):
        raise ToolError("Set a user password, an owner password, or both")
    allowed = {"print": allow_print, "copy": allow_copy, "modify": allow_modify, "annotate": allow_annotate, "fill_forms": allow_fill_forms}
    permissions = pymupdf.PDF_PERM_ACCESSIBILITY | sum(PERMS[k] for k, on in allowed.items() if on)
    return ("_protected.pdf", doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw=user_password or None,
                                          owner_pw=owner_password or user_password, permissions=permissions,
                                          garbage=3, deflate=True))


@tool("Remove password", "Security")
def unlock(doc: PDF):
    """Save a copy with no password and no restrictions. Enter the current password below."""
    return doc


@tool("Sanitize", "Security")
def sanitize(doc: PDF, javascript: bool = True, attachments: bool = True, metadata: bool = True, links: bool = True,
             hidden_text: bool = False):
    """Strip active and hidden content before sharing a file. Hidden text also removes OCR layers, so it is off by default."""
    doc.scrub(javascript=javascript, attached_files=attachments, embedded_files=attachments, metadata=metadata,
              xml_metadata=metadata, remove_links=links, hidden_text=hidden_text, reset_fields=False, reset_responses=False)
    if javascript:
        strip_scripts(doc)
    data = doc.tobytes(garbage=3, deflate=True)
    if javascript and scripts(pymupdf.open("pdf", data)):  # never hand back a "sanitized" file that still has scripts
        raise ToolError("Some scripts could not be removed from this file")
    return ("_sanitize.pdf", data)


@tool("Redact text", "Security")
def redact(doc: PDF, find: Annotated[str, Form(description="Text to remove, one term per line", json_schema_extra={"format": "textarea"})],
           regex: bool = False, color: Color = "#000000", pages: Pages = "all"):
    """Permanently remove every occurrence of the given text - and any image pixels under it - and cover the
    spot with a box. Matching ignores case. With regex on, each line is a regular expression."""
    hits = 0
    for i in set(parse_pages(pages, len(doc))):
        quads = find_text(doc[i], find, regex)
        for q in quads:
            doc[i].add_redact_annot(q, fill=rgb(color))
        doc[i].apply_redactions()
        hits += len(quads)
    if not hits:
        raise ToolError("No matching text found, so nothing was redacted")
    return doc


@tool("Add watermark", "Security")
def watermark(doc: PDF, text: str = "CONFIDENTIAL", image: Annotated[Upload | None, "image/*"] = None,
              size: Annotated[float, Form(ge=4, le=500, description="Text size in points")] = 48,
              image_width: Annotated[float, Form(gt=0, le=100, description="Image width, in percent of the page width")] = 40,
              opacity: Opacity = 0.25, rotation: Annotated[float, Form(description="Degrees counter-clockwise")] = 45,
              color: Color = "#808080", tile: bool = False, pages: Pages = "all"):
    """Print text across each page, or an image if you choose one. Tile repeats it over the whole page."""
    for i in set(parse_pages(pages, len(doc))):
        put(doc[i], "tile" if tile else "center", text=text, image=image and image.data, size=size,
            image_width=image_width, color=color, opacity=opacity, angle=rotation)
    return doc


@tool("Sign with certificate", "Security", accept=".pdf")
def sign(file: Upload, certificate: Annotated[Upload, ".p12,.pfx"], certificate_password: Secret = "",
         reason: str = "", location: str = "", visible: bool = False):
    """Add a digital signature from a PKCS#12 certificate (.p12 or .pfx). Earlier signatures stay valid.
    "Visible" also prints the signer's name at the bottom left of the first page."""
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers

    with tempfile.TemporaryDirectory() as d:
        Path(d, "cert.p12").write_bytes(certificate.data)
        signer = signers.SimpleSigner.load_pkcs12(str(Path(d, "cert.p12")), passphrase=certificate_password.encode() or None)
    if signer is None:
        raise ToolError("Can't open the certificate: wrong password, or not a PKCS#12 file")
    name = f"Signature{secrets.token_hex(4)}"
    try:
        writer = IncrementalPdfFileWriter(io.BytesIO(file.data), strict=False)
        if visible:
            fields.append_signature_field(writer, fields.SigFieldSpec(name, on_page=0, box=(36, 36, 236, 96)))
        meta = signers.PdfSignatureMetadata(field_name=name, reason=reason or None, location=location or None)
        return ("_signed.pdf", signers.sign_pdf(writer, meta, signer=signer).getvalue())
    except Exception as e:
        raise ToolError(f"Can't sign this PDF (remove its password first if it has one): {e}") from e


@tool("Validate signatures", "Security", accept=".pdf")
def validate_signatures(file: Upload):
    """Check each digital signature: who signed, whether the file changed since, and whether the certificate
    chains to an authority this server trusts."""
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature

    try:
        found = PdfFileReader(io.BytesIO(file.data), strict=False).embedded_signatures
    except Exception as e:
        raise ToolError(f"{file.name} is not a readable PDF") from e
    report = []
    for sig in found:
        status = validate_pdf_signature(sig)
        report.append({"field": sig.field_name, "signer": status.signing_cert.subject.human_friendly,
                       "unchanged_since_signing": status.intact and status.valid, "trusted_certificate": status.trusted,
                       "covers": status.coverage.name, "signed_at": status.signer_reported_dt})
    return {"signed": bool(report), "signatures": report}


@tool("Remove signatures", "Security")
def remove_signatures(doc: PDF):
    """Delete digital signature fields."""
    for page in doc:
        for widget in list(page.widgets(types=[pymupdf.PDF_WIDGET_TYPE_SIGNATURE])):
            page.delete_widget(widget)
    return doc
