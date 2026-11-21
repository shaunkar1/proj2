"""PDF handling (pikepdf / qpdf).

  * document information dictionary and XMP metadata packets;
  * every string in every dictionary / array (annotation authors, bookmarks, form
    values, file-specification names ...);
  * page, form-XObject and pattern content streams: the operands of the text-showing
    operators (Tj, ', ", TJ).  The pieces of one TJ array are matched as one string and
    written back piece by piece, which works because replacements keep their length;
  * embedded files (attachments) are processed recursively with the normal handlers.

The file is rewritten in full, so earlier revisions kept by incremental saves are dropped.
If a PDF cannot be parsed it falls back to a same-length pass over its raw bytes.
"""
import io
import re

try:
    import pikepdf
except Exception:  # pragma: no cover
    pikepdf = None

IDENTITY_KEYS = ("/Author", "/Company", "/Manager", "/LastModifiedBy", "/Owner", "/T")
SKIP_KEYS = ("/ID", "/Registry", "/Ordering", "/CheckSum", "/Panose", "/CharSet", "/BaseFont", "/FontName",
             "/O", "/U", "/OE", "/UE", "/Perms", "/Contents_")
TEXT_OPS = ("Tj", "'", '"', "TJ")
HAS_TEXT = re.compile(rb"(?:Tj|TJ|'|\")")
LITERAL = re.compile(rb"\(((?:[^()\\]|\\.)*)\)")
HEXSTR = re.compile(rb"<([0-9A-Fa-f\s]{4,})>")


def process(path, data, engine, ctx, depth):
    if pikepdf is None:
        ctx.notes.append("pikepdf missing: PDF handled by raw pass only")
        return engine.scrub(data, ctx, True, "")
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except Exception as exc:
        ctx.notes.append("PDF could not be parsed (%s); raw pass only" % type(exc).__name__)
        return engine.scrub(data, ctx, True, "")
    try:
        changed = _process(pdf, path, engine, ctx, depth)
        if not changed:
            # nothing in the live document; still clean plain-text leftovers (old
            # incremental revisions, unreferenced objects) without moving any offset
            return engine.scrub(data, ctx, True, "")
        out = io.BytesIO()
        pdf.save(out, object_stream_mode=pikepdf.ObjectStreamMode.preserve, compress_streams=True)
        return out.getvalue()
    finally:
        pdf.close()


def _scrub_pdf_string(obj, engine, ctx, identity=False):
    """Return a replacement pikepdf.String or None."""
    raw = bytes(obj)
    if not raw:
        return None
    if raw[:2] == b"\xfe\xff":
        try:
            t = raw[2:].decode("utf-16-be")
        except Exception:
            return None
        new = engine.scrub_field(t, ctx, "pdf-author") if identity else engine.scrub_text(t, ctx)
        if new == t or len(new) != len(t):
            return None
        return pikepdf.String(b"\xfe\xff" + new.encode("utf-16-be"))
    if identity:
        t = raw.decode("latin-1")
        new = engine.scrub_field(t, ctx, "pdf-author")
        return None if new == t else pikepdf.String(new.encode("latin-1"))
    new = engine.scrub(raw, ctx, False, "")
    return None if new == raw else pikepdf.String(new)


def _walk(obj, engine, ctx, seen, depth=0):
    """Scrub strings in nested direct dictionaries / arrays.  Returns True if changed."""
    changed = False
    if depth > 40:
        return False
    if isinstance(obj, pikepdf.Dictionary) or isinstance(obj, pikepdf.Stream):
        keys = list(obj.keys())
        for k in keys:
            if k in SKIP_KEYS:
                continue
            try:
                v = obj[k]
            except Exception:
                continue
            if isinstance(v, pikepdf.String):
                ident = k in IDENTITY_KEYS and not (k == "/T" and "/Subtype" not in obj)
                rep = _scrub_pdf_string(v, engine, ctx, identity=ident)
                if rep is not None:
                    obj[k] = rep
                    changed = True
            elif isinstance(v, (pikepdf.Dictionary, pikepdf.Array)) and not v.is_indirect:
                if _walk(v, engine, ctx, seen, depth + 1):
                    changed = True
    elif isinstance(obj, pikepdf.Array):
        for i in range(len(obj)):
            try:
                v = obj[i]
            except Exception:
                continue
            if isinstance(v, pikepdf.String):
                rep = _scrub_pdf_string(v, engine, ctx)
                if rep is not None:
                    obj[i] = rep
                    changed = True
            elif isinstance(v, (pikepdf.Dictionary, pikepdf.Array)) and not v.is_indirect:
                if _walk(v, engine, ctx, seen, depth + 1):
                    changed = True
    return changed


