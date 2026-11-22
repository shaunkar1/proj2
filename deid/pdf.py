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
import os
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
    if os.environ.get("DEID_PDF_RAW") == "1":
        return data             # experiment only: documents passed through untouched
    keep = ctx.numbers_ok
    try:
        out = _process_file(path, data, engine, ctx, depth)
        if os.environ.get("DEID_PDF_OCR") == "1":
            from . import ocr
            if ocr.available():
                ctx.numbers_ok = True
                out = ocr.redact(out, engine, ctx)
        return out
    finally:
        ctx.numbers_ok = keep


_PDF_NAME = re.compile(rb"(?<![A-Za-z0-9/:.\\~_\-])/[A-Za-z][A-Za-z0-9_.#+\-]{1,60}(?=[\s/<>\[\]()]|$)")


def _raw_pass(data, engine, ctx):
    """Plain same-length pass over the file bytes.  PDF syntax names (/CIDSystemInfo,
    /FontDescriptor ...) are put back: a name hit inside one would break the file."""
    new = engine.scrub(data, ctx, True, "")
    if new == data:
        return new
    out = None
    for m in _PDF_NAME.finditer(data):
        a, b = m.span()
        if new[a:b] != data[a:b]:
            if out is None:
                out = bytearray(new)
            out[a:b] = data[a:b]
    return new if out is None else bytes(out)


_OWN_GENERIC = set("""drawing drawings draw drw dwg sheet sheets page pages rev revision version ver final copy
new old draft assembly assy asm part parts model models view views detail details section sections plan plans layout
print export output scan scanned document doc docs file files pdf step stp iges template title block bom list table
left right front back rear side upper lower inner outer main base with without from for and the with inch metric
mm cm test sample example manual guide instructions datasheet spec specification specifications schematic schematics
zeichnung blatt ansicht teil baugruppe plano hoja dibujo conjunto pieza dessin feuille ensemble piece vue""".split())


def _own_title_words(path, ctx):
    """Experiment: the words of a document's own file name (its part / drawing title) are
    treated as identifying and replaced wherever the document repeats them."""
    from .identities import is_word
    stem = path.rsplit("/", 1)[-1]
    stem = stem[:stem.rfind(".")] if "." in stem else stem
    for tok in re.findall("[A-Za-z0-9]+", stem):
        low = tok.lower()
        if low in _OWN_GENERIC or low in ctx.produced:
            continue
        if low.isalpha():
            if len(low) >= 4 or (len(low) == 3 and tok.isupper() and not is_word(low)):
                ctx.local_wide.add(low)     # a word of the title, or initials ("..._EJG.pdf")
        elif low.isdigit():
            if len(low) >= 5:
                ctx.local_wide.add(low)
        elif len(low) >= 4:
            ctx.local_wide.add(low)


def _strip_text(pdf):
    done = False
    streams = []
    for page in pdf.pages:
        streams.append(page)
    for obj in pdf.objects:
        try:
            if isinstance(obj, pikepdf.Stream) and (str(obj.get("/Subtype", "")) == "/Form" or "/PatternType" in obj):
                streams.append(obj)
        except Exception:
            pass
    for st in streams:
        try:
            ops = pikepdf.parse_content_stream(st)
            keep = [(a, o) for a, o in ops if str(o) not in TEXT_OPS]
            if len(keep) != len(ops):
                new = pikepdf.unparse_content_stream(keep)
                if isinstance(st, pikepdf.Page):
                    st.obj["/Contents"] = pdf.make_stream(new)
                else:
                    st.write(new)
                done = True
        except Exception:
            pass
    return done


_BT_ET = re.compile(rb"(?s)(?<![A-Za-z0-9_/])BT(?![A-Za-z0-9_]).*?(?<![A-Za-z0-9_/])ET(?![A-Za-z0-9_])")


