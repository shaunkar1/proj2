"""Scanned and outlined pages (switch DEID_PDF_OCR=1; macOS only).

Pages without a text layer - scans, drawings exported with their lettering converted to
curves - carry their title blocks as pixels or outlines.  The page is rendered, read with the
system's text recognition (Apple Vision), the recognised text goes through the same engine as
ordinary document text, and every word the engine replaces is covered on the page by a white
box with the pseudonym written into it.  Everything else on the page stays as it is.

Needs PyMuPDF and pyobjc-framework-Vision; without them the document is returned unchanged.
"""
import os
import re

_STATE = {}
# a word run together with the decimal that follows it ("approximately.008"): a reading slip,
# not a part number
_GLUED = re.compile(r"^[A-Za-z]{2,}\.{1,2}\d+\W?$")


def available():
    if "ok" not in _STATE:
        try:
            import fitz  # noqa: F401
            import Vision  # noqa: F401
            _STATE["ok"] = True
        except Exception:
            _STATE["ok"] = False
    return _STATE["ok"]


def _recognise(png):
    """[[(word, x0, y0, x1, y1), ...] per line]; coordinates relative to the picture, origin
    top left, 0..1."""
    import Vision
    from Foundation import NSData, NSMakeRange
    data = NSData.dataWithBytes_length_(png, len(png))
    handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(data, None)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)
    handler.performRequests_error_([req], None)
    lines = []
    for obs in (req.results() or []):
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        cand = cands[0]
        if float(cand.confidence()) < 0.3:
            continue
        s = str(cand.string())
        words = []
        pos = 0
        for w in s.split():
            k = s.find(w, pos)
            pos = k + len(w)
            try:
                rng, _err = cand.boundingBoxForRange_error_(NSMakeRange(k, len(w)), None)
            except Exception:
                rng = None
            if rng is None:
                continue
            b = rng.boundingBox()
            x0, y0, bw, bh = float(b.origin.x), float(b.origin.y), float(b.size.width), float(b.size.height)
            words.append((w, x0, 1.0 - y0 - bh, x0 + bw, 1.0 - y0))
        if words:
            lines.append(words)
    return lines


def redact(data, engine, ctx, recognise=None, max_pages=None, dpi=None):
    """Return the PDF with the names on its text-less pages covered and replaced."""
    try:
        import fitz
    except Exception:
        return data
    recognise = recognise or _recognise
    max_pages = max_pages or int(os.environ.get("DEID_OCR_PAGES", "40"))
    dpi = dpi or int(os.environ.get("DEID_OCR_DPI", "200"))
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return data
    changed = 0
    try:
        for pno in range(min(len(doc), max_pages)):
            page = doc[pno]
            try:
                if len(page.get_text("text").split()) >= 8:
                    continue                      # the page has a text layer: handled elsewhere
                if page.rotation:
                    page.remove_rotation()        # same appearance, plain coordinates
                rect = page.rect
                scale = dpi / 72.0
                if max(rect.width, rect.height) * scale > 6000:
                    scale = 6000.0 / max(rect.width, rect.height)
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY, alpha=False)
                lines = recognise(pix.tobytes("png"))
            except Exception:
                continue
            if not lines:
                continue
            words = []
            buf = []
            pos = 0
            for line in lines:
                for (w, x0, y0, x1, y1) in line:
                    words.append((pos, pos + len(w), w, x0, y0, x1, y1))
                    buf.append(w)
                    buf.append(" ")
                    pos += len(w) + 1
                buf[-1] = "\n"
            text = "".join(buf)
            try:
                new = engine.scrub_text(text, ctx, "prose")
            except Exception:
                continue
            if new == text or len(new) != len(text):
                continue
            todo = []
            for (s, e, w, x0, y0, x1, y1) in words:
                rep = new[s:e]
                if rep == w or _GLUED.match(w):
                    continue
                r = fitz.Rect(rect.x0 + x0 * rect.width, rect.y0 + y0 * rect.height,
                              rect.x0 + x1 * rect.width, rect.y0 + y1 * rect.height)
                if r.is_empty or r.height < 1 or r.width < 1:
                    continue
                todo.append((r, rep))
            if not todo:
                continue
            try:
                # pixels under the boxes are removed from scanned pictures; outlined lettering
                # (drawn as curves) is covered
                for r, rep in todo:
                    pad = 0.06 * r.height
                    page.add_redact_annot(fitz.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad), fill=(1, 1, 1))
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS, graphics=fitz.PDF_REDACT_LINE_ART_NONE)
            except Exception:
                pass
            for r, rep in todo:
                try:
                    pad = 0.06 * r.height
                    page.draw_rect(fitz.Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad),
                                   color=None, fill=(1, 1, 1), overlay=True, width=0)
                    upright = len(rep) >= 3 and r.height > 2.0 * r.width     # lettering running up the sheet
                    thick, long = (r.width, r.height) if upright else (r.height, r.width)
                    size = max(3.0, thick * 0.8)
                    width = fitz.get_text_length(rep, fontname="helv", fontsize=size)
                    if width > long and width > 0:
                        size = max(2.0, size * long / width)
                    if upright:
                        page.insert_text(fitz.Point(r.x1 - 0.2 * r.width, r.y1), rep, fontsize=size,
                                         fontname="helv", color=(0, 0, 0), rotate=90, overlay=True)
                    else:
                        page.insert_text(fitz.Point(r.x0, r.y1 - 0.2 * r.height), rep, fontsize=size,
                                         fontname="helv", color=(0, 0, 0), overlay=True)
                    changed += 1
                except Exception:
                    continue
        if not changed:
            return data
        ctx.notes.append("%d names on pages without a text layer covered and rewritten (text recognition)" % changed)
        return doc.tobytes(garbage=3, deflate=True)
    except Exception:
        return data
    finally:
        try:
            doc.close()
        except Exception:
            pass
