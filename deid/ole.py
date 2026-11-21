"""In-place editing of OLE2 compound files (Inventor .ipt/.iam/.idw, Solid Edge
.par/.asm/.dft/.psm, pre-2015 SolidWorks, legacy Office ...).

Every stream is located through the FAT / mini-FAT, read as one logical buffer
(so strings that straddle sector boundaries are still seen), de-identified with the
same-length engine and written back to the very same sectors.  Nothing in the
container structure changes.  SummaryInformation-style property sets are parsed so
that Author / Last saved by / Company / Manager and Inventor's Designer / Engineer /
Authority / Checked by / Approved by / Vendor fields are replaced with pseudonyms even
when the value is an ordinary word.
"""
import struct

MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
ENDOFCHAIN = 0xFFFFFFFE
FREESECT = 0xFFFFFFFF

FMT_SUMMARY = bytes.fromhex("e0859ff2f94f6810ab9108002b27b3d9")
FMT_DOCSUMMARY = bytes.fromhex("02d5cdd59c2e1b10939708002b2cf9ae")
FMT_INV_DESIGN = bytes.fromhex("0f3f85324434d1119e930060b03c1ca6")
IDENTITY_PROPS = {
    FMT_SUMMARY: {4, 8},
    FMT_DOCSUMMARY: {14, 15},
    FMT_INV_DESIGN: {10, 12, 17, 30, 41, 42, 43},
}


class Ole(object):
    def __init__(self, data):
        if data[:8] != MAGIC or len(data) < 512:
            raise ValueError("not OLE")
        self.buf = bytearray(data)
        h = data[:512]
        self.sec = 1 << struct.unpack_from("<H", h, 30)[0]
        self.mini = 1 << struct.unpack_from("<H", h, 32)[0]
        if self.sec not in (512, 4096) or self.mini != 64:
            raise ValueError("unsupported OLE geometry")
        nfat = struct.unpack_from("<I", h, 44)[0]
        dir_start = struct.unpack_from("<I", h, 48)[0]
        self.cutoff = struct.unpack_from("<I", h, 56)[0]
        minifat_start, nminifat, difat_start, ndifat = struct.unpack_from("<IIII", h, 60)
        difat = list(struct.unpack_from("<109I", h, 76))
        s = difat_start
        guard = 0
        per = self.sec // 4
        while s not in (ENDOFCHAIN, FREESECT) and guard < 100000:
            block = self._sector(s)
            vals = struct.unpack("<%dI" % per, block)
            difat.extend(vals[:-1])
            s = vals[-1]
            guard += 1
        fat = []
        for s in difat[:max(nfat, 0) if nfat <= len(difat) else len(difat)]:
            if s in (FREESECT, ENDOFCHAIN):
                continue
            fat.extend(struct.unpack("<%dI" % per, self._sector(s)))
        self.fat = fat
        self.minifat = []
        for s in self._chain(minifat_start):
            self.minifat.extend(struct.unpack("<%dI" % per, self._sector(s)))
        self.entries = []
        dirbytes = b"".join(self._sector(s) for s in self._chain(dir_start))
        for i in range(0, len(dirbytes) - 127, 128):
            e = dirbytes[i:i + 128]
            nlen = struct.unpack_from("<H", e, 64)[0]
            typ = e[66]
            if typ not in (1, 2, 5) or nlen < 2 or nlen > 64:
                continue
            name = e[:nlen - 2].decode("utf-16-le", "replace")
            start = struct.unpack_from("<I", e, 116)[0]
            size = struct.unpack_from("<Q", e, 120)[0]
            if self.sec == 512:
                size &= 0xFFFFFFFF
            self.entries.append((name, typ, start, size))
        root = [e for e in self.entries if e[1] == 5]
        self.mini_extents = []
        if root:
            self.mini_chain = self._chain(root[0][2])
        else:
            self.mini_chain = []

    def _sector(self, s):
        off = (s + 1) * self.sec
        b = bytes(self.buf[off:off + self.sec])
        if len(b) < self.sec:
            b += b"\x00" * (self.sec - len(b))
        return b

    def _chain(self, start):
        out = []
        s = start
        seen = 0
        limit = len(self.fat) + 2
        while s not in (ENDOFCHAIN, FREESECT) and s < len(self.fat) and seen < limit:
            out.append(s)
            s = self.fat[s]
            seen += 1
        return out

    def extents(self, start, size):
        """File (offset, length) pieces holding a stream."""
        out = []
        remaining = size
        if size < self.cutoff:
            s = start
            seen = 0
            per = self.sec // self.mini
            while remaining > 0 and s not in (ENDOFCHAIN, FREESECT) and s < len(self.minifat) and seen <= len(self.minifat):
                idx, sub = divmod(s, per)
                if idx >= len(self.mini_chain):
                    break
                off = (self.mini_chain[idx] + 1) * self.sec + sub * self.mini
                n = min(self.mini, remaining)
                out.append((off, n))
                remaining -= n
                s = self.minifat[s]
                seen += 1
        else:
            for s in self._chain(start):
                if remaining <= 0:
                    break
                n = min(self.sec, remaining)
                out.append(((s + 1) * self.sec, n))
                remaining -= n
        if remaining:
            return None
        return out

    def streams(self):
        for name, typ, start, size in self.entries:
            if typ != 2 or size == 0 or size > len(self.buf):
                continue
            ext = self.extents(start, size)
            if ext is None or any(o + n > len(self.buf) for o, n in ext):
                continue
            yield name, ext

    def read(self, ext):
        return b"".join(bytes(self.buf[o:o + n]) for o, n in ext)

    def write(self, ext, data):
        pos = 0
        for o, n in ext:
            self.buf[o:o + n] = data[pos:pos + n]
            pos += n


