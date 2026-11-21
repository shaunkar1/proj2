"""README.txt generation (limitations / exceptions) and promotion of discovered names."""
import collections
import json
import os
import re

from .identities import strong_atom

ATOM_RE = re.compile(r"[A-Za-z0-9]+")

HOW_TO_RUN = """Machina De-ID - submission notes
================================

How to run
----------
Requirements: Python 3.9+, and the packages in code/requirements.txt
(pikepdf for PDFs, pyahocorasick for fast matching - optional, py7zr for .7z).

    python3 -m venv .venv && .venv/bin/pip install -r code/requirements.txt
    cd code
    # streaming mode (no local copy of the corpus; needs an upload token file):
    ../.venv/bin/python -m deid.cli run --attempt N --state ../state \\
        --dest gs://<bucket>/teams/<team>/attempt-N/ --token-file ../state/token.txt
    ../.venv/bin/python -m deid.cli finish --attempt N --state ../state --dest ... --token-file ...
    # or, with the input already on disk:
    ../.venv/bin/python -m deid.cli local --input ./input --output ./cleaned-corpus --state ../state

The private key, the name dictionary and the original->pseudonym table live in the
state folder and are not part of this submission.

What the pipeline does
----------------------
* Pseudonyms are deterministic and keep length and character classes (letters stay
  letters, digits stay digits, case kept), so the same original name gets the same
  pseudonym in every file, folder name, archive member and binary property, and
  fixed-width / length-prefixed / offset-based formats stay valid.
* Identities come from the corpus layout (account, organisation and site names in
  the source paths), a curated list of organisations, and identity fields found in
  each file: STEP FILE_NAME author/organisation and PERSON/ORGANIZATION/ADDRESS
  entities, user-profile and UNC paths, e-mail addresses, PDF Info/XMP authors,
  OLE summary properties (Author, Last saved by, Company, Manager, Inventor
  Designer/Engineer/Authority/...), SolidWorks docProps and history streams,
  FreeCAD/ODF/XML author and company properties, DXF $LASTSAVEDBY.
* File and folder names are rewritten with the same mapping, so references between
  assemblies, parts and drawings keep pointing at the renamed files.  Extensions and
  non-identifying path parts are unchanged.
* Containers are opened and rebuilt: zip (incl. FCStd, 3MF, F3D, ODF), gzip, tar,
  compress(.Z), 7z; SolidWorks 2015+ packages are edited stream by stream in place
  (recompressed to the original size, CRCs updated); OLE compound files are edited
  in place through the FAT; PDFs are rewritten with qpdf/pikepdf (metadata, strings,
  page text operands, attachments).
* Geometry is never edited: binary STL bodies, mesh data, B-rep streams and numeric
  sections only change where an identity string was found in a text run.
"""


def _records(adir):
    p = os.path.join(adir, "done.jsonl")
    if not os.path.exists(p):
        return
    with open(p) as f:
        for line in f:
            try:
                yield json.loads(line)
            except Exception:
                continue


def write_readme(adir, out_path):
    n = ok = changed = 0
    empty = []
    notes = collections.Counter()
    examples = collections.defaultdict(list)
    failed = []
    for rec in _records(adir):
        n += 1
        if not rec.get("ok"):
            failed.append(rec.get("path", "?"))
            continue
        ok += 1
        changed += 1 if rec.get("changed") else 0
        if rec.get("in", 0) == 0:
            empty.append(rec.get("out_path", ""))
        for note in rec.get("notes", []):
            key = re.sub(r"^[^:]{0,200}?: (?=[A-Za-z])", "", note) if ": " in note[:200] and note.count(":") > 1 else note
            key = re.sub(r"\(.*?\)", "(...)", key)[:110]
            notes[key] += 1
            if len(examples[key]) < 3:
                examples[key].append(rec.get("out_path", ""))
    lines = [HOW_TO_RUN, "", "Run summary", "-----------",
             "files processed: %d (content changed in %d; every file was renamed/re-uploaded)" % (ok, changed), ""]
    lines += ["Source defects, limitations and exceptions (final paths)", "--------------------------------------------------------"]
    if empty:
        lines.append("* %d input files are empty (0 bytes) in the source; they are passed through empty, e.g.:" % len(empty))
        lines += ["    " + e for e in empty[:5]]
    lines.append("* Git LFS pointer files (small text stubs instead of the real model) are source defects and are kept as they are.")
    lines.append("* DWG: file and folder names are de-identified; internal DWG properties are not rewritten because the sections are compressed and checksummed (no safe writer available).")
    lines.append("* RAR archives cannot be re-created with free tools; they are kept unmodified.")
    lines.append("* Text drawn with embedded subset fonts using custom glyph codes, text converted to outlines, raster logos and scanned title blocks inside PDFs/images are not rewritten in this version.")
    lines.append("* Names written in non-Latin scripts are not pseudonymised (pseudonyms must keep byte length).")
    for key, cnt in notes.most_common(40):
        lines.append("* %d files: %s" % (cnt, key))
        lines += ["    e.g. " + e for e in examples[key][:2]]
    if failed:
        lines.append("* %d files could not be transferred in this run." % len(failed))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return out_path


