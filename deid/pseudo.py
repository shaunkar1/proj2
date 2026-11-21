"""Deterministic, format-preserving pseudonyms.

Every identity "atom" (a run of ASCII letters/digits) maps to a pseudonym of the
same length and the same character classes (vowel->vowel, consonant->consonant,
digit->digit, case kept).  The mapping is a keyed HMAC of the case-folded atom, so
the same name gets the same pseudonym everywhere (paths, text, UTF-16 inside
binaries, archive members) without a shared lookup table, and same-length output
keeps fixed-width / length-prefixed / offset-based formats valid.
"""
import hashlib
import hmac
import re
from functools import lru_cache

VOWELS = "aeiou"
CONS = "bcdfghjklmnpqrstvwxyz"
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


@lru_cache(maxsize=200000)
def _atom(low):
    """Pseudonym (lower case) for a lower-case ASCII atom."""
    rnd = _stream(low.encode("ascii", "replace"), len(low) + 8)
    out = []
    for i, ch in enumerate(low):
        r = rnd[i]
        if ch in VOWELS:
            out.append(VOWELS[r % 5])
        elif "a" <= ch <= "z":
            out.append(CONS[r % 21])
        elif "0" <= ch <= "9":
            out.append("0123456789"[r % 10])
        else:
            out.append(ch)
    res = "".join(out)
    if res == low:  # never map a name onto itself
        for i, ch in enumerate(low):
            alt = None
            if ch in VOWELS:
                alt = VOWELS[(VOWELS.index(ch) + 1 + rnd[-1] % 4) % 5]
            elif "a" <= ch <= "z":
                alt = CONS[(CONS.index(ch) + 1 + rnd[-1] % 20) % 21]
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
