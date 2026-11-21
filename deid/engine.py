"""Matching and same-length replacement on byte buffers.

``Engine.scrub(data, ctx, mode)`` returns a buffer of identical length in which
  1. corpus-wide identities (strong atoms, phrases, repository-scoped atoms) and
  2. identities found in this buffer's own identity fields (STEP FILE_NAME author /
     organisation, PERSON / ORGANIZATION entities, user-profile paths, e-mail
     addresses, XML author/company properties, DXF $LASTSAVEDBY, IGES author ...)
are replaced by deterministic pseudonyms (see ``deid.pseudo``).

Both ASCII/UTF-8 and UTF-16LE occurrences are handled.  In binary buffers a short
ASCII hit is only accepted inside a printable run, so numeric geometry is never touched.
"""
import re

from . import pseudo
from .identities import SOFTWARE, STOP, VENDOR_DOMAINS, is_first_name, is_word, strong_atom

try:  # optional accelerator; a pure-Python trie regex is used when it is missing
    import ahocorasick
except Exception:  # pragma: no cover
    ahocorasick = None

ALNUM = frozenset(b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
BLOBCH = frozenset(b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/=")
ATOM_RE = re.compile(r"[A-Za-z0-9]+")
PHRASE_SEPS = ("", "-", "_", " ", ".")
CHUNK = 48 * 1024 * 1024
OVERLAP = 4096

USER_STOP = set("""public default shared user users admin administrator guest owner pc home desktop
student usuario utilisateur benutzer root ubuntu runner travis vsts circleci jenkins builder build
admini defaul operator sysadmin workeradm
all appdata programdata local temp tmp me username name your yourname youruser xxx www-data vagrant
docker app worker ci git node pi debian ec2-user opc azureuser lenovo dell hp asus acer msi""".split())
USER_STOP_PHRASES = {"all users", "default user", "your name", "user name"}
FIELD_STOP = {
    "", "author", "none", "unknown", "n/a", "na", "-", "--", "---", "anonymous", "user", "default",
    "unspecified", "not specified", "undefined", "null", "nil", "empty", "tbd", "xxx", "name",
    "your name", "company", "organization", "organisation", "open cascade", "$", "?", "*", "..",
    "design", "approved", "checked", "drawn", "company name", "author name", "not applicable",
    "yes", "no", "it", "a company", "an author", "my company", "my organization", "unknown authorisation",
    "unknown organization", "unknown author", "china", "1", "u", "x", "me", "pc", "user1", "sysadmin",
}
GENERIC_FIELD_WORDS = set("""organization organisation description person first last middle name id company
department title unknown unspecified default author creator owner user admin the of and for by a an my your
project division group team office it engineering design not applicable none""".split())
FILE_EXT = set("""png jpg jpeg gif svg pdf step stp stl obj iges igs sat sldprt sldasm slddrw ipt iam idw dwg dxf
fcstd prt asm drw txt xml json html htm css js py md zip gz tar rar bmp tif tiff ico webp wrl x_t x_b""".split())

WINPATH = re.compile(
    r'(?i)(?:\\\\(?P<host>[A-Za-z0-9][A-Za-z0-9._-]{1,40})|[a-z]:)?[\\/]{1,4}'
    r'(?:users|documents and settings|benutzer|utilisateurs|usuarios)[\\/]{1,4}'
    r'(?P<name>[^\\/:*?"<>|\r\n\t\x00]{1,48}?)[\\/]')
NIXPATH = re.compile(r'(?:(?<![A-Za-z0-9._~%-])|^)/(?:Users|home)/(?P<name>[^\\/:*?"<>|\r\n\t\x00 ]{1,48}?)/')
UNCHOST = re.compile(r'(?<![\\A-Za-z0-9:])\\\\(?P<host>[A-Za-z0-9][A-Za-z0-9_-]{2,40})\\[A-Za-z0-9$ _.-]{1,60}\\')
XT_FIELD = re.compile(r"(?:^|[;\n\r])\s*(?P<k>USER|SITE|MC_ID)=(?P<v>[^;\r\n\x00]{1,60})")
GIT_ACCOUNT = re.compile(r"(?i)\b(?:github|gitlab)\.com[/:](?P<u>[A-Za-z0-9][A-Za-z0-9-]{1,38})(?![A-Za-z0-9-])")
GIT_RESERVED = set("""orgs features about pricing login join settings topics explore marketplace sponsors apps
user users repos search site blog contact security enterprise collections trending issues pulls notifications
git raw gist assets images downloads releases wiki""".split())
SHORT83 = re.compile(r"^([A-Za-z0-9]{6})~\d")
HOSTNAME = re.compile(r"(?i)\b(?:hostname|host name|computer ?name|machine ?name)\s*[:=]\s*['\"]?(?P<host>[A-Za-z0-9][A-Za-z0-9._-]{2,40})")
CLOUDORG = re.compile(r'(?i)(?:OneDrive - (?P<a>[^\\/\r\n"<>|]{2,60}?)|Dropbox \((?P<b>[^)\\/\r\n]{2,60})\))(?=[\\/])')
TLDS = set("""com org net edu gov mil int io co ac de uk fr jp cn it es nl ch at se no dk fi be pl cz ru br mx
ca au nz in kr tw sg hk us eu info biz me tech dev app xyz science studio global engineering ir tr gr pt hu ro
sk si hr bg lt lv ee ie il za ar cl pe ec uy ve th my id ph vn pk bd ua by kz sa ae eg ma tn ng ke lu li is
online site store shop cloud ai design tools systems solutions email works world space today life live""".split())
EMAIL = re.compile(r'(?<![A-Za-z0-9._%+-])(?P<local>[A-Za-z0-9][A-Za-z0-9._%+-]{0,63})@'
                   r'(?P<dom>(?:[A-Za-z0-9-]+\.)+(?P<tld>[A-Za-z]{2,24}))(?![A-Za-z0-9-])')
STEP_FILE_NAME = re.compile(r'FILE_NAME\s*\(')
STEP_PERSON_ORG = re.compile(r'=\s*(PERSON|ORGANIZATION|ADDRESS|PERSONAL_ADDRESS|ORGANIZATIONAL_ADDRESS)\s*\(')
XML_FIELDS = [
    re.compile(r'(?is)<(?P<tag>dc:creator|dc:publisher|dc:contributor|dc:rights|dc:lastModifiedBy|cp:lastModifiedBy|'
               r'pdf:Author|xmp:Author|xap:Author|photoshop:AuthorsPosition|photoshop:Credit|Company|Manager|'
               r'meta:initial-creator|meta:printed-by|LastAuthor|Author|LastSavedBy|Designer|Engineer)\b[^>]*>'
               r'(?P<v>.*?)</(?P=tag)>'),
    re.compile(r'(?is)<Property\s+name="(?:CreatedBy|LastModifiedBy|Company)"[^>]*>\s*<String\s+value="(?P<v>[^"]*)"'),
    re.compile(r'(?is)<property\b[^>]*\bname="[^"]*(?:author|drawn|check|design|approv|company|engineer|creator|'
               r'owner|client|customer|vendor|supplier|manufactur|modified by|saved by)[^"]*"[^>]*>\s*'
               r'<vt:lpw?str>(?P<v>[^<]*)</vt:lpw?str>'),
    re.compile(r'(?is)<meta\s+name="author"\s+content="(?P<v>[^"]*)"'),
]
XML_TEXT = re.compile(r'>([^<>]+)<')
DXF_SAVEDBY = re.compile(r'\$LASTSAVEDBY\s*\r?\n\s*1\s*\r?\n(?P<v>[^\r\n]*)')
PS_FOR = re.compile(r'(?m)^%%For:[ \t]*(?P<v>[^\r\n]*)')
CAD_EXT = re.compile(r'(?i)\.(?:sldprt|sldasm|slddrw|ipt|iam|idw|ipn|prt|asm|drw|par|psm|dft|catpart|catproduct|'
                     r'catdrawing|step|stp|igs|iges|x_t|x_b|sat|stl|dwg|dxf|fcstd|f3d|3mf|jt|pdf|obj|wrl)(?![A-Za-z0-9])')


class Ctx(object):
    """Per-file state: scope, local identities, findings for the discovery log."""

    def __init__(self, path, scope):
        self.path = path
        self.scope = scope
        self.local_wide = set()   # lower-case atoms replaced file-wide
        self.local_exact = set()  # ordinary first names seen as account names: exact-case, file-wide
        self.local_prefix = set()  # 8.3 short names (JSMITH~1): prefix of the real account name
        self.produced = set()     # pseudonyms already written (never replaced again)
        self.findings = []        # (kind, value)
        self.notes = []
        self.hits = 0

    def found(self, kind, value):
        value = value.strip()
        if value and len(self.findings) < 200:
            self.findings.append((kind, value[:120]))


class _Entry(object):
    __slots__ = ("kind", "atoms", "bounded", "scopes", "wide")

    def __init__(self, kind, atoms, bounded, scopes, wide):
        self.kind, self.atoms, self.bounded, self.scopes, self.wide = kind, atoms, bounded, scopes, wide


def _u16(s):
    return "".join(c + "\x00" for c in s)


class Engine(object):
    def __init__(self, dictionary):
        self.D = dictionary
        pats = {}

        def add(key, entry):
            pats.setdefault(key, []).append(entry)

        for a in dictionary.strong:
            bounded = not (len(a) >= 6 or (len(a) >= 5 and any(c.isdigit() for c in a) and any(c.isalpha() for c in a)))
            add(a, _Entry("S", None, bounded, None, False))
            add(_u16(a), _Entry("S", None, bounded, None, True))
        for _, atoms in dictionary.phrases.items():
            for sep in PHRASE_SEPS:
                v = sep.join(atoms)
                add(v, _Entry("P", atoms, True, None, False))
                add(_u16(v), _Entry("P", atoms, True, None, True))
        for scope, plist in getattr(dictionary, "scoped_phrases", {}).items():
            for atoms in plist:
                atoms = list(atoms)
                for sep in PHRASE_SEPS:
                    v = sep.join(atoms)
                    add(v, _Entry("P", atoms, True, frozenset([scope]), False))
                    add(_u16(v), _Entry("P", atoms, True, frozenset([scope]), True))
        scoped = {}
        for scope, atoms in dictionary.scoped.items():
            for a in atoms:
                scoped.setdefault(a, set()).add(scope)
        for a, scopes in scoped.items():
            if a in dictionary.strong:
                continue
            add(a, _Entry("C", None, True, frozenset(scopes), False))
            add(_u16(a), _Entry("C", None, True, frozenset(scopes), True))
        self._pats = pats
        self._auto = None
        self._rx = None
        if ahocorasick is not None:
            A = ahocorasick.Automaton()
            for key, entries in pats.items():
                A.add_word(key, (len(key), entries))
            if len(pats):
                A.make_automaton()
                self._auto = A
        if self._auto is None and pats:
            self._rx = _trie_regex(pats.keys())

    # -- global candidates --------------------------------------------
    def _candidates(self, low_str):
        if self._auto is not None:
            for end, (n, entries) in self._auto.iter(low_str):
                yield end - n + 1, end + 1, entries
        elif self._rx is not None:
            pos = 0
            search = self._rx.search
            while True:
                m = search(low_str, pos)
                if not m:
                    break
                s = m.start()
                # report every dictionary prefix at this start (longest first)
                e = m.end()
                while e > s:
                    ent = self._pats.get(low_str[s:e])
                    if ent:
                        yield s, e, ent
                    e -= 1
                pos = s + 1

    def _accept(self, data, s, e, entry, ctx, binary):
        n = len(data)
        if entry.scopes is not None and ctx.scope not in entry.scopes:
            return False
        if entry.wide:
            if entry.bounded:
                if s >= 2 and data[s - 1] == 0 and data[s - 2] in ALNUM:
                    return False
                if e + 1 < n and data[e + 1] == 0 and data[e] in ALNUM:
                    return False
            return True
        if entry.bounded:
            if s > 0 and data[s - 1] in ALNUM:
                return False
            if e < n and data[e] in ALNUM:
                return False
        if _blobby(data, s, e):
            return False  # inside base64 / hex encoded data
        if binary and e - s < 10 and not _texty(data, s, e):
            return False
        return True

    def _replacement(self, orig, entry):
        if entry.wide:
            t = orig.decode("utf-16-le", "replace")
        else:
            t = orig.decode("latin-1")
        if entry.kind == "P":
            out = []
            i = 0
            for a in entry.atoms:
                while i < len(t) and not t[i].isalnum():
                    out.append(t[i])
                    i += 1
                piece = t[i:i + len(a)]
                out.append(piece if a in STOP else pseudo.atom(piece))
                i += len(a)
            out.append(t[i:])
            r = "".join(out)
        else:
            r = pseudo.atom(t)
        rb = r.encode("utf-16-le") if entry.wide else r.encode("latin-1")
        return rb if len(rb) == len(orig) else orig

    def _global_plan(self, data, ctx, binary):
        """List of non-overlapping (start, end, replacement)."""
        low = data.lower().decode("latin-1")
        cands = []
        for s, e, entries in self._candidates(low):
            for entry in entries:
                if entry.wide and not binary:
                    continue
                if self._accept(data, s, e, entry, ctx, binary):
                    cands.append((s, e, entry))
                    break
        if not cands:
            return []
        cands.sort(key=lambda c: (c[0], -(c[1] - c[0])))
        plan = []
        last = -1
        for s, e, entry in cands:
            if s < last:
                continue
            plan.append((s, e, self._replacement(data[s:e], entry)))
            last = e
        return plan

    # -- local detection ----------------------------------------------
    def detect(self, text, ctx, unit_spans, is_step=False, is_xml=False, is_dxf=False, is_ps=False):
        """Find identity fields in ``text`` (a str); append (start, end) spans to unit_spans."""
        def name_ok(name):
            low = name.strip().lower()
            if not low or low in USER_STOP or low in USER_STOP_PHRASES:
                return False
            if "~" in low and low.split("~")[0] in USER_STOP:
                return False
            atoms = [a.lower() for a in ATOM_RE.findall(name)]
            if atoms and all(a in ctx.produced or a in STOP for a in atoms):
                return False
            return any(a not in USER_STOP and a not in STOP for a in atoms)

        for m in WINPATH.finditer(text):
            name = m.group("name")
            if name_ok(name):
                unit_spans.append(m.span("name"))
                ctx.found("user", name)
                self._learn(name, ctx, person=True)
            host = m.group("host")
            if host and host.lower() not in STOP:
                unit_spans.append(m.span("host"))
                ctx.found("host", host)
                self._learn(host, ctx)
        for m in NIXPATH.finditer(text):
            name = m.group("name")
            if name_ok(name):
                unit_spans.append(m.span("name"))
                ctx.found("user", name)
                self._learn(name, ctx, person=True)
        for m in UNCHOST.finditer(text):
            host = m.group("host")
            if host.lower() not in STOP and not is_word(host.lower()):
                unit_spans.append(m.span("host"))
                ctx.found("host", host)
        for m in XT_FIELD.finditer(text):
            v = m.group("v").strip()
            if not v or v.lower() in ("huntsville, al", "unknown", "none") or m.group("k") == "SITE" and len(v) < 3:
                continue
            self._field(text, m.start("v"), m.end("v"), unit_spans, ctx,
                        "xt-user" if m.group("k") == "USER" else "host" if m.group("k") == "MC_ID" else "xt-site")
            if m.group("k") == "USER":
                self._learn(v, ctx, person=True)
        for m in GIT_ACCOUNT.finditer(text):
            u = m.group("u")
            low = u.lower()
            if low in GIT_RESERVED or low in STOP or low in SOFTWARE or low in ctx.produced:
                continue
            unit_spans.append(m.span("u"))
            ctx.found("account", u)
            self._learn(u, ctx)
        for m in HOSTNAME.finditer(text):
            host = m.group("host")
            hat = [x.lower() for x in ATOM_RE.findall(host)]
            if hat and not all(x in ctx.produced or x in STOP for x in hat) and host.lower() not in ("localhost", "unknown"):
                unit_spans.append(m.span("host"))
                ctx.found("host", host)
                self._learn(host, ctx)
        for m in CLOUDORG.finditer(text):
            g = "a" if m.group("a") else "b"
            org_atoms = [x.lower() for x in ATOM_RE.findall(m.group(g))]
            if org_atoms and all(x in ctx.produced or x in STOP for x in org_atoms):
                continue
            if m.group(g).strip().lower() not in ("personal", "business", "work", "school", "team"):
                unit_spans.append(m.span(g))
                ctx.found("org", m.group(g))
                self._learn(m.group(g), ctx)
        for m in EMAIL.finditer(text):
            dom = m.group("dom").lower()
            if m.group("tld").lower() not in TLDS or any(dom == v or dom.endswith("." + v) for v in VENDOR_DOMAINS):
                continue
            if not any(c.isalpha() for c in m.group("local")):
                continue
            mail_atoms = [x.lower() for x in ATOM_RE.findall(m.group("local") + " " + dom.rsplit(".", 1)[0])]
            if all(x in ctx.produced or x in STOP for x in mail_atoms):
                continue
            unit_spans.append(m.span("local"))
            ds, de = m.span("dom")
            unit_spans.append((ds, de - len(m.group("tld")) - 1))
            ctx.found("email", m.group(0))
            self._learn(m.group("local"), ctx, person=True)
            self._learn(dom.rsplit(".", 1)[0], ctx)
        if is_step:
            head = text[:65536]
            m = STEP_FILE_NAME.search(head)
            if m:
                args = _step_args(head, m.end())
                for idx in (2, 3, 6):
                    if idx < len(args):
                        for (a, b) in args[idx]:
                            self._field(text, a, b, unit_spans, ctx, "step-author" if idx == 2 else "step-org")
            for m in STEP_PERSON_ORG.finditer(text):
                args = _step_args(text[m.end():m.end() + 2000], 0)
                ent = m.group(1)
                # PERSON(id, last, first, middle, prefix, suffix); ORGANIZATION(id, name, description)
                pick = args[1:] if ent == "PERSON" else args[1:2] if ent == "ORGANIZATION" else args
                for arg in pick:
                    for (a, b) in arg:
                        self._field(text, m.end() + a, m.end() + b, unit_spans, ctx, "step-person-org")
        if is_xml:
            for rx in XML_FIELDS:
                for m in rx.finditer(text):
                    v = m.group("v")
                    vs = m.start("v")
                    if "<" in v:
                        for t in XML_TEXT.finditer(">" + v + "<"):
                            self._field(text, vs + t.start(1) - 1, vs + t.end(1) - 1, unit_spans, ctx, "xml-field")
                    else:
                        self._field(text, vs, m.end("v"), unit_spans, ctx, "xml-field")
        if is_dxf:
            for m in DXF_SAVEDBY.finditer(text):
                self._field(text, m.start("v"), m.end("v"), unit_spans, ctx, "dxf-savedby")
        if is_ps:
            for m in PS_FOR.finditer(text[:20000]):
                self._field(text, m.start("v"), m.end("v"), unit_spans, ctx, "ps-for")

    def _field(self, text, a, b, unit_spans, ctx, kind):
        value = text[a:b]
        low = value.strip().lower()
        if low in FIELD_STOP or not any(c.isalnum() for c in low) or _IDLIKE.match(low):
            return
        atoms = [x.lower() for x in ATOM_RE.findall(value)]
        if not any(x not in STOP for x in atoms) or any(x in SOFTWARE for x in atoms):
            return
        if all(x in GENERIC_FIELD_WORDS or x in STOP or x.isdigit() for x in atoms) or value.lstrip().lower().startswith(("http://", "https://")):
            return
        if all(x in ctx.produced or x in STOP for x in atoms):
            return  # already pseudonymised by an earlier pass over this file
        unit_spans.append((a, b))
        ctx.found(kind, value)
        self._learn(value, ctx)

    def _learn(self, value, ctx, person=False):
        m83 = SHORT83.match(value.strip())
        if m83 and m83.group(1).lower() not in USER_STOP and m83.group(1).lower() not in ctx.produced:
            ctx.local_prefix.add(m83.group(1).lower())
        for a in ATOM_RE.findall(value):
            low = a.lower()
            if low in USER_STOP or low in STOP or low in ctx.produced:
                continue
            if strong_atom(low):
                ctx.local_wide.add(low)
            elif person and len(a) >= 4 and is_first_name(low):
                ctx.local_exact.add(a)

    # -- the main entry -----------------------------------------------
    def scrub(self, data, ctx, binary=False, kind=""):
        """Return a same-length, de-identified copy of ``data`` (bytes)."""
        if not data:
            return data
        if len(data) > CHUNK:
            return self._scrub_big(data, ctx, binary, kind)
        return self._scrub_one(data, ctx, binary, kind, detect=True)

    def learn(self, data, ctx, binary=False, kind=""):
        """Detection only: collect this buffer's identities into ``ctx`` (nothing is changed)."""
        if data:
            self._scrub_one(data[:8 * 1024 * 1024], ctx, binary, kind, detect=True, dry=True)

    def _scrub_big(self, data, ctx, binary, kind):
        # identity fields live in headers: detect on the first and last few MB, then
        # replace chunk by chunk (same-length, so chunks are independent).
        head = data[:4 * 1024 * 1024]
        self._scrub_one(head, ctx, binary, kind, detect=True, dry=True)
        self._scrub_one(data[-1024 * 1024:], ctx, binary, kind, detect=True, dry=True)
        out = bytearray()
        pos = 0
        n = len(data)
        while pos < n:
            end = min(n, pos + CHUNK)
            ext_end = min(n, end + OVERLAP)
            piece = self._scrub_one(data[pos:ext_end], ctx, binary, kind, detect=(pos == 0))
            out += piece[:end - pos]
            pos = end
        return bytes(out)

    def _scrub_one(self, data, ctx, binary, kind, detect=True, dry=False):
        plan = self._global_plan(data, ctx, binary)
        spans = []       # (start, end) byte spans of identity fields (ASCII)
        wspans = []      # same for UTF-16LE (byte offsets)
        if detect:
            flags = dict(is_step=kind == "step", is_xml=kind in ("xml", "svg"), is_dxf=kind == "dxf", is_ps=kind == "ps")
            if not binary:
                self.detect(data.decode("latin-1"), ctx, spans, **flags)
            else:
                for m in _ASCII_RUN.finditer(data):
                    if not _INTERESTING.search(m.group()):
                        continue
                    tmp = []
                    self.detect(m.group().decode("latin-1"), ctx, tmp, is_xml=b"<" in m.group())
                    spans.extend((m.start() + a, m.start() + b) for a, b in tmp)
                for m in _U16_RUN.finditer(data):
                    if not _INTERESTING_W.search(m.group()):
                        continue
                    tmp = []
                    t = m.group().decode("utf-16-le", "replace")
                    self.detect(t, ctx, tmp, is_xml="<" in t)
                    wspans.extend((m.start() + 2 * a, m.start() + 2 * b) for a, b in tmp)
        if dry:
            return data
        occupied = [(s, e) for s, e, _ in plan]
        extra = []
        # identity-field spans: every atom not already handled, except generic words
        for (a, b) in spans:
            for m in _ATOM_B.finditer(data, a, b):
                self._local_atom(data, m.start(), m.end(), False, occupied, extra, in_field=True, ctx=ctx)
        for (a, b) in wspans:
            for m in _ATOM_W.finditer(data, a, b):
                self._local_atom(data, m.start(), m.end(), True, occupied, extra, in_field=True, ctx=ctx)
        # distinctive names learnt from this file's identity fields: replace file-wide
        if ctx.local_wide:
            names = sorted(ctx.local_wide, key=len, reverse=True)
            alt = "|".join(re.escape(x) for x in names)
            rx = re.compile(("(?i)(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % alt).encode("latin-1"))
            for m in rx.finditer(data):
                if binary and not _texty(data, m.start(), m.end()):
                    continue
                if _in_filename(data, m.end()):
                    continue
                self._local_atom(data, m.start(), m.end(), False, occupied, extra, in_field=False, ctx=ctx)
            if binary:
                walt = "|".join("".join(re.escape(c) + "\\x00" for c in x) for x in names)
                wrx = re.compile(("(?i)(?:%s)" % walt).encode("latin-1"))
                for m in wrx.finditer(data):
                    s, e = m.start(), m.end()
                    if s >= 2 and data[s - 1] == 0 and data[s - 2] in ALNUM:
                        continue
                    if e + 1 < len(data) and data[e + 1] == 0 and data[e] in ALNUM:
                        continue
                    self._local_atom(data, s, e, True, occupied, extra, in_field=False, ctx=ctx)
        if ctx.local_exact:
            alt = "|".join(re.escape(x) for x in sorted(ctx.local_exact, key=len, reverse=True))
            rx = re.compile(("(?<![A-Za-z0-9])(?:%s)(?![A-Za-z0-9])" % alt).encode("latin-1"))
            for m in rx.finditer(data):
                if binary and not _texty(data, m.start(), m.end(), 6):
                    continue
                if _in_filename(data, m.end()):
                    continue
                self._local_atom(data, m.start(), m.end(), False, occupied, extra, in_field=False, ctx=ctx)
            if binary:
                walt = "|".join("".join(re.escape(c) + "\\x00" for c in x) for x in sorted(ctx.local_exact, key=len, reverse=True))
                wrx = re.compile(("(?:%s)" % walt).encode("latin-1"))
                for m in wrx.finditer(data):
                    s, e = m.start(), m.end()
                    if s >= 2 and data[s - 1] == 0 and data[s - 2] in ALNUM:
                        continue
                    if e + 1 < len(data) and data[e + 1] == 0 and data[e] in ALNUM:
                        continue
                    self._local_atom(data, s, e, True, occupied, extra, in_field=False, ctx=ctx)
        if ctx.local_prefix:
            alt = "|".join(re.escape(x) for x in sorted(ctx.local_prefix))
            rx = re.compile(("(?i)(?<![A-Za-z0-9])(?:%s)[A-Za-z0-9]{1,24}(?![A-Za-z0-9~])" % alt).encode("latin-1"))
            for m in rx.finditer(data):
                if binary and not _texty(data, m.start(), m.end(), 6):
                    continue
                if _in_filename(data, m.end()):
                    continue
                self._local_atom(data, m.start(), m.end(), False, occupied, extra, in_field=False, ctx=ctx)
            if binary:
                walt = "|".join("".join(re.escape(c) + "\\x00" for c in x) for x in sorted(ctx.local_prefix))
                wrx = re.compile(("(?i)(?:%s)(?:[A-Za-z0-9]\\x00){1,24}" % walt).encode("latin-1"))
                for m in wrx.finditer(data):
                    s, e = m.start(), m.end()
                    if s >= 2 and data[s - 1] == 0 and data[s - 2] in ALNUM:
                        continue
                    if e + 1 < len(data) and data[e + 1] == 0 and (data[e] in ALNUM or data[e] == 0x7E):
                        continue
                    self._local_atom(data, s, e, True, occupied, extra, in_field=False, ctx=ctx)
        if not plan and not extra:
            return data
        out = bytearray(data)
        for s, e, rb in plan:
            out[s:e] = rb
            for a in _ATOM_B.findall(rb.replace(b"\x00", b"")):
                ctx.produced.add(a.decode("latin-1").lower())
        for s, e, rb in extra:
            out[s:e] = rb
        ctx.hits += len(plan) + len(extra)
        return bytes(out)

    def _local_atom(self, data, s, e, wide, occupied, extra, in_field, ctx):
        for (a, b) in occupied:
            if s < b and a < e:
                return
        raw = data[s:e]
        t = raw.decode("utf-16-le", "replace") if wide else raw.decode("latin-1")
        low = t.lower()
        if low in STOP or len(low) < 2 or low in ctx.produced:
            return
        if in_field and len(low) < 3 and not low.isalpha():
            return
        if in_field and low in TLDS and s > 0 and data[s - 1] == 0x2E:
            return  # keep the top-level domain of an address
        r = pseudo.atom(t)
        rb = r.encode("utf-16-le") if wide else r.encode("latin-1")
        if len(rb) != len(raw):
            return
        extra.append((s, e, rb))
        occupied.append((s, e))
        ctx.produced.add(r.lower())

    def maybe(self, data, ctx):
        """Cheap test: could ``data`` contain any identity if word boundaries are ignored?
        (Used on glyph-by-glyph PDF text, where boundaries are only known after layout.)"""
        low = data.lower()
        for name in ctx.local_wide:
            if name.encode("latin-1", "replace") in low:
                return True
        for name in ctx.local_exact:
            if name.encode("latin-1", "replace") in data:
                return True
        for s, e, entries in self._candidates(low.decode("latin-1")):
            for entry in entries:
                if not entry.wide and (entry.scopes is None or ctx.scope in entry.scopes):
                    return True
        return False

    # -- strings (paths, names, decoded metadata) ---------------------
    def rename(self, value, ctx):
        """Apply corpus-wide identities only (file and folder names, archive members)."""
        raw = value.encode("utf-8")
        plan = self._global_plan(raw, ctx, False)
        if not plan:
            return value
        out = bytearray(raw)
        for s, e, rb in plan:
            out[s:e] = rb
        return out.decode("utf-8", "replace")

    def scrub_text(self, value, ctx, kind=""):
        """De-identify a str.  Non-latin-1 characters are preserved untouched."""
        if not value:
            return value
        try:
            raw = value.encode("latin-1")
            return self.scrub(raw, ctx, False, kind).decode("latin-1")
        except UnicodeEncodeError:
            pass
        # mixed scripts: work on the UTF-8 bytes (ASCII atoms keep their byte length)
        raw = value.encode("utf-8")
        return self.scrub(raw, ctx, False, kind).decode("utf-8", "replace")

    def scrub_field(self, value, ctx, kind="field"):
        """De-identify a str that *is* an identity field (author, company ...)."""
        if not value or value.strip().lower() in FIELD_STOP:
            return value
        atoms0 = [a.lower() for a in ATOM_RE.findall(value)]
        if any(a in SOFTWARE for a in atoms0) or all(a in ctx.produced or a in STOP for a in atoms0):
            return value
        ctx.found(kind, value)
        self._learn(value, ctx)
        v = self.scrub_text(value, ctx)

        def keep(a):
            return a in STOP
        # atoms the global pass did not touch are still part of an identity field
        out = []
        last = 0
        for m in ATOM_RE.finditer(value):
            out.append(v[last:m.start()])
            seg_new = v[m.start():m.end()]
            if seg_new == m.group(0) and not keep(m.group(0).lower()) and len(m.group(0)) >= 2:
                seg_new = pseudo.atom(m.group(0))
                ctx.hits += 1
            ctx.produced.add(seg_new.lower())
            out.append(seg_new)
            last = m.end()
        out.append(v[last:])
        return "".join(out)


_IDLIKE = re.compile(r"^(?:id|no|nr|ref|#)?[\s_#.-]*\d{1,6}$")
_ASCII_RUN = re.compile(rb"[\x20-\x7e]{6,}")
_INTERESTING = re.compile(rb"[\\/@<]|[Hh]ost|[Nn]ame\s*[:=]|USER=|SITE=|MC_ID=")
_INTERESTING_W = re.compile(rb"[\\/@<]\x00|[Hh]\x00o\x00s\x00t\x00")
_U16_RUN = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
_ATOM_B = re.compile(rb"[A-Za-z0-9]+")
_ATOM_W = re.compile(rb"(?:[A-Za-z0-9]\x00)+")


def _texty(data, s, e, need=12):
    """True if the hit sits in a printable-ASCII run of at least ``need`` bytes, or in a
    string that carries its own 32-bit length prefix (serialized property values)."""
    if e - s >= need:
        return True
    a = s
    while a > 0 and s - a < need and 32 <= data[a - 1] < 127:
        a -= 1
    b = e
    n = len(data)
    while b < n and b - e < need and 32 <= data[b] < 127:
        b += 1
    if b - a >= need:
        return True
    # exact extent of the printable run
    while a > 0 and 32 <= data[a - 1] < 127:
        a -= 1
    while b < n and 32 <= data[b] < 127:
        b += 1
    if a >= 4 and b - a >= 3 and (b == n or data[b] == 0):
        ln = data[a - 4] | (data[a - 3] << 8) | (data[a - 2] << 16) | (data[a - 1] << 24)
        if ln in (b - a, b - a + 1):
            return True
    return False


_HEXCH = frozenset(b"0123456789abcdefABCDEF\r\n")
_B64CH = frozenset(b"0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ+/=\r\n")


def _blobby(data, s, e):
    """True if the hit lies inside encoded data (base64 or hex), judged on +-40 bytes."""
    a = max(0, s - 40)
    b = min(len(data), e + 40)
    if b - a < 60:
        return False
    win = data[a:b]
    hexlike = True
    for c in win:
        if c not in _B64CH:
            return False
        if hexlike and c not in _HEXCH:
            hexlike = False
    if hexlike:
        return True
    # base64 mixes case and digits constantly; names and paths do not
    trans = 0
    prev = 0
    for c in win:
        cls = 1 if 97 <= c <= 122 else 2 if 65 <= c <= 90 else 3 if 48 <= c <= 57 else 4
        if prev and cls != prev:
            trans += 1
        prev = cls
    return trans * 3 >= len(win)


def _in_filename(data, end):
    """True if the bytes after ``end`` run straight into a CAD file extension
    (the hit is inside a file name that other files reference; only corpus-wide
    identities, which also rename the file itself, may change it)."""
    tail = data[end:end + 90]
    for i, c in enumerate(tail):
        if c in b'\\/:*?"<>|\r\n\x00\'':
            return False
        if c == 0x2E:
            if CAD_EXT.match(tail[i:i + 14].decode("latin-1")):
                return True
    return False


def _step_args(text, pos):
    """Parse the top-level arguments of a STEP record starting just after '('.

    Returns a list (one item per argument) of lists of (start, end) spans of the
    string-literal contents inside that argument."""
    args = [[]]
    depth = 0
    i = pos
    n = len(text)
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            args[-1].append((i + 1, j))
            i = j + 1
            continue
        if c == "/" and text[i:i + 2] == "/*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            args.append([])
        i += 1
    return args


def _trie_regex(keys):
    """Compile a trie-shaped alternation matching any key (longest-first)."""
    trie = {}
    for k in keys:
        node = trie
        for ch in k:
            node = node.setdefault(ch, {})
        node[""] = True

    def emit(node):
        end = "" in node
        alts = []
        for ch in sorted(k for k in node if k != ""):
            alts.append(re.escape(ch) + emit(node[ch]))
        if not alts:
            return ""
        body = alts[0] if len(alts) == 1 and not end else "(?:" + "|".join(alts) + ")"
        return body + ("?" if end else "")
    return re.compile(emit(trie), re.S)
