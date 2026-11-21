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
_HINT = re.compile(rb"@|:\\|[Uu]sers[\\/]|https?:|www\.")
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
    __slots__ = ("width", "to_uni", "from_uni", "ok", "adv", "adv_default", "embedded", "used")

    def __init__(self):
        self.width = 1
        self.to_uni = None
        self.from_uni = None
        self.ok = True
        self.adv = None          # code -> advance in 1/1000 em
        self.adv_default = None
        self.embedded = False
        self.used = set()        # codes this document already draws with the font

    def advance(self, raw):
        """Advance of a string in 1/1000 em, or None when widths are unknown."""
        if self.adv is None:
            return None
        total = 0
        w = self.width
        for i in range(0, len(raw) - w + 1, w):
            code = int.from_bytes(raw[i:i + w], "big")
            a = self.adv.get(code, self.adv_default)
            if a is None:
                return None
            total += a
        return total


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


_GLYPH_NAMES = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#", "dollar": "$", "percent": "%",
    "ampersand": "&", "quotesingle": "'", "quoteright": "\u2019", "parenleft": "(", "parenright": ")",
    "asterisk": "*", "plus": "+", "comma": ",", "hyphen": "-", "minus": "-", "period": ".", "slash": "/",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "colon": ":", "semicolon": ";", "less": "<", "equal": "=", "greater": ">",
    "question": "?", "at": "@", "bracketleft": "[", "backslash": "\\", "bracketright": "]",
    "asciicircum": "^", "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|", "braceright": "}",
    "asciitilde": "~", "degree": "\u00b0", "plusminus": "\u00b1", "endash": "\u2013", "emdash": "\u2014",
    "bullet": "\u2022", "nbspace": " ", "Oslash": "\u00d8", "oslash": "\u00f8", "multiply": "\u00d7",
}


def _differences_table(enc):
    """code -> text for a simple font with an /Encoding dictionary (/Differences)."""
    table = {c: chr(c) for c in range(32, 127)}
    try:
        code = 0
        for item in enc.get("/Differences", []):
            if isinstance(item, pikepdf.Name):
                name = str(item)[1:]
                if len(name) == 1:
                    table[code] = name
                elif name in _GLYPH_NAMES:
                    table[code] = _GLYPH_NAMES[name]
                elif name.startswith("uni") and len(name) == 7:
                    try:
                        table[code] = chr(int(name[3:], 16))
                    except ValueError:
                        table[code] = "\ufffd"
                else:
                    table[code] = "\ufffd"
                code += 1
            else:
                code = int(item)
    except Exception:
        return None
    return table


def _font_info(font, cache):
    key = getattr(font, "objgen", None)
    if key in cache and key != (0, 0):
        return cache[key]
    info = _Font()
    try:
        subtype = str(font.get("/Subtype", ""))
        enc = font.get("/Encoding")
        fd = font.get("/FontDescriptor")
        if fd is not None and any(k in fd for k in ("/FontFile", "/FontFile2", "/FontFile3")):
            info.embedded = True
        if subtype == "/Type0":
            info.width = 2
            if enc is None or str(enc) not in ("/Identity-H", "/Identity-V"):
                info.ok = False
        elif isinstance(enc, pikepdf.Dictionary) and "/Differences" in enc and "/ToUnicode" not in font:
            table = _differences_table(enc)
            if table is None:
                info.ok = False
            else:
                info.to_uni = table
                rev = {}
                listed = set()
                code = 0
                for item in enc.get("/Differences", []):
                    if isinstance(item, pikepdf.Name):
                        listed.add(code)
                        code += 1
                    else:
                        code = int(item)
                for c, t in table.items():
                    # an embedded subset only holds the glyphs it lists
                    if len(t) == 1 and t != "\ufffd" and t not in rev and (c in listed or not info.embedded):
                        rev[t] = c
                info.from_uni = rev
        try:
            if subtype == "/Type0":
                df = font["/DescendantFonts"][0]
                info.adv_default = float(df.get("/DW", 1000))
                info.adv = {}
                warr = df.get("/W")
                if warr is not None:
                    items = list(warr)
                    i = 0
                    while i < len(items) and i < 40000:
                        first = int(items[i])
                        nxt = items[i + 1]
                        if isinstance(nxt, pikepdf.Array):
                            for k, wv in enumerate(nxt):
                                info.adv[first + k] = float(wv)
                            i += 2
                        else:
                            last = int(nxt)
                            wv = float(items[i + 2])
                            if last - first < 70000:
                                for c in range(first, last + 1):
                                    info.adv[c] = wv
                            i += 3
            elif "/Widths" in font:
                fc = int(font.get("/FirstChar", 0))
                factor = 1.0
                if subtype == "/Type3" and "/FontMatrix" in font:
                    factor = abs(float(font["/FontMatrix"][0])) * 1000.0
                info.adv = {fc + k: float(wv) * factor for k, wv in enumerate(font["/Widths"])}
        except Exception:
            info.adv = None
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
            enc = new.encode("latin-1")
        except UnicodeEncodeError:
            return None
        if info.embedded:
            # an embedded font program may hold only the glyphs the document uses
            for o, n in zip(raw, enc):
                if o != n and n not in info.used:
                    return None
        return enc
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