def property_fields(stream):
    """Yield (start, end, wide) byte spans of identity-bearing string properties."""
    if len(stream) < 48 or stream[:2] != b"\xfe\xff":
        return
    nsets = struct.unpack_from("<I", stream, 24)[0]
    for k in range(min(nsets, 4)):
        base = 28 + k * 20
        if base + 20 > len(stream):
            break
        fmtid = stream[base:base + 16]
        off = struct.unpack_from("<I", stream, base + 16)[0]
        ids = IDENTITY_PROPS.get(fmtid)
        if ids is None or off + 8 > len(stream):
            continue
        nprops = struct.unpack_from("<I", stream, off + 4)[0]
        for j in range(min(nprops, 400)):
            p = off + 8 + j * 8
            if p + 8 > len(stream):
                break
            pid, poff = struct.unpack_from("<II", stream, p)
            if pid not in ids:
                continue
            q = off + poff
            if q + 8 > len(stream):
                continue
            vt, ln = struct.unpack_from("<II", stream, q)
            vt &= 0xFFFF
            if vt == 30 and 0 < ln <= 4096 and q + 8 + ln <= len(stream):
                yield q + 8, q + 8 + ln, False
            elif vt == 31 and 0 < ln <= 4096 and q + 8 + 2 * ln <= len(stream):
                yield q + 8, q + 8 + 2 * ln, True


def string_properties(stream):
    """Yield (start, end, wide, is_identity) for every string property of a property-set stream."""
    if len(stream) < 48 or stream[:2] != b"\xfe\xff":
        return
    nsets = struct.unpack_from("<I", stream, 24)[0]
    for k in range(min(nsets, 4)):
        base = 28 + k * 20
        if base + 20 > len(stream):
            break
        fmtid = stream[base:base + 16]
        off = struct.unpack_from("<I", stream, base + 16)[0]
        ids = IDENTITY_PROPS.get(fmtid, ())
        if off + 8 > len(stream):
            continue
        nprops = struct.unpack_from("<I", stream, off + 4)[0]
        for j in range(min(nprops, 600)):
            p = off + 8 + j * 8
            if p + 8 > len(stream):
                break
            pid, poff = struct.unpack_from("<II", stream, p)
            q = off + poff
            if pid in (0, 1) or q + 8 > len(stream):
                continue
            vt, ln = struct.unpack_from("<II", stream, q)
            vt &= 0xFFFF
            if vt == 30 and 0 < ln <= 8192 and q + 8 + ln <= len(stream):
                yield q + 8, q + 8 + ln, False, pid in ids
            elif vt == 31 and 0 < ln <= 8192 and q + 8 + 2 * ln <= len(stream):
                yield q + 8, q + 8 + 2 * ln, True, pid in ids


def process(data, engine, ctx, sub, learn=None):
    """``sub(stream_name, bytes) -> bytes`` of the same length (or None)."""
    ole = Ole(data)
    changed = False
    items = list(ole.streams())
    if learn is not None:
        # first pass: only collect identities, so a name found in one stream is
        # replaced in every stream
        for name, ext in items:
            try:
                learn(name, ole.read(ext))
            except Exception:
                pass
    for name, ext in items:
        raw = ole.read(ext)
        new = sub(name, raw)
        if new is None or new == raw or len(new) != len(raw):
            continue
        ole.write(ext, new)
        changed = True
    return bytes(ole.buf) if changed else data
