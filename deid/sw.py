"""SolidWorks 2015+ files (.sldprt/.sldasm/.slddrw/.slddrt/...).

These are zip-like packages with scrambled signatures: each stream has a local
record [sig(4) 14 00 06 00 08 00 time(4) crc32(4) csize(4) usize(4) nlen(2) xlen(2) name data]
where the name bytes are nibble-swapped and the data is raw deflate, plus a directory
of matching records near the end.  We decompress every stream, de-identify it with
the same-length engine, and write it back *in place*: the recompressed stream is
padded with empty deflate blocks to exactly the original compressed size, so no
offset in the file moves; only the stream bytes and the two CRC-32 fields change.
Geometry, configuration and display streams are therefore byte-identical unless they
contain an identity string.
"""
import re
import struct
import zlib

MARK = b"\x14\x00\x06\x00\x08\x00"
MARK_RE = re.compile(re.escape(MARK))
SKIP_STREAMS = ("PreviewPNG", "Preview", "Images/")
XML_HINT = (b"<?xml", b"<")


def _swap(b):
    return bytes(((x << 4) | (x >> 4)) & 0xFF for x in b)


def looks_like(data):
    return len(data) > 64 and data[4:8] == b"\x00\x00\x00\x04" and MARK in data[:4096]


def parse(data):
    """Return (locals, directory) lists of dict records."""
    n = len(data)
    locs, cdir = [], []
    for m in MARK_RE.finditer(data):
        i = m.start()
        if i + 26 > n:
            continue
        t, crc, cs, us, nl, el = struct.unpack("<IIIIHH", data[i + 6:i + 26])
        if nl == 0 or nl > 512:
            continue
        # directory record: sig(4) 00 00 | MARK ... nlen xlen clen disk iattr eattr(4) off(4) name
        if data[i - 2:i] == b"\x00\x00" and i + 40 + nl <= n:
            name = _swap(data[i + 40:i + 40 + nl])
            if all(32 <= c < 127 for c in name):
                cdir.append({"pos": i, "crc": crc, "cs": cs, "us": us, "name": name})
                continue
        d0 = i + 26 + nl + el
        if d0 + cs > n:
            continue
        name = _swap(data[i + 26:i + 26 + nl])
        if not all(32 <= c < 127 for c in name):
            continue
        comp = data[d0:d0 + cs]
        try:
            raw = zlib.decompress(comp, -15)
        except Exception:
            continue
        if len(raw) != us or (zlib.crc32(raw) & 0xFFFFFFFF) != crc:
            continue
        locs.append({"pos": i, "crc": crc, "cs": cs, "us": us, "name": name.decode("ascii"),
                     "data_off": d0, "raw": raw})
    return locs, cdir


_ENDINGS = {2: b"\x03\x00", 3: b"\x02\x0c\x00", 4: b"\x02\x08\x30\x00",
            5: b"\x01\x00\x00\xff\xff", 6: b"\x02\x04\x00\x00\xff\xff"}
_FILLER = b"\x00\x00\x00\xff\xff"


def deflate_to_size(raw, target):
    """Raw-deflate ``raw`` into exactly ``target`` bytes, or None."""
    best = None
    for level, mem, strat in ((9, 9, zlib.Z_DEFAULT_STRATEGY), (9, 8, zlib.Z_DEFAULT_STRATEGY),
                              (6, 8, zlib.Z_DEFAULT_STRATEGY), (9, 9, zlib.Z_FILTERED), (1, 8, zlib.Z_DEFAULT_STRATEGY)):
        c = zlib.compressobj(level, zlib.DEFLATED, -15, mem, strat)
        fin = c.compress(raw) + c.flush()
        if len(fin) == target:
            return fin
        c = zlib.compressobj(level, zlib.DEFLATED, -15, mem, strat)
        body = c.compress(raw) + c.flush(zlib.Z_SYNC_FLUSH)
        pad = target - len(body)
        if pad >= 2:
            k, r = divmod(pad - 2, 5)
            out = body + _FILLER * k + _ENDINGS[2 + r]
            if len(out) == target:
                return out
        if len(fin) < target and (best is None or len(fin) < len(best)):
            best = fin
    if best is not None:
        # tolerated by inflate implementations: data after the final block is ignored
        return best + b"\x00" * (target - len(best))
    return None


def process(data, engine, ctx, sub):
    """De-identify a SolidWorks package in place.  ``sub(name, raw)`` -> new raw."""
    locs, cdir = parse(data)
    if not locs:
        return None
    out = bytearray(data)
    changed = 0
    # learn identities from the document-property streams first
    order = sorted(locs, key=lambda r: (0 if r["name"].startswith(("docProps/", "swXmlContents/", "_MO_")) else 1))
    for rec in order:
        name = rec["name"]
        if rec["us"] == 0:
            continue
        new = sub(name, rec["raw"])
        if new is None or new == rec["raw"]:
            continue
        if len(new) != len(rec["raw"]):
            ctx.notes.append("sw stream length changed, kept original: %s" % name)
            continue
        comp = deflate_to_size(new, rec["cs"])
        if comp is None:
            ctx.notes.append("sw stream could not be re-packed in place: %s" % name)
            continue
        ncrc = zlib.crc32(new) & 0xFFFFFFFF
        out[rec["data_off"]:rec["data_off"] + rec["cs"]] = comp
        out[rec["pos"] + 10:rec["pos"] + 14] = struct.pack("<I", ncrc)
        swapped = _swap(name.encode("ascii"))
        for c in cdir:
            if c["crc"] == rec["crc"] and c["cs"] == rec["cs"] and c["us"] == rec["us"] and _swap(c["name"]) == swapped:
                out[c["pos"] + 10:c["pos"] + 14] = struct.pack("<I", ncrc)
        changed += 1
    return bytes(out) if changed else data
