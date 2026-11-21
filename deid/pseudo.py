"""Deterministic, format-preserving pseudonyms.

Every identity "atom" (a run of ASCII letters/digits) maps to a pseudonym of the
same length (letter->letter of similar printed width, digit->digit, case kept).  The mapping is a keyed HMAC of the case-folded atom, so
the same name gets the same pseudonym everywhere (paths, text, UTF-16 inside
binaries, archive members) without a shared lookup table, and same-length output
keeps fixed-width / length-prefixed / offset-based formats valid.
"""
import hashlib
import hmac
import re
from functools import lru_cache

# Letters are exchanged inside groups of similar printed width, so a pseudonym set in
# the original font of a drawing occupies the same space as the name it replaces
# (text positioned glyph by glyph in PDFs stays evenly spaced and extracts cleanly).
# The groups hold for lower and upper case alike, so "Name" and "NAME" get the same
# letters; "i" has no partner of its width in both cases and is kept.
LOWER_GROUPS = ("i", "jl", "ftr", "abcekpsvxyz", "dghnoqu", "mw")
_GROUP = {}
for _g in LOWER_GROUPS:
    for _c in _g:
        _GROUP[_c] = _g
ATOM_RE = re.compile(r"[A-Za-z0-9]+")

_KEY = b"unset"


def set_key(key):
    global _KEY
    _KEY = key if isinstance(key, bytes) else key.encode("utf-8")
    _atom.cache_clear()


def _stream(seed, n):
    out = b""
    counter = 0
    while len(out) < n:
        out += hmac.new(_KEY, seed + b"|" + str(counter).encode(), hashlib.sha256).digest()
        counter += 1
    return out[:n]


@lru_cache(maxsize=400000)
def _atom(low):
    """Pseudonym (lower case) for a lower-case ASCII atom."""
    rnd = _stream(low.encode("ascii", "replace"), 2 * len(low) + 8)
    out = []
    for i, ch in enumerate(low):
        r = rnd[2 * i] * 256 + rnd[2 * i + 1]
        g = _GROUP.get(ch)
        if g:
            if len(g) > 1:
                # always a different letter of the same width group
                out.append(g[(g.index(ch) + 1 + r % (len(g) - 1)) % len(g)])
            else:
                out.append(ch)
        elif "0" <= ch <= "9":
            out.append("0123456789"[r % 10])
        else:
            out.append(ch)
    res = "".join(out)
    if res == low:  # never map a name onto itself
        for i, ch in enumerate(low):
            alt = None
            g = _GROUP.get(ch)
            if g and len(g) > 1:
                alt = g[(g.index(ch) + 1 + rnd[-1] % (len(g) - 1)) % len(g)]
            elif "0" <= ch <= "9":
                alt = "0123456789"[(int(ch) + 1 + rnd[-1] % 9) % 10]
            if alt and alt != ch:
                res = res[:i] + alt + res[i + 1:]
                break
    return res


def atom(text):
    """Pseudonymise one atom, keeping its case pattern."""
    p = _atom(text.lower())
    return "".join(pc.upper() if oc.isupper() else pc for oc, pc in zip(text, p))


def text(value, keep=None):
    """Pseudonymise every atom in ``value`` (separators untouched).

    ``keep`` is an optional predicate(atom_lower) -> True for atoms left as is.
    """
    def rep(m):
        a = m.group(0)
        if keep is not None and keep(a.lower()):
            return a
        return atom(a)
    return ATOM_RE.sub(rep, value)


_BLOCKS = ((0x4E00, 0x9FA5, 0x4E00, 0x4E00 + 6000), (0x3041, 0x3096, 0x3041, 0x3093), (0x30A1, 0x30FA, 0x30A1, 0x30F3),
           (0xAC00, 0xD7A3, 0xAC00, 0xD7A3), (0x0430, 0x044F, 0x0430, 0x044F), (0x0410, 0x042F, 0x0410, 0x042F),
           (0x03B1, 0x03C9, 0x03B1, 0x03C9), (0x0391, 0x03A9, 0x0391, 0x03A9), (0x05D0, 0x05EA, 0x05D0, 0x05EA),
           (0x0621, 0x064A, 0x0621, 0x064A), (0x0E01, 0x0E2E, 0x0E01, 0x0E2E))
FOREIGN_RE = re.compile("[\u0391-\u03a9\u03b1-\u03c9\u0410-\u044f\u05d0-\u05ea\u0621-\u064a\u0e01-\u0e2e"
                        "\u3041-\u3096\u30a1-\u30fa\u4e00-\u9fa5\uac00-\ud7a3]+")


def foreign(value):
    """Pseudonymise runs of non-Latin letters (CJK, kana, Hangul, Cyrillic, Greek, Hebrew,
    Arabic, Thai): each letter becomes another letter of the same script."""
    def rep(m):
        run = m.group(0)
        rnd = _stream(run.encode("utf-8"), 2 * len(run))
        out = []
        for i, ch in enumerate(run):
            o = ord(ch)
            r = rnd[2 * i] * 256 + rnd[2 * i + 1]
            for lo, hi, a, b in _BLOCKS:
                if lo <= o <= hi:
                    n = a + r % (b - a + 1)
                    if n == o:
                        n = a + (n - a + 1) % (b - a + 1)
                    out.append(chr(n))
                    break
            else:
                out.append(ch)
        return "".join(out)
    return FOREIGN_RE.sub(rep, value)
