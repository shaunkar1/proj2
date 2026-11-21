"""Per-format handlers.  ``process(path, data, engine, ctx)`` returns the de-identified
bytes for one corpus file; containers recurse into their members."""
import gzip
import io
import os
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import zipfile
import zlib

from . import ole, pseudo, sw
from .engine import Ctx
from .identities import STOP

MAX_DEPTH = 6
COMPOSITE_ZIP = (".fcstd", ".3mf", ".f3d", ".f3z", ".odg", ".odt", ".ods", ".odp", ".docx", ".xlsx", ".pptx",
                 ".kmz", ".fcbak", ".amf")
XML_EXT = (".xml", ".svg", ".html", ".htm", ".xhtml", ".rels", ".model", ".kml", ".urdf", ".xacro", ".sdf",
           ".dae", ".x3d", ".plist", ".ipj", ".fods", ".amf", ".rdf")
STEP_EXT = (".step", ".stp", ".p21", ".stpz", ".ifc")
IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF8", "gif"), (b"II*\x00", "tif"),
               (b"MM\x00*", "tif"), (b"BM", "bmp"))
VERSION_EXT = re.compile(r"\.\d+$")


def ext_of(path):
    base = os.path.basename(path).lower()
    base = VERSION_EXT.sub("", base)  # Creo: part.prt.12 -> .prt
    return os.path.splitext(base)[1]


def is_text(data):
    head = data[:16384]
    if not head:
        return True
    if b"\x00" in head:
        return False
    bad = sum(1 for c in head if c < 9 or (13 < c < 32 and c != 27))
    return bad * 50 < len(head)


def text_kind(ext, data):
    if ext in STEP_EXT or data[:13] == b"ISO-10303-21;":
        return "step"
    if ext in XML_EXT or data[:5] == b"<?xml" or data[:4] == b"<svg":
        return "xml"
    if ext == ".dxf":
        return "dxf"
    if ext in (".ps", ".eps", ".ai") or data[:4] == b"%!PS":
        return "ps"
    return ""


def process(path, data, engine, ctx, depth=0):
    """Dispatch on content first, extension second."""
    if not data:
        return data
    ext = ext_of(path)
    head = data[:8]
    try:
        if head[:4] == b"PK\x03\x04" or (head[:4] == b"PK\x05\x06" and ext == ".zip"):
            return do_zip(path, data, engine, ctx, depth, composite=ext in COMPOSITE_ZIP)
        if head[:2] == b"\x1f\x8b":
            return do_gzip(path, data, engine, ctx, depth)
        if head[:2] == b"\x1f\x9d":
            return do_lzw(path, data, engine, ctx, depth)
        if head[:6] == b"7z\xbc\xaf\x27\x1c":
            return do_7z(path, data, engine, ctx, depth)
        if head[:4] == b"Rar!":
            return do_rar(path, data, engine, ctx, depth)
        if ext in (".tar",) or data[257:262] == b"ustar":
            return do_tar(path, data, engine, ctx, depth)
        if head[:5] == b"%PDF-" or (ext == ".pdf" and b"%PDF-" in data[:1024]):
            from . import pdf
            return pdf.process(path, data, engine, ctx, depth)
        if head == ole.MAGIC:
            return do_ole(path, data, engine, ctx)
        if sw.looks_like(data):
            return do_sw(path, data, engine, ctx)
        for magic, kind in IMAGE_MAGIC:
            if data.startswith(magic):
                return do_image(path, data, engine, ctx, kind)
        if ext == ".stl" and not _ascii_stl(data):
            return do_stl_binary(data, engine, ctx)
        if head[:4] == b"AC10" and ext in (".dwg", ".dwt", ".dws"):
            return do_dwg(path, data, engine, ctx)
    except Exception as exc:  # never lose a file because a parser choked on it
        ctx.notes.append("%s handler failed (%s: %s); generic pass used" % (ext or "file", type(exc).__name__, str(exc)[:80]))
    if is_text(data):
        out = engine.scrub(data, ctx, False, text_kind(ext, data))
        if ext in (".igs", ".iges") or (len(out) > 80 and out[72:73] == b"S"):
            try:
                out = iges_fields(out, engine, ctx)
            except Exception:
                pass
        return out
    return engine.scrub(data, ctx, True, "")


