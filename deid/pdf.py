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
HEXSTR = re.compile(rb"<([0-9A-Fa-f\s]{2,})>")


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


class _Font(object):
    """Code <-> Unicode tables of one PDF font (None tables = bytes are text)."""
    __slots__ = ("width", "to_uni", "from_uni", "ok")

    def __init__(self):
        self.width = 1
        self.to_uni = None
        self.from_uni = None
        self.ok = True


_BFCHAR = re.compile(rb"beginbfchar(.*?)endbfchar", re.S)
_BFRANGE = re.compile(rb"beginbfrange(.*?)endbfrange", re.S)
_HEX = re.compile(rb"<([0-9A-Fa-f\s]*)>")
_RANGE_LINE = re.compile(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(\[[^\]]*\]|<[0-9A-Fa-f\s]*>)")


def _hex_to_str(h):
    try:
        raw = bytes.fromhex(re.sub(rb"\s", b"", h).decode("ascii"))
        return raw.decode("utf-16-be", "replace")
    except Exception:
        return ""


def _parse_tounicode(data):
    table = {}
    for blk in _BFCHAR.findall(data):
        items = _HEX.findall(blk)
        for i in range(0, len(items) - 1, 2):
            try:
                table[int(items[i], 16)] = _hex_to_str(items[i + 1])
            except Exception:
                pass
    for blk in _BFRANGE.findall(data):
        for lo, hi, dst in _RANGE_LINE.findall(blk):
            try:
                lo_i, hi_i = int(lo, 16), int(hi, 16)
            except Exception:
                continue
            if hi_i - lo_i > 70000:
                continue
            if dst[:1] == b"[":
                vals = _HEX.findall(dst)
                for k, v in enumerate(vals):
                    if lo_i + k <= hi_i:
                        table[lo_i + k] = _hex_to_str(v)
            else:
                base = _hex_to_str(dst[1:-1])
                if not base:
                    continue
                for k in range(hi_i - lo_i + 1):
                    table[lo_i + k] = base[:-1] + chr(ord(base[-1]) + k)
    return table


def _font_info(font, cache):
    key = getattr(font, "objgen", None)
    if key in cache and key != (0, 0):
        return cache[key]
    info = _Font()
    try:
        subtype = str(font.get("/Subtype", ""))
        enc = font.get("/Encoding")
        if subtype == "/Type0":
            info.width = 2
            if enc is None or str(enc) not in ("/Identity-H", "/Identity-V"):
                info.ok = False
        elif isinstance(enc, pikepdf.Dictionary) and "/Differences" in enc and "/ToUnicode" not in font:
            info.ok = False
        tu = font.get("/ToUnicode")
        if tu is not None and info.ok:
            table = _parse_tounicode(tu.read_bytes())
            if table:
                info.to_uni = table
                rev = {}
                for code, text in table.items():
                    if len(text) == 1 and text not in rev:
                        rev[text] = code
                info.from_uni = rev
            elif info.width == 2:
                info.ok = False
        elif info.width == 2:
            info.ok = False
    except Exception:
        info.ok = False
    if key and key != (0, 0):
        cache[key] = info
    return info


def _decode(raw, info):
    """bytes -> (text, spans) where spans[i] = (start, end) of code i in the text."""
    if info.to_uni is None:
        return raw.decode("latin-1"), None
    w = info.width
    parts = []
    spans = []
    pos = 0
    for i in range(0, len(raw) - w + 1, w):
        code = int.from_bytes(raw[i:i + w], "big")
        t = info.to_uni.get(code)
        if t is None:
            t = "�"
        parts.append(t)
        spans.append((pos, pos + len(t)))
        pos += len(t)
    return "".join(parts), spans


def _encode_changes(raw, old, new, spans, info):
    """Re-encode ``raw`` so that it shows ``new`` instead of ``old``; None if a glyph is missing."""
    if info.to_uni is None:
        try:
            return new.encode("latin-1")
        except UnicodeEncodeError:
            return None
    w = info.width
    out = bytearray(raw)
    for idx, (a, b) in enumerate(spans):
        if old[a:b] == new[a:b]:
            continue
        if b - a != 1:
            return None
        code = info.from_uni.get(new[a:b])
        if code is None:
            return None
        out[idx * w:idx * w + w] = code.to_bytes(w, "big")
    return bytes(out)


def _helv(pdf, holder):
    if holder.get("font") is None:
        holder["font"] = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name("/Font"), Subtype=pikepdf.Name("/Type1"),
            BaseFont=pikepdf.Name("/Helvetica"), Encoding=pikepdf.Name("/WinAnsiEncoding")))
    return holder["font"]


def _strings_of(raw):
    out = [m.group(1) for m in LITERAL.finditer(raw)]
    for m in HEXSTR.finditer(raw):
        try:
            out.append(bytes.fromhex(re.sub(rb"\s", b"", m.group(1)).decode("ascii")))
        except Exception:
            pass
    return out