_PLACEHOLDER = re.compile(r"^(?:org|per|person|title|id|user|usuario|owner|author|name|company|pc|desktop|laptop|server|admin\w*|"
                          r"operator|sysadmin|gebruiker|utilisateur|benutzer|default|guest|student|temp|test)\d*$")
_PARTNO = re.compile(r"^\d{4,}[a-z]\d{2,}$|^\d+(?:mm|vac|vdc|cm|in|phi|x\d+.*)$|^[0-9a-f]{12,}$|^0x[0-9a-f]+$")


_WIDGET = re.compile(r"view|button|page\d|print|select|carousel|scroll|click|toggle|layer\d")


def promote(state):
    """Collect names seen in identity fields across finished attempts.

    Distinctive account / author / machine names become corpus-wide entries; ordinary
    first names seen as an account / author name become entries scoped to the repository
    they were seen in.  Organisation fields are not promoted (they hold vendor and CAD
    system names as often as owners); placeholders, part numbers and values that are
    themselves pseudonyms are skipped."""
    from . import pseudo
    from .identities import SOFTWARE, STOP, VENDOR_DOMAINS, is_first_name, is_word, scope_of
    kinds = ("user", "host", "email", "step-author", "pdf-author", "ole-property", "xml-field",
             "dxf-savedby", "iges-author", "png-author", "ps-for")
    person_kinds = ("user", "email", "step-author", "pdf-author", "ole-property", "iges-author", "xml-field")
    vendor_words = set()
    for d in VENDOR_DOMAINS:
        vendor_words.update(ATOM_RE.findall(d.lower()))
    strong = collections.Counter()
    scoped = collections.defaultdict(set)
    recs = []
    for name in sorted(os.listdir(state)):
        adir = os.path.join(state, name)
        if not (name.startswith("attempt-") and os.path.isdir(adir)) or "trial" in name:
            continue
        for rec in _records(adir):
            if rec.get("ok") and rec.get("findings"):
                recs.append(rec)
    for rec in recs:
        scope = scope_of(rec["path"])
        for kind, value in rec.get("findings", []):
            if kind not in kinds:
                continue
            if kind == "email" and "@" in value:
                value = value.split("@", 1)[0]          # the mailbox, not the provider
            if "~" in value:
                continue                                # 8.3 short names are handled per file
            for a in ATOM_RE.findall(value):
                low = a.lower()
                if low in STOP or low in SOFTWARE or low in vendor_words:
                    continue
                if _PLACEHOLDER.match(low) or _PARTNO.match(low) or low.isdigit():
                    continue
                if strong_atom(low) and len(low) >= 5:
                    strong[low] += 1
                elif kind in person_kinds and len(low) >= 4 and is_first_name(low):
                    scoped[scope].add(low)
    try:
        made = {pseudo.atom(a) for a in strong}
    except Exception:
        made = set()
    # a term that names files in several unrelated collections is a part / material /
    # vendor term, not somebody's account name
    spread = collections.defaultdict(set)
    mpath = os.path.join(state, "manifest.tsv")
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8", errors="replace") as f:
            for line in f:
                path = line.split("\t", 1)[0]
                sc = scope_of(path)
                for a in ATOM_RE.findall(path.lower()):
                    if a in strong and len(spread[a]) < 3:
                        spread[a].add(sc)
    keep = sorted(a for a in strong if a not in made and len(spread[a]) < 3 and not _WIDGET.search(a))
    out = {"strong": keep, "scoped": {k: sorted(v) for k, v in scoped.items()}}
    with open(os.path.join(state, "discovered.json"), "w") as f:
        json.dump(out, f)
    return len(out["strong"]) + sum(len(v) for v in out["scoped"].values())
