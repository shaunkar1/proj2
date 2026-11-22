"""Second look at a processed PDF through a text extractor (switch DEID_PDF_SWEEP=1).

The main pass rewrites the page content operators.  A reader of the finished document sees
the text through an extractor, and a name can survive there: an occurrence the operator pass
could not decode, a name recognised by its context in one place and repeated bare in
another, a name known from a sibling document of the same collection.  One surviving
occurrence is enough to give the identity away.

This pass extracts the text of the finished document (PyMuPDF), finds

  * names the dictionary / this document's own identity fields would still replace,
  * words and numbers that the main pass replaced somewhere in this document but not
    everywhere,
  * words and numbers replaced in another PDF of the same collection (file given by
    DEID_SWEEP_SCOPE, private, produced by a dry run),

removes those glyphs from the page and writes the usual pseudonym in their place.

Needs PyMuPDF; without it the document is returned as the main pass left it.
"""
import collections
import json
import os
import re

from . import pseudo

_STATE = {}
ATOM = re.compile(r"[A-Za-z0-9]+")
CODE = re.compile(r"[A-Za-z0-9]+(?:[-_.][A-Za-z0-9]+)+")
MAX_PAGES = 300

# shop-floor abbreviations a detector may once mistake for a name: never spread
_ABBR = set("""shcs bhcs fhcs shss fhss thru csk cbore cskd assy qty typ ref dwg dia rad max min nom tol tbd
pcs pos item nema awg npt bsp unc unf rohs pcb smd led usb vcc gnd pwm gpio uart spi i2c""".split())


def available():
    if "ok" not in _STATE:
        try:
            import fitz  # noqa: F401
            _STATE["ok"] = True
        except Exception:
            _STATE["ok"] = False
    return _STATE["ok"]


def _scope_sets():
    if "scope" not in _STATE:
        data = {}
        path = os.environ.get("DEID_SWEEP_SCOPE", "")
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        _STATE["scope"] = data
    return _STATE["scope"]


def _generic(low):
    from .identities import is_word, is_foreign_word
    from .engine import STOP
    return low in STOP or low in _ABBR or is_word(low) or is_foreign_word(low)


def atom_ok(low):
    """May this replaced word be replaced in every place it occurs?"""
    if low.isdigit():
        return len(low) >= 6
    if low.isalpha():
        return len(low) >= 4 and not _generic(low)
    from .engine import _partno_tok
    return len(low) >= 5 and _partno_tok(low)


def code_ok(low):
    return len(low) >= 6 and sum(c.isdigit() for c in low) >= 2 and len(low) <= 60


def code_new(engine, code):
    from .engine import _partno_tok
    return ATOM.sub(lambda m: engine.token(m.group(0)) if _partno_tok(m.group(0)) else m.group(0), code)


def _page_texts(doc):
    out = []
    for i, page in enumerate(doc):
        if i >= MAX_PAGES:
            break
        try:
            out.append(page.get_text("text"))
        except Exception:
            out.append("")
    return out


def replaced_sets(engine, texts0, texts1):
    """(words, numbers) the main pass replaced at least once: fewer occurrences than in the
    original and the pseudonym newly present."""
    a0 = collections.Counter()
    a1 = collections.Counter()
    c0 = collections.Counter()
    c1 = collections.Counter()
    for t in texts0:
        low = t.lower()
        a0.update(ATOM.findall(low))
        c0.update(CODE.findall(low))
    for t in texts1:
        low = t.lower()
        a1.update(ATOM.findall(low))
        c1.update(CODE.findall(low))
    atoms = {}
    for a, n0 in a0.items():
        n1 = a1.get(a, 0)
        if n1 >= n0 or len(a) < 3:
            continue
        p = pseudo.atom(a)
        if p == a or a1.get(p, 0) <= a0.get(p, 0):
            continue
        atoms[a] = (n0, n1)
    codes = {}
    for c, n0 in c0.items():
        n1 = c1.get(c, 0)
        if n1 >= n0:
            continue
        p = code_new(engine, c).lower()
        if p == c or c1.get(p, 0) <= c0.get(p, 0):
            continue
        codes[c] = (n0, n1)
    return atoms, codes


def own_targets(atoms, codes):
    """Words / numbers replaced in part of this document that are replaced in all of it."""
    ta = set()
    for a, (n0, n1) in atoms.items():
        if n1 == 0:
            continue
        if atom_ok(a):
            ta.add(a)
        elif a.isalpha() and len(a) >= 4 and n1 <= n0 - n1 and a not in _ABBR:
            ta.add(a)          # an ordinary word used as a name here: most places already replaced
    tc = set(c for c, (n0, n1) in codes.items() if n1 and code_ok(c))
    return ta, tc


def scope_entry(atoms, codes):
    """What a dry run records for the collection file (see DEID_SWEEP_SCOPE)."""
    return sorted(a for a in atoms if atom_ok(a)), sorted(c for c in codes if code_ok(c))