def _strip_text_fast(pdf):
    """Experiment only: cut every text object (BT ... ET) out of the page and form streams
    without parsing them."""
    done = False
    for obj in pdf.objects:
        try:
            if not isinstance(obj, pikepdf.Stream):
                continue
            typ = str(obj.get("/Type", ""))
            sub = str(obj.get("/Subtype", ""))
            if sub in ("/Image", "/XML") or typ in ("/Metadata", "/XRef", "/ObjStm", "/EmbeddedFile") or "/Length1" in obj or "/FunctionType" in obj:
                continue
            data = obj.read_bytes()
            if b"BT" not in data:
                continue
            new = _BT_ET.sub(b" ", data)
            if new != data:
                obj.write(new)
                done = True
        except Exception:
            pass
    return done


_TOOL_WORDS = frozenset("""solidworks sldworks autodesk autocad inventor fusion revit creo parametric catia dassault
systemes siemens solidedge unigraphics onshape freecad librecad draftsight bricscad rhino rhinoceros sketchup
adobe acrobat distiller indesign illustrator photoshop pdfmaker microsoft word excel powerpoint office visio
publisher ghostscript pdfcreator pscript quartz pdfcontext skia cairo inkscape latex pdftex luatex xetex texlive
bluebeam foxit nitro pdfsam itext itextsharp reportlab wkhtmltopdf chromium chrome canon epson xerox ricoh
kyocera lexmark brother konica minolta toshiba fujitsu scansnap navigator bullzip cutepdf primopdf dopdf
libreoffice openoffice writer draw impress corel coreldraw kicad eagle altium orcad eplan zuken ansys
matlab mathworks wolfram mathematica keynote pages preview apple macos windows mac""".split())


def _tool_words(pdf, ctx):
    """Names of the programs that produced the document (taken from its own Creator /
    Producer properties): replaced where the page text repeats them."""
    from .identities import is_word
    try:
        info = pdf.trailer.get("/Info")
        if info is None:
            return
        for k in ("/Creator", "/Producer"):
            v = info.get(k)
            if not isinstance(v, pikepdf.String):
                continue
            for tok in re.findall(r"[A-Za-z][A-Za-z0-9]+", str(v)):
                low = tok.lower()
                if low in _TOOL_WORDS or (len(low) >= 6 and low.isalpha() and not is_word(low)):
                    if len(low) >= 4:
                        ctx.local_wide.add(low)
    except Exception:
        pass


def _all_properties(pdf, engine, keep=()):
    """Every document property becomes a pseudonym (word by word); the XMP copies are dropped.
    Returns True when something changed."""
    done = False
    try:
        if "/Metadata" in pdf.Root:
            del pdf.Root["/Metadata"]
            done = True
    except Exception:
        pass
    try:
        info = pdf.trailer.get("/Info")
        if info is not None:
            for k in list(info.keys()):
                v = info[k]
                if isinstance(v, pikepdf.String) and not str(k).endswith("Date") and str(k) not in keep:
                    t = re.sub(r"[A-Za-z0-9]+", lambda m: engine.token(m.group(0)), str(v))
                    if t != str(v):
                        info[k] = pikepdf.String(t)
                        done = True
    except Exception:
        pass
    for obj in pdf.objects:
        try:
            if isinstance(obj, pikepdf.Stream) and (str(obj.get("/Subtype", "")) == "/XML" or str(obj.get("/Type", "")) == "/Metadata"):
                obj.write(b"")
                done = True
        except Exception:
            pass
    return done


def _blank_all_images(pdf):
    """Experiment only: every picture becomes an empty one of the same size."""
    done = False
    for obj in pdf.objects:
        try:
            if isinstance(obj, pikepdf.Stream) and str(obj.get("/Subtype", "")) == "/Image":
                if _blank_image(obj, 255):
                    done = True
        except Exception:
            pass
    return done


def _nuke(pdf, engine):
    _all_properties(pdf, engine)
    _blank_all_images(pdf)