def _content_stream(stream, engine, ctx):
    """Rewrite text operands of a content stream.  Returns True if changed."""
    try:
        raw = stream.read_bytes()
    except Exception:
        return False
    if not raw or not HAS_TEXT.search(raw):
        return False
    # cheap pre-check: would anything change in the string operands at all?
    pieces = [m.group(1) for m in LITERAL.finditer(raw)]
    for m in HEXSTR.finditer(raw):
        try:
            pieces.append(bytes.fromhex(re.sub(rb"\s", b"", m.group(1)).decode("ascii")))
        except Exception:
            pass
    if not pieces:
        return False
    sep = b"\n".join(pieces)
    cat = b"".join(pieces)
    if engine.scrub(sep, ctx, False, "") == sep and engine.scrub(cat, ctx, False, "") == cat:
        return False
    try:
        ops = pikepdf.parse_content_stream(stream)
    except Exception:
        return False
    changed = False
    new_ops = []
    for operands, operator in ops:
        op = str(operator)
        if op in TEXT_OPS and operands:
            operands = list(operands)
            if op == "TJ" and isinstance(operands[0], pikepdf.Array):
                arr = operands[0]
                idx = [i for i in range(len(arr)) if isinstance(arr[i], pikepdf.String)]
                pieces = [bytes(arr[i]) for i in idx]
                joined = b"".join(pieces)
                new = engine.scrub(joined, ctx, False, "")
                if new != joined:
                    pos = 0
                    items = list(arr)
                    for i, piece in zip(idx, pieces):
                        items[i] = pikepdf.String(new[pos:pos + len(piece)])
                        pos += len(piece)
                    operands[0] = pikepdf.Array(items)
                    changed = True
            else:
                j = len(operands) - 1
                if isinstance(operands[j], pikepdf.String):
                    b = bytes(operands[j])
                    new = engine.scrub(b, ctx, False, "")
                    if new != b:
                        operands[j] = pikepdf.String(new)
                        changed = True
        new_ops.append((operands, operator))
    if changed:
        stream.write(pikepdf.unparse_content_stream(new_ops))
    return changed


def _process(pdf, path, engine, ctx, depth):
    from . import handlers
    changed = False
    done = set()
    # 1. document information dictionary (first: names found here apply to the pages)
    try:
        info = pdf.trailer.get("/Info")
        if info is not None:
            if _walk(info, engine, ctx, set()):
                changed = True
            try:
                done.add(info.objgen)
            except Exception:
                pass
    except Exception:
        pass
    # which streams are page contents?
    content_ids = set()
    try:
        for page in pdf.pages:
            c = page.obj.get("/Contents")
            if c is None:
                continue
            items = list(c) if isinstance(c, pikepdf.Array) else [c]
            for it in items:
                try:
                    content_ids.add(it.objgen)
                except Exception:
                    pass
    except Exception:
        pass
    for obj in pdf.objects:
        try:
            if getattr(obj, "objgen", None) in done:
                continue
            if isinstance(obj, pikepdf.Stream):
                typ = str(obj.get("/Type", ""))
                sub = str(obj.get("/Subtype", ""))
                if typ == "/Metadata" or sub == "/XML":
                    raw = obj.read_bytes()
                    new = engine.scrub(raw, ctx, raw[:2] in (b"\xff\xfe", b"\xfe\xff"), "xml")
                    if new != raw:
                        obj.write(new)
                        changed = True
                elif typ == "/EmbeddedFile":
                    raw = obj.read_bytes()
                    sub_ctx = handlers.Ctx(path + "/attachment", ctx.scope)
                    new = handlers.process("attachment.bin", raw, engine, sub_ctx, depth + 1)
                    ctx.findings.extend(sub_ctx.findings[:50])
                    if new != raw:
                        obj.write(new)
                        changed = True
                elif sub == "/Form" or getattr(obj, "objgen", None) in content_ids or "/PatternType" in obj:
                    if _content_stream(obj, engine, ctx):
                        changed = True
                if _walk(obj, engine, ctx, set()):
                    changed = True
            elif isinstance(obj, (pikepdf.Dictionary, pikepdf.Array)):
                if _walk(obj, engine, ctx, set()):
                    changed = True
        except Exception as exc:
            ctx.notes.append("pdf object skipped (%s)" % type(exc).__name__)
    return changed