_NEEDED_OPS = frozenset(("q", "Q", "cm", "BT", "Tf", "Tm", "Td", "TD", "T*", "Tj", "TJ", "'", '"'))


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
    probes = [b"\n".join(pieces), b"".join(pieces)]   # odd indexes: concatenated forms
    for fi in infos[:6]:
        try:
            dec = [_decode(pc, fi)[0] for pc in pieces]
            probes.append("\n".join(dec).encode("utf-8", "replace"))
            probes.append("".join(dec).encode("utf-8", "replace"))
        except Exception:
            pass
    glyphwise = len(pieces) > 40 and sum(len(pc) for pc in pieces) < 3 * len(pieces)
    if (all(engine.scrub(pr, ctx, False, "prose") == pr for pr in probes)
            and not any(engine.maybe(pr, ctx) or _HINT.search(pr) for pr in probes[1::2])
            and not (glyphwise and len(pieces) < 60000)):
        return None
    try:
        ops = pikepdf.parse_content_stream(stream)
    except Exception:
        return None
    # only the operators that place or show text are unpacked; everything else (the
    # drawing's line work) is carried through untouched
    lazy = []
    for ins in ops:
        try:
            opr = ins.operator
            if str(opr) in _NEEDED_OPS:
                lazy.append((list(ins.operands), opr))
            else:
                lazy.append((None, ins))
        except Exception:
            lazy.append((None, ins))
    ops = lazy
    default = _Font()
    ident = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

    def mul(m, n):
        return (m[0] * n[0] + m[1] * n[2], m[0] * n[1] + m[1] * n[3],
                m[2] * n[0] + m[3] * n[2], m[2] * n[1] + m[3] * n[3],
                m[4] * n[0] + m[5] * n[2] + n[4], m[4] * n[1] + m[5] * n[3] + n[5])

    def num(x):
        try:
            return float(x)
        except Exception:
            return 0.0

    ctm = ident
    tm = tlm = ident
    cur = None
    stack = []
    units = []        # one per string piece
    vtext = []
    vmap = []
    last = None       # (start position, advance or None, size, nchars) of the previous text op
    moved = True      # a positioning operator ran since the previous text op
    for oi, (operands, operator) in enumerate(ops):
        if operands is None:
            continue
        op = str(operator)
        if op == "q":
            stack.append((ctm, cur))
        elif op == "Q":
            if stack:
                ctm, cur = stack.pop()
        elif op == "cm" and len(operands) >= 6:
            ctm = mul(tuple(num(x) for x in operands[:6]), ctm)
        elif op == "BT":
            tm = tlm = ident
            moved = True
        elif op == "Tf" and len(operands) >= 2:
            cur = (operands[0], operands[1])
        elif op == "Tm" and len(operands) >= 6:
            tm = tlm = tuple(num(x) for x in operands[:6])
            moved = True
        elif op in ("Td", "TD") and len(operands) >= 2:
            tlm = mul((1.0, 0.0, 0.0, 1.0, num(operands[0]), num(operands[1])), tlm)
            tm = tlm
            moved = True
        elif op == "T*":
            moved = True
        elif op in TEXT_OPS and operands:
            info = default
            if cur is not None and fonts is not None:
                try:
                    info = _font_info(fonts[str(cur[0])], fcache)
                except Exception:
                    info = default
            if op in ("'", '"'):
                moved = True
            if not info.ok:
                last = None
                moved = True
                continue
            if op == "TJ" and isinstance(operands[0], pikepdf.Array):
                arr = list(operands[0])
                operands[0] = arr
                src = arr
            else:
                src = operands
                arr = None
            first_piece = True
            size = abs(num(cur[1])) if cur is not None else 1.0
            full = mul(tm, ctm)
            scale = (full[0] ** 2 + full[1] ** 2) ** 0.5 or 1.0
            eff = size * scale or 1.0
            pos = (full[4], full[5])
            op_adv = 0.0
            op_chars = 0
            pending_space = False
            for pi, item in enumerate(src):
                if not isinstance(item, pikepdf.String):
                    if arr is not None and not isinstance(item, (pikepdf.Array, pikepdf.Dictionary)):
                        if num(item) <= -250:
                            pending_space = True
                    continue
                if arr is None and pi != len(src) - 1:
                    continue
                rawp = bytes(item)
                txt, spans = _decode(rawp, info)
                if not txt:
                    continue
                if info.to_uni is None and info.embedded:
                    info.used.update(rawp)
                sep = ""
                if first_piece:
                    if not vtext:
                        sep = ""
                    elif not moved:
                        sep = ""
                    elif last is None:
                        sep = "\n"
                    else:
                        (lx, ly), ladv, lsize, ln = last
                        dist = ((pos[0] - lx) ** 2 + (pos[1] - ly) ** 2) ** 0.5
                        ref = max(eff, lsize)
                        if ladv is not None:
                            gap = dist - ladv
                            sep = "" if abs(gap) <= 0.2 * ref else " " if 0 < gap <= 1.3 * ref else "\n"
                        elif ln == 1:
                            sep = "" if dist <= 0.85 * ref else " " if dist <= 1.7 * ref else "\n"
                        else:
                            sep = "\n"
                elif pending_space:
                    sep = " "
                pending_space = False
                first_piece = False
                if sep:
                    vtext.append(sep)
                    vmap.append(None)
                u = len(units)
                units.append({"op": oi, "pi": pi, "raw": rawp, "txt": txt, "spans": spans, "info": info, "arr": arr is not None})
                for ci, ch in enumerate(txt):
                    vtext.append(ch)
                    vmap.append((u, ci))
                a = info.advance(rawp)
                if a is None or op_adv is None:
                    op_adv = None
                else:
                    op_adv += a * eff / 1000.0
                op_chars += len(txt)
            if not first_piece:
                last = (pos, op_adv, eff, op_chars)
                moved = False
    if not units:
        return None
    old = "".join(vtext)
    if "�" in old:
        old_clean = old.replace("�", "\x00")
    else:
        old_clean = old
    new = engine.scrub_text(old_clean, ctx, "prose")
    if new == old_clean or len(new) != len(old_clean):
        return None
    changes = {}
    for j, (o, n) in enumerate(zip(old_clean, new)):
        if o != n and vmap[j] is not None:
            u, ci = vmap[j]
            changes.setdefault(u, {})[ci] = n
    if not changes:
        return None
    fallback_ops = set()
    for u, repl in changes.items():
        unit = units[u]
        txt = unit["txt"]
        newtxt = "".join(repl.get(i, c) for i, c in enumerate(txt))
        unit["new"] = newtxt
        enc = _encode_changes(unit["raw"], txt, newtxt, unit["spans"], unit["info"])
        if enc is None:
            fallback_ops.add(unit["op"])
        else:
            unit["enc"] = enc
    by_op = {}
    for unit in units:
        by_op.setdefault(unit["op"], []).append(unit)
    need_helv = False
    out_ops = []
    cur = None
    stack = []
    for oi, (operands, operator) in enumerate(ops):
        if operands is None:
            out_ops.append(operator)
            continue
        op = str(operator)
        if op == "q":
            stack.append(cur)
        elif op == "Q":
            cur = stack.pop() if stack else cur
        elif op == "Tf" and len(operands) >= 2:
            cur = (operands[0], operands[1])
        us = by_op.get(oi)
        if us and oi in fallback_ops and cur is not None and pdf is not None and resources is not None:
            # the embedded subset font lacks a needed glyph: draw this string with Helvetica
            if us[0]["arr"]:
                parts = []
                for item in operands[0]:
                    if isinstance(item, pikepdf.String):
                        unit = next((x for x in us if operands[0][x["pi"]] is item), None)
                        parts.append(unit.get("new", unit["txt"]) if unit else "")
                    elif not isinstance(item, (pikepdf.Array, pikepdf.Dictionary)) and num(item) <= -250:
                        parts.append(" ")
                shown_txt = "".join(parts)
            else:
                shown_txt = us[0].get("new", us[0]["txt"])
            try:
                shown = shown_txt.encode("cp1252")
            except UnicodeEncodeError:
                shown = None
            if shown is not None:
                if op in ("'", '"'):
                    out_ops.append(([], pikepdf.Operator("T*")))
                out_ops.append(([pikepdf.Name("/DeidHelv"), cur[1]], pikepdf.Operator("Tf")))
                out_ops.append(([pikepdf.String(shown)], pikepdf.Operator("Tj")))
                out_ops.append(([cur[0], cur[1]], pikepdf.Operator("Tf")))
                need_helv = True
                continue
        if us:
            for unit in us:
                if "enc" in unit:
                    if unit["arr"]:
                        operands[0][unit["pi"]] = pikepdf.String(unit["enc"])
                    else:
                        operands[unit["pi"]] = pikepdf.String(unit["enc"])
        if us and us[0]["arr"]:
            operands = [pikepdf.Array(operands[0])] + list(operands[1:])
        elif op == "TJ" and operands and isinstance(operands[0], list):
            operands = [pikepdf.Array(operands[0])] + list(operands[1:])
        out_ops.append((operands, operator))
    if need_helv:
        try:
            if "/Font" not in resources:
                resources["/Font"] = pikepdf.Dictionary()
            resources["/Font"]["/DeidHelv"] = _helv(pdf, holder if holder is not None else {})
        except Exception:
            return None
    return pikepdf.unparse_content_stream(out_ops)


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