def _process_file(path, data, engine, ctx, depth):
    if pikepdf is None:
        ctx.notes.append("pikepdf missing: PDF handled by raw pass only")
        return _raw_pass(data, engine, ctx)
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except Exception as exc:
        ctx.notes.append("PDF could not be parsed (%s); raw pass only" % type(exc).__name__)
        return _raw_pass(data, engine, ctx)
    try:
        if os.environ.get("DEID_PDF_OWN") == "1":
            _own_title_words(path, ctx)
        if os.environ.get("DEID_PDF_TOOLS") == "1":
            _tool_words(pdf, ctx)
        ctx.numbers_ok = True         # drawing text and document properties
        try:
            changed = _process(pdf, path, engine, ctx, depth)
        finally:
            ctx.numbers_ok = False
        if os.environ.get("DEID_PDF_NOTEXT") == "1":
            # experiment only: drop every text-showing operator (measures how much of a
            # document's identity sits in its page text)
            if _strip_text(pdf):
                changed = True
        if os.environ.get("DEID_PDF_META") == "all":
            # every document property (title, subject, keywords, author, producing program)
            # is treated as an identity field
            if _all_properties(pdf, engine):
                changed = True
        if os.environ.get("DEID_PDF_IMAGES") == "blank":
            if _blank_all_images(pdf):
                changed = True
        if os.environ.get("DEID_PDF_NUKE") == "1":
            # experiment only: nothing readable is left in the document - no page text, no
            # pictures, every document property a pseudonym (measures what the remaining
            # findings depend on)
            _strip_text_fast(pdf)
            _all_properties(pdf, engine)
            if os.environ.get("DEID_PDF_NUKE_IMAGES") == "1":
                _nuke(pdf, engine)
            changed = True
        if not changed:
            # nothing in the live document; still clean plain-text leftovers (old
            # incremental revisions, unreferenced objects) without moving any offset
            return _raw_pass(data, engine, ctx)
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