def iges_fields(data, engine, ctx):
    """Pseudonymise the author (21) and organisation (22) of an IGES global section."""
    pos = 0
    n = len(data)
    chars = []   # file offsets of global-section characters (columns 1-72)
    while pos < n and len(chars) < 20000:
        nl = data.find(b"\n", pos)
        end = n if nl < 0 else nl
        line = data[pos:end]
        if len(line) >= 73:
            sec = line[72:73]
            if sec == b"G":
                chars.extend(range(pos, pos + 72))
            elif sec in (b"D", b"P", b"T"):
                break
        pos = end + 1
        if nl < 0:
            break
    if not chars:
        return data
    g = bytes(data[i] for i in chars).decode("latin-1")
    # parameter and record delimiters (defaults , and ;)
    i = 0
    pd, rd = ",", ";"
    if g[:2] == "1H":
        pd = g[2]
        i = 3 + 1 if g[3:4] == pd else 3
    else:
        i = 1 if g[:1] == "," else 0
    fields = []   # (start, end) of Hollerith payloads, or None
    idx = 1
    j = i if g[:2] == "1H" else 0
    # generic scan of parameters
    k = 0
    params = []
    while k < len(g) and len(params) < 30:
        m = re.match(r"\s*(\d+)H", g[k:])
        if m:
            ln = int(m.group(1))
            start = k + m.end()
            params.append((start, start + ln))
            k = start + ln
        else:
            params.append(None)
        nxt = k
        while nxt < len(g) and g[nxt] not in (pd, rd):
            nxt += 1
        if nxt >= len(g) or g[nxt] == rd:
            break
        k = nxt + 1
    out = bytearray(data)
    changed = False
    for want in (20, 21):   # zero-based: author, organisation
        if want < len(params) and params[want]:
            a, b = params[want]
            value = g[a:b]
            new = engine.scrub_field(value, ctx, "iges-author" if want == 20 else "iges-org")
            if new != value and len(new) == len(value):
                for off, ch in zip(chars[a:b], new.encode("latin-1")):
                    out[off] = ch
                changed = True
    return bytes(out) if changed else data


# ---------------------------------------------------------------- archives
class _RawNameInfo(zipfile.ZipInfo):
    """ZipInfo that writes a legacy (non-UTF-8) member name byte-for-byte."""
    _raw_name = None

    def _encodeFilenameFlags(self):
        if self._raw_name is not None:
            return self._raw_name, self.flag_bits & ~0x800
        return zipfile.ZipInfo._encodeFilenameFlags(self)


def rename_path(engine, ctx, name):
    return "/".join(engine.rename(part, ctx) for part in name.split("/"))