def _spans(engine, ctx, text, ta, tc):
    """[(start, end, replacement)] on one page's text."""
    out = []
    taken = []

    def free(s, e):
        return all(e <= a or s >= b for a, b in taken)

    # numbers first (longest unit), then the dictionary, then single words
    if tc:
        for m in CODE.finditer(text):
            low = m.group(0).lower()
            if low in tc:
                new = code_new(engine, m.group(0))
                if new != m.group(0) and len(new) == len(m.group(0)):
                    out.append((m.start(), m.end(), new))
                    taken.append((m.start(), m.end()))
    try:
        raw = "".join(c if ord(c) < 256 else "\x00" for c in text).encode("latin-1")
        new = engine._scrub_one(raw, ctx, False, "", detect=False).decode("latin-1")
    except Exception:
        new = None
    if new is not None and len(new) == len(text):
        for m in ATOM.finditer(text):
            s, e = m.span()
            if new[s:e] != text[s:e] and "\x00" not in new[s:e] and free(s, e):
                out.append((s, e, new[s:e]))
                taken.append((s, e))
    if ta:
        for m in ATOM.finditer(text):
            s, e = m.span()
            w = m.group(0)
            low = w.lower()
            if low in ctx.produced or not free(s, e):
                continue
            if low in ta:
                out.append((s, e, engine.token(w)))
                taken.append((s, e))
                continue
            if len(low) >= 8:
                # "Name21", "2019Name": the name with a short number attached
                core = low.strip("0123456789")
                if core != low and len(core) >= 6 and len(low) - len(core) <= 4 and core in ta and core.isalpha():
                    a = s + low.index(core)
                    out.append((a, a + len(core), engine.token(text[a:a + len(core)])))
                    taken.append((a, a + len(core)))
    return out


def _chars(page, fitz):
    """Page text as the extractor reads it, with one box per character."""
    text = []
    info = []
    d = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    for b in d.get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            direction = ln.get("dir", (1.0, 0.0))
            for sp in ln.get("spans", []):
                size = sp.get("size", 8.0)
                color = sp.get("color", 0)
                for ch in sp.get("chars", []):
                    c = ch.get("c", "")
                    if len(c) != 1:
                        c = (c or " ")[0]
                    text.append(c)
                    info.append((ch["bbox"], ch["origin"], size, color, direction))
            text.append("\n")
            info.append(None)
    return "".join(text), info


def _rotation(direction):
    cx, sy = direction
    if abs(cx) >= abs(sy):
        return 0 if cx >= 0 else 180
    return 90 if sy < 0 else 270


def sweep(orig, out, engine, ctx):
    """Return ``out`` with the surviving occurrences replaced (or ``out`` itself)."""
    try:
        import fitz
    except Exception:
        return out
    d0 = d1 = None
    try:
        try:
            d1 = fitz.open(stream=out, filetype="pdf")
            if d1.needs_pass or d1.page_count == 0:
                return out
            texts1 = _page_texts(d1)
            if out is orig or out == orig:
                texts0 = texts1
            else:
                d0 = fitz.open(stream=orig, filetype="pdf")
                texts0 = _page_texts(d0) if not d0.needs_pass else texts1
        except Exception:
            return out
        atoms, codes = replaced_sets(engine, texts0, texts1) if texts0 is not texts1 else ({}, {})
        ctx.swept = scope_entry(atoms, codes)
        ta, tc = own_targets(atoms, codes)
        sets = _scope_sets()
        for sc in (getattr(ctx, "scopes", None) or (ctx.scope,)):
            entry = sets.get(str(sc))
            if entry:
                ta.update(a for a in entry[0] if a not in ctx.produced)
                tc.update(entry[1])
        changed = 0
        left = 0
        for pno, quick in enumerate(texts1):
            if not quick.strip():
                continue
            try:
                if not _spans(engine, ctx, quick, ta, tc):
                    continue
                page = d1[pno]
                if page.rotation:
                    page.remove_rotation()
                text, info = _chars(page, fitz)
                todo = []
                for s, e, rep in _spans(engine, ctx, text, ta, tc):
                    boxes = [info[i] for i in range(s, e)]
                    if any(b is None for b in boxes) or not boxes:
                        continue
                    rect = fitz.Rect(boxes[0][0])
                    for b in boxes[1:]:
                        rect |= fitz.Rect(b[0])
                    if rect.is_empty or rect.width <= 0 or rect.height <= 0:
                        continue
                    todo.append((rect, rep, boxes[0]))
                if not todo:
                    continue
                for rect, rep, first in todo:
                    # the middle of the glyph boxes: neighbours on the same line stay
                    dx = min(0.6, rect.width * 0.08)
                    dy = rect.height * 0.25
                    page.add_redact_annot(fitz.Rect(rect.x0 + dx, rect.y0 + dy, rect.x1 - dx, rect.y1 - dy))
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                      text=fitz.PDF_REDACT_TEXT_REMOVE)
                for rect, rep, first in todo:
                    bbox, origin, size, color, direction = first
                    rot = _rotation(direction)
                    long = rect.width if rot in (0, 180) else rect.height
                    try:
                        safe = rep.encode("latin-1", "replace").decode("latin-1")
                        width = fitz.get_text_length(safe, fontname="helv", fontsize=size)
                        fs = size if width <= long * 1.05 or width <= 0 else max(size * 0.5, size * long / width)
                        rgb = ((color >> 16 & 255) / 255.0, (color >> 8 & 255) / 255.0, (color & 255) / 255.0)
                        page.insert_text(fitz.Point(origin), safe, fontsize=fs, fontname="helv", color=rgb,
                                         rotate=rot, overlay=True)
                        changed += 1
                    except Exception:
                        left += 1
            except Exception:
                continue
        if not changed:
            return out
        try:
            new = d1.tobytes(garbage=3, deflate=True)
        except Exception:
            return out
        ctx.hits += changed
        ctx.notes.append("%d surviving occurrences replaced in the finished document" % changed)
        return new
    finally:
        for d in (d0, d1):
            try:
                if d is not None:
                    d.close()
            except Exception:
                pass