_TITLE_SEQ = re.compile(r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*")


def _title_numbers(raw, engine, ctx):
    """A document title usually repeats the drawing's file name: a drawing / part number in
    it ("S1186-B2P-A Print Head Cable", "LCSAP-304") is replaced wherever the document
    text repeats it (the title itself is rewritten by the caller).  Returns the new title
    text or None."""
    from .engine import _partno_ok, _partno_tok
    from .identities import is_word
    wide = raw[:2] == b"\xfe\xff"
    try:
        text = raw[2:].decode("utf-16-be") if wide else raw.decode("latin-1")
    except Exception:
        return None
    if len(text) > 200:
        return None
    out = []
    pos = 0
    for m in _TITLE_SEQ.finditer(text):
        v = m.group(0)
        parts = re.split("[-_./]", v)
        ok = _partno_ok(v)
        if not ok and len(parts) >= 2 and sum(c.isdigit() for c in v) >= 3:
            # "LCSAP-304", "MET-PMP-402": an upper-case letter code with a number
            ok = any(len(x) >= 2 and x.isalpha() and x.isupper() and not is_word(x.lower()) for x in parts)
        if not ok:
            continue
        ctx.local_seqs.append(v)
        ctx.found("pdf-number", v)
        new = re.sub("[A-Za-z0-9]+", lambda t: engine.token(t.group(0)) if _partno_tok(t.group(0)) else t.group(0), v)
        out.append(text[pos:m.start()] + new)
        pos = m.end()
    if not out:
        return None
    new_text = "".join(out) + text[pos:]
    if len(new_text) != len(text) or new_text == text:
        return None
    try:
        return pikepdf.String(b"\xfe\xff" + new_text.encode("utf-16-be")) if wide else pikepdf.String(new_text.encode("latin-1"))
    except Exception:
        return None


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
                if k == "/Title":
                    nt = _title_numbers(bytes(v), engine, ctx)
                    if nt is not None:
                        obj[k] = v = nt
                        changed = True
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
            elif subtype in ("/Type1", "/TrueType") and not info.embedded and not isinstance(enc, pikepdf.Dictionary):
                base = str(font.get("/BaseFont", "")).lower()
                if "courier" in base:
                    info.adv, info.adv_default = {}, 600.0
                elif ("helvetica" in base or "arial" in base) and "bold" not in base and "narrow" not in base:
                    info.adv = dict(HELV)
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


_HELV_W = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
           556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
           1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
           667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
           333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
           556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
HELV = {32 + i: float(w) for i, w in enumerate(_HELV_W)}     # Helvetica advances, 1/1000 em


def _helv_width(ch):
    return HELV.get(ord(ch), 556.0)


def _scaled_runs(old_raw, new_raw, info):
    """Split ``new_raw`` into (bytes, factor) stretches: every run of replaced glyphs gets the
    horizontal scaling that makes it exactly as wide as the glyphs it stands for, so each
    replaced word covers the same area as before (text extraction groups glyphs by the gaps
    between them) and nothing after it moves.  None when no scaling is needed or the widths
    are unknown."""
    if info.adv is None or len(old_raw) != len(new_raw):
        return None
    w = info.width
    n = len(new_raw) // w
    out = []
    any_scaled = False
    k = 0
    start = 0
    while k < n:
        if old_raw[k * w:k * w + w] == new_raw[k * w:k * w + w]:
            k += 1
            continue
        j = k
        so = sn = 0.0
        ok = True
        while j < n and old_raw[j * w:j * w + w] != new_raw[j * w:j * w + w]:
            ao = info.adv.get(int.from_bytes(old_raw[j * w:j * w + w], "big"), info.adv_default)
            an = info.adv.get(int.from_bytes(new_raw[j * w:j * w + w], "big"), info.adv_default)
            if not ao or not an or ao <= 0 or an <= 0:
                ok = False
            else:
                so += ao
                sn += an
            j += 1
        r = round(min(2.5, max(0.4, so / sn)), 4) if ok and sn > 0 else 1.0
        if abs(r - 1.0) >= 0.004:
            if k > start:
                out.append((new_raw[start * w:k * w], 1.0))
            out.append((new_raw[k * w:j * w], r))
            start = j
            any_scaled = True
        k = j
    if not any_scaled:
        return None
    if start < n:
        out.append((new_raw[start * w:], 1.0))
    return out


def _helv_op(parts, size, tz0):
    """Operators that show a text operator in Helvetica.  ``parts`` holds numbers (kerning of
    the TJ array) and (text, raw, spans, info) tuples.  Every word is drawn with the horizontal
    scaling that makes it exactly as wide as the glyphs it stands for, so nothing after it
    moves and no gap opens inside it.  None when a character has no cp1252 code."""
    seq = []                        # ("g", chars, original advance or None) / ("k", number)
    for part in parts:
        if not isinstance(part, tuple):
            try:
                seq.append(("k", float(part)))
            except Exception:
                pass
            continue
        text, raw, spans, info = part
        try:
            text.encode("cp1252")
        except UnicodeEncodeError:
            return None
        w = info.width
        if spans is None:
            spans = [(i, i + 1) for i in range(len(text))]
        for idx, (a, b) in enumerate(spans):
            grp = text[a:b]
            if not grp:
                continue
            ao = None
            if info.adv is not None:
                ao = info.adv.get(int.from_bytes(raw[idx * w:idx * w + w], "big"), info.adv_default)
                if ao is not None and ao <= 0:
                    ao = None
            seq.append(("g", grp, ao))
    if not any(x[0] == "g" for x in seq):
        return None
    out = []
    Tz, TJ = pikepdf.Operator("Tz"), pikepdf.Operator("TJ")
    i = 0
    n = len(seq)
    lead = []                       # separators drawn at the original scaling
    while i < n:
        x = seq[i]
        word = x[0] == "g" and not x[1].isspace()
        if not word:
            if x[0] == "k":
                lead.append(round(x[1], 2))
            else:
                lead.append(x[1].encode("cp1252"))
                if x[2] is not None:
                    d = sum(_helv_width(c) for c in x[1]) - x[2]
                    if abs(d) >= 0.5:
                        lead.append(round(d, 2))
            i += 1
            continue
        j = i
        slot = 0.0
        nat = 0.0
        chars = []
        while j < n:
            y = seq[j]
            if y[0] == "k":
                if abs(y[1]) >= 120:
                    break
                slot -= y[1]
            elif y[1].isspace():
                break
            else:
                hw = sum(_helv_width(c) for c in y[1])
                nat += hw
                slot += y[2] if y[2] is not None else hw
                chars.append(y[1])
            j += 1
        if lead:
            out.append(([tz0], Tz))
            out.append(([_tj_array(lead)], TJ))
            lead = []
        r = slot / nat if nat > 0 and slot > 0 else 1.0
        rc = min(2.5, max(0.4, r))
        items = ["".join(chars).encode("cp1252")]
        rest = (nat * rc - slot) / rc
        if abs(rest) >= 0.5:
            items.append(round(rest, 2))
        out.append(([round(tz0 * rc, 3)], Tz))
        out.append(([_tj_array(items)], TJ))
        i = j
    if lead:
        out.append(([tz0], Tz))
        out.append(([_tj_array(lead)], TJ))
    out.append(([tz0], Tz))
    return out


def _tj_array(items):
    return pikepdf.Array([pikepdf.String(x) if isinstance(x, (bytes, bytearray)) else x for x in items])


def _helv(pdf, holder):
    if holder.get("font") is None:
        holder["font"] = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name("/Font"), Subtype=pikepdf.Name("/Type1"),
            BaseFont=pikepdf.Name("/Helvetica"), Encoding=pikepdf.Name("/WinAnsiEncoding")))
    return holder["font"]