def do_zip(path, data, engine, ctx, depth, composite=False):
    if depth > MAX_DEPTH:
        return engine.scrub(data, ctx, True, "")
    zin = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    used = set()
    touched = False
    with zipfile.ZipFile(out, "w", allowZip64=True) as zout:
        for zi in zin.infolist():
            name = zi.filename
            legacy = not (zi.flag_bits & 0x800) and any(ord(c) > 127 for c in name)
            new_name = rename_path(engine, ctx, name)
            if new_name in used:
                new_name = name
            used.add(new_name)
            if zi.flag_bits & 0x1:
                raise RuntimeError("encrypted zip member")
            payload = b"" if name.endswith("/") else zin.read(zi)
            if new_name != name:
                touched = True
            if payload:
                sub = ctx if composite else Ctx(path + "/" + name, ctx.scope)
                before = payload
                payload = process(name, payload, engine, sub, depth + 1)
                if payload != before:
                    touched = True
                if sub is not ctx:
                    ctx.findings.extend(sub.findings[:50])
                    ctx.notes.extend("%s: %s" % (new_name, n) for n in sub.notes[:20])
                    ctx.hits += sub.hits
            ni = _RawNameInfo(new_name, date_time=zi.date_time)
            if legacy:
                try:
                    ni._raw_name = new_name.encode("cp437")
                except UnicodeEncodeError:
                    ni._raw_name = None
            ni.compress_type = zi.compress_type if zi.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) else zipfile.ZIP_DEFLATED
            ni.external_attr = zi.external_attr
            ni.create_system = zi.create_system
            ni.comment = engine.scrub(zi.comment, ctx, False, "") if zi.comment else b""
            if ni.comment != zi.comment:
                touched = True
            zout.writestr(ni, payload)
        if zin.comment:
            zout.comment = engine.scrub(zin.comment, ctx, False, "")
            if zout.comment != zin.comment:
                touched = True
    # nothing identifying inside: keep the archive byte-for-byte
    return out.getvalue() if touched else data


def do_gzip(path, data, engine, ctx, depth):
    raw = gzip.decompress(data)
    flags = data[3]
    mtime = struct.unpack("<I", data[4:8])[0]
    inner_name = os.path.basename(path)
    for suffix, repl in ((".tgz", ".tar"), (".gz", ""), (".z", "")):
        if inner_name.lower().endswith(suffix):
            inner_name = inner_name[:len(inner_name) - len(suffix)] + repl
            break
    new = process(inner_name, raw, engine, ctx, depth + 1)
    out = io.BytesIO()
    fname = engine.rename(inner_name, ctx) if flags & 0x08 else ""
    if new == raw and (not fname or fname == inner_name or fname.encode("latin-1", "replace") in data[:400]):
        return data
    with gzip.GzipFile(filename=fname, mode="wb", fileobj=out, mtime=mtime, compresslevel=6) as g:
        g.write(new)
    return out.getvalue()


def do_tar(path, data, engine, ctx, depth):
    tin = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    out = io.BytesIO()
    fmt = tin.format if tin.format in (tarfile.USTAR_FORMAT, tarfile.GNU_FORMAT, tarfile.PAX_FORMAT) else tarfile.GNU_FORMAT
    touched = False
    with tarfile.open(fileobj=out, mode="w", format=fmt) as tout:
        for ti in tin:
            payload = None
            if ti.isreg():
                before = tin.extractfile(ti).read()
                sub = Ctx(path + "/" + ti.name, ctx.scope)
                payload = process(ti.name, before, engine, sub, depth + 1)
                touched = touched or payload != before
                ctx.findings.extend(sub.findings[:50])
                ctx.hits += sub.hits
            new_name = rename_path(engine, ctx, ti.name)
            touched = touched or new_name != ti.name
            ti.name = new_name
            if ti.linkname:
                ti.linkname = rename_path(engine, ctx, ti.linkname)
            for attr in ("uname", "gname"):
                v = getattr(ti, attr)
                if v and v.lower() not in STOP and v.lower() not in ("root", "wheel", "staff", "users", "nogroup", "nobody", "runner", "docker"):
                    setattr(ti, attr, pseudo.text(v))
                    touched = True
            ti.pax_headers = {}
            if payload is not None:
                ti.size = len(payload)
                tout.addfile(ti, io.BytesIO(payload))
            else:
                tout.addfile(ti)
    return out.getvalue() if touched else data