def _content_stream(target, raw, engine, ctx, resources=None, pdf=None, fcache=None, holder=None):
    """Rewrite the text operands of a page (all its content streams together) or of a
    form XObject.  Returns the new content bytes, or None if nothing changed."""
    if not raw or not HAS_TEXT.search(raw):
        return None
    stream = target
    fonts = None
    try:
        if resources is not None:
            fonts = resources.get("/Font")
    except Exception:
        fonts = None
    fcache = {} if fcache is None else fcache
    infos = []
    if fonts is not None:
        try:
            for k in fonts.keys():
                fi = _font_info(fonts[k], fcache)
                if fi.to_uni is not None and fi.ok:
                    infos.append(fi)
        except Exception:
            pass
    # cheap pre-check: would anything change in the string operands at all?
    pieces = _strings_of(raw)
    if not pieces:
        return None
    probes = [b"\n".join(pieces), b"".join(pieces)]
    for fi in infos[:6]:
        try:
            dec = [_decode(pc, fi)[0] for pc in pieces]
            probes.append("\n".join(dec).encode("utf-8", "replace"))
            probes.append("".join(dec).encode("utf-8", "replace"))
        except Exception:
            pass
    if all(engine.scrub(pr, ctx, False, "") == pr for pr in probes):
        return None
    try:
        ops = pikepdf.parse_content_stream(stream)
    except Exception:
        return None
    changed = False
    need_helv = False
    new_ops = []
    cur = None          # (font name, size operand)
    stack = []
    default = _Font()
    for operands, operator in ops:
        op = str(operator)
        if op == "q":
            stack.append(cur)
        elif op == "Q":
            cur = stack.pop() if stack else cur
        elif op == "Tf" and len(operands) >= 2:
            cur = (operands[0], operands[1])
        elif op in TEXT_OPS and operands:
            info = default
            if cur is not None and fonts is not None:
                try:
                    info = _font_info(fonts[str(cur[0])], fcache)
                except Exception:
                    info = default
            if info.ok:
                operands = list(operands)
                if op == "TJ" and isinstance(operands[0], pikepdf.Array):
                    arr = list(operands[0])
                    idx = [i for i in range(len(arr)) if isinstance(arr[i], pikepdf.String)]
                else:
                    arr = None
                    idx = [len(operands) - 1] if isinstance(operands[-1], pikepdf.String) else []
                src = arr if arr is not None else operands
                pieces = [bytes(src[i]) for i in idx]
                decoded = [_decode(pc, info) for pc in pieces]
                old = "".join(d[0] for d in decoded)
                if old and "�" not in old:
                    new = engine.scrub_text(old, ctx)
                    if new != old and len(new) == len(old):
                        pos = 0
                        enc = []
                        for (txt, spans), pc in zip(decoded, pieces):
                            seg_old = old[pos:pos + len(txt)]
                            seg_new = new[pos:pos + len(txt)]
                            pos += len(txt)
                            enc.append(pc if seg_old == seg_new else _encode_changes(pc, seg_old, seg_new, spans, info))
                        if all(e is not None for e in enc):
                            for i, e in zip(idx, enc):
                                src[i] = pikepdf.String(e)
                            if arr is not None:
                                operands[0] = pikepdf.Array(arr)
                            changed = True
                        elif cur is not None and pdf is not None and resources is not None:
                            # the subset font lacks a glyph: draw this string with Helvetica
                            try:
                                shown = new.encode("cp1252")
                            except UnicodeEncodeError:
                                shown = None
                            if shown is not None:
                                if op in ("'", '"'):
                                    new_ops.append(([], pikepdf.Operator("T*")))
                                new_ops.append(([pikepdf.Name("/DeidHelv"), cur[1]], pikepdf.Operator("Tf")))
                                new_ops.append(([pikepdf.String(shown)], pikepdf.Operator("Tj")))
                                new_ops.append(([cur[0], cur[1]], pikepdf.Operator("Tf")))
                                need_helv = True
                                changed = True
                                continue
        new_ops.append((operands, operator))
    if changed:
        if need_helv:
            try:
                if "/Font" not in resources:
                    resources["/Font"] = pikepdf.Dictionary()
                resources["/Font"]["/DeidHelv"] = _helv(pdf, holder if holder is not None else {})
            except Exception:
                return None
        return pikepdf.unparse_content_stream(new_ops)
    return None


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
    # page content streams, with the resources (fonts) they are drawn with.  A page's
    # streams are parsed together because text state carries across them.
    content_ids = set()
    fcache = {}
    holder = {}
    try:
        for page in pdf.pages:
            c = page.obj.get("/Contents")
            if c is None:
                continue
            try:
                res = page.resources
            except Exception:
                res = None
            items = list(c) if isinstance(c, pikepdf.Array) else [c]
            fresh = False
            for it in items:
                og = getattr(it, "objgen", None)
                if og not in content_ids:
                    fresh = True
                content_ids.add(og)
            if not fresh:
                continue
            try:
                raw = b"\n".join(it.read_bytes() for it in items)
                new = _content_stream(page, raw, engine, ctx, res, pdf, fcache, holder)
                if new is not None:
                    page.obj["/Contents"] = pdf.make_stream(new)
                    changed = True
            except Exception as exc:
                ctx.notes.append("pdf page text skipped (%s)" % type(exc).__name__)
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
                elif getattr(obj, "objgen", None) in content_ids:
                    pass
                elif sub == "/Form" or "/PatternType" in obj:
                    new = _content_stream(obj, obj.read_bytes(), engine, ctx, obj.get("/Resources"), pdf, fcache, holder)
                    if new is not None:
                        obj.write(new)
                        changed = True
                if _walk(obj, engine, ctx, set()):
                    changed = True
            elif isinstance(obj, (pikepdf.Dictionary, pikepdf.Array)):
                if _walk(obj, engine, ctx, set()):
                    changed = True
        except Exception as exc:
            ctx.notes.append("pdf object skipped (%s)" % type(exc).__name__)
    return changed