_KEEP_ADV = os.environ.get("DEID_PDF_KEEPADV", "1") != "0"
_NEEDED_OPS = frozenset(("q", "Q", "cm", "BT", "Tf", "Tm", "Td", "TD", "T*", "Tj", "TJ", "'", '"', "Tz"))


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
            probes.append("\n".join(dec).encode("latin-1", "replace"))
            probes.append("".join(dec).encode("latin-1", "replace"))
        except Exception:
            pass
    glyphwise = len(pieces) > 40 and sum(len(pc) for pc in pieces) < 3 * len(pieces)
    # raw operand bytes are readable text only when no font maps codes through a table
    kinds = ["" if infos else "prose"] * 2 + ["prose"] * (len(probes) - 2)
    if (all(engine.scrub(pr, ctx, False, kd) == pr for pr, kd in zip(probes, kinds))
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
    tz = 100.0
    stack = []
    for oi, (operands, operator) in enumerate(ops):
        if operands is None:
            out_ops.append(operator)
            continue
        op = str(operator)
        if op == "q":
            stack.append((cur, tz))
        elif op == "Q":
            cur, tz = stack.pop() if stack else (cur, tz)
        elif op == "Tf" and len(operands) >= 2:
            cur = (operands[0], operands[1])
        elif op == "Tz" and operands:
            tz = num(operands[0]) or 100.0
        us = by_op.get(oi)
        if us and oi in fallback_ops and cur is not None and pdf is not None and resources is not None:
            # the embedded subset font lacks a needed glyph: draw this string with Helvetica
            parts = []
            bypi = {x["pi"]: x for x in us}
            if us[0]["arr"]:
                for pi, item in enumerate(operands[0]):
                    if isinstance(item, pikepdf.String):
                        unit = bypi.get(pi)
                        if unit is not None:
                            parts.append((unit.get("new", unit["txt"]), unit["raw"], unit["spans"], unit["info"]))
                    elif not isinstance(item, (pikepdf.Array, pikepdf.Dictionary)):
                        parts.append(num(item))
            else:
                parts.append((us[0].get("new", us[0]["txt"]), us[0]["raw"], us[0]["spans"], us[0]["info"]))
            shown = _helv_op(parts, num(cur[1]), tz)
            if shown:
                if op == '"' and len(operands) >= 3:
                    out_ops.append(([operands[0]], pikepdf.Operator("Tw")))
                    out_ops.append(([operands[1]], pikepdf.Operator("Tc")))
                if op in ("'", '"'):
                    out_ops.append(([], pikepdf.Operator("T*")))
                out_ops.append(([pikepdf.Name("/DeidHelv"), cur[1]], pikepdf.Operator("Tf")))
                out_ops.extend(shown)
                out_ops.append(([cur[0], cur[1]], pikepdf.Operator("Tf")))
                need_helv = True
                continue
        if us:
            scaled = False
            for unit in us:
                if "enc" in unit:
                    if unit["arr"]:
                        operands[0][unit["pi"]] = pikepdf.String(unit["enc"])
                    else:
                        operands[unit["pi"]] = pikepdf.String(unit["enc"])
                    unit["runs"] = _scaled_runs(unit["raw"], unit["enc"], unit["info"]) if _KEEP_ADV else None
                    if unit["runs"]:
                        scaled = True
            if scaled:
                flat = []
                bypi = {x["pi"]: x for x in us}
                if us[0]["arr"]:
                    for pi, item in enumerate(operands[0]):
                        unit = bypi.get(pi)
                        if unit is not None and unit.get("runs"):
                            flat.extend(unit["runs"])
                        else:
                            flat.append((item, 1.0))
                else:
                    flat = list(us[0]["runs"])
                if op == '"' and len(operands) >= 3:
                    out_ops.append(([operands[0]], pikepdf.Operator("Tw")))
                    out_ops.append(([operands[1]], pikepdf.Operator("Tc")))
                if op in ("'", '"'):
                    out_ops.append(([], pikepdf.Operator("T*")))
                now = 1.0
                buf = []
                for item, r in flat:
                    if r != now:
                        if buf:
                            out_ops.append(([_tj_array(buf)], pikepdf.Operator("TJ")))
                            buf = []
                        out_ops.append(([round(tz * r, 3)], pikepdf.Operator("Tz")))
                        now = r
                    buf.append(item)
                if buf:
                    out_ops.append(([_tj_array(buf)], pikepdf.Operator("TJ")))
                if now != 1.0:
                    out_ops.append(([tz], pikepdf.Operator("Tz")))
                continue
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


def image_key(obj):
    """Identity of an embedded picture: digest of its stored bytes."""
    import hashlib
    return hashlib.md5(obj.read_raw_bytes()).hexdigest()[:12]


def _blank_image(obj, value):
    """Fill a picture with one grey level (255 = white / opaque, 0 = black / transparent)."""
    import zlib
    w = int(obj.get("/Width", 0))
    h = int(obj.get("/Height", 0))
    if w <= 0 or h <= 0 or w * h > 40000000:
        return False
    for k in ("/Decode", "/DecodeParms", "/Mask", "/Intent", "/Interpolate", "/Alternates", "/Metadata", "/OC", "/Matte"):
        if k in obj:
            del obj[k]
    if bool(obj.get("/ImageMask", False)):
        # a stencil: all bits set = nothing painted
        obj.write(zlib.compress(b"\xff" * (((w + 7) // 8) * h)), filter=pikepdf.Name("/FlateDecode"))
        obj["/BitsPerComponent"] = 1
    else:
        obj.write(zlib.compress(bytes([value]) * (w * h)), filter=pikepdf.Name("/FlateDecode"))
        obj["/ColorSpace"] = pikepdf.Name("/DeviceGray")
        obj["/BitsPerComponent"] = 8
    return True


def _blank_logo(obj, logos, ctx):
    """Replace a known logo / stamp picture by an empty one of the same size: the picture
    becomes white and, when it has a soft mask, fully transparent."""
    try:
        if image_key(obj) not in logos:
            return False
        w = int(obj.get("/Width", 0))
        h = int(obj.get("/Height", 0))
        sm = obj.get("/SMask")
        if not _blank_image(obj, 255):
            return False
        if isinstance(sm, pikepdf.Stream):
            try:
                _blank_image(sm, 0)
            except Exception:
                pass
        ctx.hits += 1
        ctx.found("pdf-logo", "%dx%d" % (w, h))
        return True
    except Exception:
        return False


def _process(pdf, path, engine, ctx, depth):
    from . import handlers
    changed = False
    done = set()
    logos = getattr(engine.D, "logos", None)
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
                elif sub == "/Image":
                    if logos and _blank_logo(obj, logos, ctx):
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