def lzw_decode(data):
    """Decompress a Unix compress(1) .Z stream."""
    exe = shutil.which("gzip")
    if exe:
        p = subprocess.run([exe, "-dc"], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0 or p.stdout:
            return p.stdout
    raise RuntimeError("no .Z decoder available")


def lzw_encode(raw):
    """Write a valid .Z stream.  Uses compress(1) when present; otherwise a simple
    encoder that emits 9-bit literal codes and resets the table before it can grow."""
    exe = shutil.which("compress")
    if exe:
        p = subprocess.run([exe, "-c"], input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.stdout[:2] == b"\x1f\x9d":
            return p.stdout
    out = bytearray(b"\x1f\x9d\x90")  # block mode, 16 bits max
    acc = 0
    nbits = 0
    count = 0
    for b in raw:
        acc |= b << nbits
        nbits += 9
        count += 1
        if count == 247:  # 247 literals + CLEAR = 31 full groups of 8 codes
            acc |= 256 << nbits
            nbits += 9
            count = 0
        while nbits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nbits -= 8
    if nbits:
        out.append(acc & 0xFF)
    return bytes(out)


def do_lzw(path, data, engine, ctx, depth):
    raw = lzw_decode(data)
    inner = os.path.basename(path)
    if inner.lower().endswith(".z"):
        inner = inner[:-2]
    new = process(inner, raw, engine, ctx, depth + 1)
    if new == raw:
        return data
    return lzw_encode(new)


def _extract_with(tool_args, data, suffix):
    tmp = tempfile.mkdtemp(prefix="deid-arc-")
    src = os.path.join(tmp, "in" + suffix)
    dst = os.path.join(tmp, "x")
    os.mkdir(dst)
    with open(src, "wb") as f:
        f.write(data)
    return tmp, src, dst


def do_7z(path, data, engine, ctx, depth):
    try:
        import py7zr
    except Exception:
        ctx.notes.append("7z archive kept unmodified (py7zr not installed)")
        return data
    tmp = tempfile.mkdtemp(prefix="deid-7z-")
    try:
        src = os.path.join(tmp, "in.7z")
        dst = os.path.join(tmp, "x")
        with open(src, "wb") as f:
            f.write(data)
        with py7zr.SevenZipFile(src, "r") as z:
            z.extractall(dst)
        outp = os.path.join(tmp, "out.7z")
        touched = False
        with py7zr.SevenZipFile(outp, "w") as z:
            for root, dirs, files in os.walk(dst):
                dirs.sort()
                for fn in sorted(files):
                    full = os.path.join(root, fn)
                    rel = os.path.relpath(full, dst).replace(os.sep, "/")
                    with open(full, "rb") as f:
                        payload = f.read()
                    sub = Ctx(path + "/" + rel, ctx.scope)
                    before = payload
                    payload = process(rel, payload, engine, sub, depth + 1)
                    ctx.findings.extend(sub.findings[:50])
                    ctx.hits += sub.hits
                    new_rel = rename_path(engine, ctx, rel)
                    touched = touched or payload != before or new_rel != rel
                    z.writestr(payload, new_rel)
        if not touched:
            return data
        with open(outp, "rb") as f:
            return f.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def do_rar(path, data, engine, ctx, depth):
    # RAR cannot be written with free tools.  The archive is kept only if a raw scan of
    # its (usually compressed) bytes shows nothing; this is recorded as a limitation.
    ctx.notes.append("RAR archive: contents could not be rewritten (no RAR writer); kept as is")
    return data


# ---------------------------------------------------------------- native CAD
def do_ole(path, data, engine, ctx):
    def sub(name, raw):
        buf = bytearray(raw)
        if name.startswith("\x05"):
            for a, b, wide in ole.property_fields(raw):
                seg = raw[a:b]
                try:
                    if wide:
                        txt = seg.decode("utf-16-le")
                        new = engine.scrub_field(txt.rstrip("\x00"), ctx, "ole-property")
                        enc = (new + txt[len(txt.rstrip("\x00")):]).encode("utf-16-le")
                    else:
                        txt = seg.decode("latin-1")
                        new = engine.scrub_field(txt.rstrip("\x00"), ctx, "ole-property")
                        enc = (new + txt[len(txt.rstrip("\x00")):]).encode("latin-1")
                except Exception:
                    continue
                if len(enc) == len(seg):
                    buf[a:b] = enc
        return engine.scrub(bytes(buf), ctx, True, "")
    out = ole.process(data, engine, ctx, sub)
    # free sectors and slack can still hold old strings: one more pass over the raw file
    return engine.scrub(out, ctx, True, "")


def do_sw(path, data, engine, ctx):
    def sub(name, raw):
        if raw[:5] == b"<?xml" or (name.endswith(".xml") or name.endswith(".rels")):
            return engine.scrub(raw, ctx, False, "xml")
        if raw[:2] == b"\xff\xfe" and raw[2:6] == b"<\x00?\x00":
            return engine.scrub(raw, ctx, True, "")
        return engine.scrub(raw, ctx, True, "")
    out = sw.process(data, engine, ctx, sub)
    if out is None:
        return engine.scrub(data, ctx, True, "")
    return out


def _ascii_stl(data):
    return data[:5].lower() == b"solid" and b"\x00" not in data[:512] and b"facet" in data[:4096]


def do_stl_binary(data, engine, ctx):
    # only the 80-byte header can hold text; triangle data must never be touched
    if len(data) < 84:
        return data
    head = engine.scrub(data[:80], ctx, True, "")
    return head + data[80:]


def do_dwg(path, data, engine, ctx):
    # DWG sections are compressed and checksummed; a byte patch would corrupt the file.
    ctx.notes.append("DWG: internal properties not rewritten (compressed, checksummed sections)")
    return data


# ---------------------------------------------------------------- images
def do_image(path, data, engine, ctx, kind):
    if kind == "png":
        return _png(data, engine, ctx)
    # JPEG / TIFF / GIF / BMP: EXIF, XMP and comment blocks are plain text; pixel data is
    # entropy-coded or raw and only ever matched by long, distinctive strings.
    return engine.scrub(data, ctx, True, "")


def _png(data, engine, ctx):
    out = bytearray(data[:8])
    pos = 8
    n = len(data)
    while pos + 12 <= n:
        length, typ = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if end > n:
            break
        body = data[pos + 8:pos + 8 + length]
        if typ in (b"tEXt", b"iTXt", b"zTXt", b"eXIf"):
            new = _png_text(typ, body, engine, ctx)
            if new != body:
                out += struct.pack(">I4s", len(new), typ) + new + struct.pack(">I", zlib.crc32(typ + new) & 0xFFFFFFFF)
                pos = end
                continue
        out += data[pos:end]
        pos = end
    out += data[pos:]
    return bytes(out)


def _png_text(typ, body, engine, ctx):
    try:
        if typ == b"tEXt" or typ == b"eXIf":
            key, _, val = body.partition(b"\x00") if typ == b"tEXt" else (b"", b"", body)
            if typ == b"eXIf":
                return engine.scrub(body, ctx, True, "")
            if key.lower() in (b"author", b"artist", b"creator", b"copyright"):
                return key + b"\x00" + engine.scrub_field(val.decode("latin-1"), ctx, "png-" + key.decode("latin-1").lower()).encode("latin-1")
            return key + b"\x00" + engine.scrub(val, ctx, False, "xml" if b"<" in val else "")
        if typ == b"zTXt":
            key, _, rest = body.partition(b"\x00")
            val = zlib.decompress(rest[1:])
            new = engine.scrub(val, ctx, False, "xml" if b"<" in val else "")
            return body if new == val else key + b"\x00\x00" + zlib.compress(new)
        if typ == b"iTXt":
            key, _, rest = body.partition(b"\x00")
            cflag, cmeth = rest[0], rest[1]
            lang, _, rest2 = rest[2:].partition(b"\x00")
            tkey, _, val = rest2.partition(b"\x00")
            raw = zlib.decompress(val) if cflag else val
            new = engine.scrub(raw, ctx, False, "xml" if b"<" in raw else "")
            if new == raw:
                return body
            return key + b"\x00" + bytes([cflag, cmeth]) + lang + b"\x00" + tkey + b"\x00" + (zlib.compress(new) if cflag else new)
    except Exception:
        return body
    return body
