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


def promote(state):
    """Collect distinctive names seen in identity fields across finished attempts."""
    seen = collections.Counter()
    for name in sorted(os.listdir(state)):
        adir = os.path.join(state, name)
        if not (name.startswith("attempt-") and os.path.isdir(adir)):
            continue
        for rec in _records(adir):
            for kind, value in rec.get("findings", []):
                if kind in ("user", "host", "email", "step-author", "step-org", "pdf-author", "ole-property",
                            "xml-field", "org", "step-person-org", "dxf-savedby"):
                    for a in ATOM_RE.findall(value):
                        low = a.lower()
                        if strong_atom(low) and len(low) >= 5:
                            seen[low] += 1
    names = sorted(seen)
    with open(os.path.join(state, "discovered.json"), "w") as f:
        json.dump(names, f)
    return len(names)
