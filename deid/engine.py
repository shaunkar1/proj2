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
import os
import re

from . import pseudo
from .identities import CODE_GENERIC, SOFTWARE, STOP, VENDOR_DOMAINS, is_first_name, is_foreign_word, is_surname, is_word, strong_atom

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
US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
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


# ---- visible prose (PDF page text, drawing title blocks) -------------------------
_UP = "A-Z\xc0-\xd6\xd8-\xde"
_LO = "a-z\xdf-\xf6\xf8-\xff"
_CAPW = "[%s][%s]+(?:[-'][%s][%s]+)?" % (_UP, _LO, _UP, _LO)
_NAMEW = "(?:%s|[%s]{2,}(?:-[%s]{2,})?)" % (_CAPW, _UP, _UP)
PROSE_PERSON = re.compile(
    "(?<![A-Za-z\xc0-\xff])(?P<first>%s)(?P<mid>(?:[ \t]+[%s]\\.?){0,2})[ \t]+(?P<last>%s(?:[ \t]+%s)?)(?![A-Za-z\xc0-\xff])"
    % (_NAMEW, _UP, _NAMEW, _NAMEW))
PROSE_INITIAL = re.compile(
    "(?<![A-Za-z0-9\xc0-\xff.])(?P<ini>(?:[%s]\\.[ \t]?){1,2})[ \t]*(?P<last>%s)(?![A-Za-z0-9\xc0-\xff])" % (_UP, _CAPW))
ORG_SUFFIX = (r"(?:Inc|Incorporated|LLC|L\.L\.C|Ltd|Limited|GmbH|AG|KG|Co|Corp|Corporation|Company|S\.A|SA|S\.L|SL|S\.r\.l|Srl|SRL|"
              r"B\.V|BV|N\.V|NV|Pty|PLC|plc|A/S|AB|Oy|Kft|s\.r\.o|Sdn Bhd|Pvt|LLP|e\.V|"
              r"Technologies|Technology|Engineering|Industries|Industrial|Systems|Solutions|Manufacturing|Instruments|"
              r"Electronics|Robotics|Labs|Lab|Laboratory|Laboratories|Institute|Foundation|Associates|Group|Works|Machining|"
              r"Enterprises|Designs|Studio|Studios|Aerospace|Automation|Dynamics|Motors|Tools|Tooling|Precision)")
PROSE_DOTTED = re.compile(
    "(?<![A-Za-z0-9\xc0-\xff.])(?P<ini>[%s]\\.)(?P<last>[%s][%s]{3,}|[%s]{4,})(?![A-Za-z0-9\xc0-\xff])" % (_UP, _UP, _LO, _UP))
PROSE_ORG = re.compile(
    "(?<![A-Za-z\xc0-\xff])(?P<name>(?:%s|[%s][%s0-9]+)(?:[ \t]+(?:%s|[%s][%s0-9]+|&|and|of|de|di|for)){0,3})"
    "[ \t]*,?[ \t]+(?P<suf>%s)(?:\\.|(?![A-Za-z0-9\xc0-\xff]))" % (_CAPW, _UP, _UP, _CAPW, _UP, _UP, ORG_SUFFIX))
PROSE_ORG_PREFIX = re.compile(
    "(?<![A-Za-z0-9\xc0-\xff])(?P<pre>(?:Technical |Technische |State |National |Royal )?(?:University|Universit\xe4t|Universit\xe9|Universidad|Universit\xe0|Universidade|"
    "Universiteit|Institute|Institut|Instituto|Istituto|College|School|Academy|Politecnico|Polit\xe9cnica|Hochschule|Fachhochschule|"
    "Fundaci\xf3|Fundaci\xf3n|Fondazione|Laboratory|Department|Dept\\.?|Faculty|Centre|Center)(?: of| de| di| del| della| f\xfcr| for| d')?(?: the| la| les)?)"
    "(?:[ \t]+|[ \t]*\n[ \t]*)(?P<name>(?:%s|[%s]{2,})(?:[ \t]+(?:%s|[%s]{2,}|of|de|di|and|at)){0,3})" % (_CAPW, _UP, _CAPW, _UP), re.I)
PROSE_COPYRIGHT = re.compile(
    "(?:\xa9|\\(c\\)|\\bCopyright\\b|\\bCOPYRIGHT\\b)[ \t]*(?:\xa9|\\(c\\))?[ \t]*(?:\\d{4}(?:[ \t]*[-,][ \t]*\\d{2,4})*)?[ \t]*,?[ \t]*(?:by[ \t]*:?[ \t]+)?"
    "(?P<name>(?:%s|[%s0-9]{2,}|&)(?:[ \t.,]+(?:%s|[%s0-9]{2,}|&|and|of|de)){0,4})" % (_CAPW, _UP, _CAPW, _UP))
PROSE_PROPERTY = re.compile(
    "(?i)\\b(?:sole )?property of[ \t\r\n]+(?:the[ \t\r\n]+)?(?P<name>[^<>.;:\r\n]{3,60}?(?:\n[^<>.;:\r\n]{2,40}?)?)[ \t]*(?=[.;,]|\n\n|$|[ \t\n]+(?:any|and its|is|it|without|reproduction)\\b)")
_PARTICLES = "van|von|de|der|den|del|della|di|da|dos|du|le|la|ten|ter|zu|af|av"
PROSE_LABEL = re.compile(
    "(?i)(?<![A-Za-z\xc0-\xff])(?:drawn|drawn by|designed|designed by|design by|checked|checked by|approved|approved by|appr\\.?|"
    "author|authors|engineer|created by|made by|prepared by|modified by|reviewed by|released by|drafter|draftsman|designer|"
    "dibuixat|dissenyat|comprovat|aprovat|dessin\xe9 par|dessinateur|gezeichnet|gepr\xfcft|bearbeitet|bearb\\.?|gez\\.?|erstellt|"
    "dibujado|dise\xf1ado|revisado|aprobado|disegnato|progettista|autor|responsable|ersteller|konstrukteur|name|contact|contacte|contacto)"
    "[ \t]*(?:by|von|por|par)?[ \t]*[:.][ \t]*(?P<name>%s(?:[ \t]+(?:(?:%s)[ \t]+){0,2}(?:%s|[%s]\\.)){0,3})" % (_NAMEW, _PARTICLES, _NAMEW, _UP))
NAME_PARTICLES = set(_PARTICLES.split("|"))
# title-block rows "DRAWN EJG 12/5/17": initials, an account or a name between the label and the date
PROSE_SIGNED = re.compile(
    "(?<![A-Za-z])(?:DRAWN BY|DRAWN|CHECKED BY|CHECKED|CHK'?D|APPV'?D|APPR'?D|APPROVED BY|APPROVED|DESIGNED BY|DESIGNED|DRN|CHKD|"
    "ENG APPR\\.?|MFG APPR\\.?|Q\\.A\\.?|QA)[ \t]+(?P<name>[A-Z]{2,4}|[a-z]{3,12}|[A-Z]\\.[ \t]?[A-Z][a-z]+|[A-Z][a-z]+(?:[ \t][A-Z][a-z]+)?)"
    "[ \t]+(?=[0-9]{1,4}[/.\\-][0-9]{1,2}[/.\\-][0-9]{1,4})")
# revision tables and signature cells: initials directly before a date ("BJD 2/27/12")
PROSE_INITIALS_DATE = re.compile(
    "(?<![A-Za-z0-9])(?P<name>[A-Z]{2,4})[ \t]+(?=[0-9]{1,2}[/.\\-][0-9]{1,2}[/.\\-](?:[0-9]{2}|[0-9]{4})(?![0-9]))")
_NOT_INITIALS = set("""rev date ecn eco dwg qty mfg appr nts tbd ref typ max min dia thk iso din ansi sym chg dcn pcn new old was
per see by on of no id unc unf npt qa sht cad pdf std jan feb mar apr may jun jul aug sep sept oct nov dec mon tue wed thu fri
sat sun am pm mm cm in ft kg lb oz to at is as or an it we us eu uk usa on up do go ok vs ver doc dim tol mat est due end run
due day wk yr qc pn sn po so wo ex inc ltd co num nr nro nom sig rel iss iso bs en ed vol pg pp fig tab sec app""".split())
# a line of digit groups right under / above an e-mail or web address: a phone number
PROSE_BARE_PHONE = re.compile("(?m)^[ \t]*(?P<p>\\+?[0-9]{2,4}(?:[ .\\-][0-9]{2,4}){2,4})[ \t]*$")
PROSE_PARTNO = re.compile(
    "(?i)(?<![A-Za-z\xc0-\xff])(?:(?:drawing|dwg|drg|drw|part|item|document|doc|artikel|article|zeichnungs?|zeichn|teile?|sach|"
    "ident|plan|plano|dibujo|dessin|codi|c\xf3digo|codigo|reference|serial|order|job|model|modell)[ \t./-]{0,2}"
    "(?:no|nr|num|n\xb0|n\xba|number|nummer|n\xfamero|numero|#)(?![A-Za-z])[.]?|p/n|s/n|codi document|document code|drawing code|part code)"
    "[ \t]*[:.#]?[ \t]{0,3}(?P<num>[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+){0,6})")
_PARTNO_DATE = re.compile(r"^(?:\d{1,2}[./-]\d{1,2}[./-](?:\d{2}|\d{4})|(?:19|20)\d{2}[./-]\d{1,2}[./-]\d{1,2})$")
# alloys, tempers, threads and sizes that can follow a label in a title block (6061-T6, M8x1.25)
_PARTNO_SPEC = re.compile(r"(?i)^(?:\d{4}-?[tTHhOo]\d{0,4}$|m\d+(?:[x.]|$)|[\d.]+x[\d.]+|(?:iso|din|en|ansi|asme|astm|jis|aisi|sae|bs|gb|nf|uni)(?![a-z])|\d+(?:[.,]\d+)?(?:mm|cm|in|kg|g|deg)$)")
_PARTNO_WORDNUM = re.compile(r"^([a-z]{3,})(\d{1,4})$")


def _partno_ok(v):
    """The text after a "drawing no." label is a number: a multi-part code with 4+ digits
    or one word of 5+ characters with 3+ digits (not a date, a sheet size or an alloy)."""
    digits = sum(ch.isdigit() for ch in v)
    if _PARTNO_DATE.match(v) or "\x00" in v or _PARTNO_SPEC.search(v):
        return False
    parts = re.split("[-_./]", v)
    if len(parts) >= 2 and digits >= 4:
        return True
    return len(parts) == 1 and len(v) >= 5 and digits >= 3 or (len(parts) >= 2 and any(len(x) >= 5 and x[0:1].isalnum() and any(c.isdigit() for c in x) and any(c.isalpha() for c in x) for x in parts))


def _partno_tok(t):
    low = t.lower()
    if len(low) < 2:
        return False
    if low.isdigit():
        return len(low) >= 3
    if low.isalpha():
        return 3 <= len(low) <= 8 and not is_word(low) and not is_foreign_word(low)
    m = _PARTNO_WORDNUM.match(low)
    if m and (is_word(m.group(1)) or is_foreign_word(m.group(1))):
        return False
    return not CODE_GENERIC.match(low)


PROSE_URL = re.compile(r"(?i)(?<![A-Za-z0-9@._-])(?:https?://|www\.)(?P<host>[A-Za-z0-9][A-Za-z0-9.-]{1,80}\.[A-Za-z]{2,12})(?P<path>/[^\s<>\"')\]]{0,120})?")
PROSE_PHONE = re.compile(
    r"(?i)(?:(?<![A-Za-z\xc0-\xff])(?:tel|phone|fax|mobile|mob|cell|tlf|telf|t\xe9l|telefon|tel\xe9fono|telefono)\.?[ \t]*(?:no\.?|number|#)?[ \t]*[:.]?[ \t]*(?P<a>\+?\(?\d[\d ()\-/]{6,18}\d|\d{2,4}(?:\.\d{2,4}){2,4})(?![\d.,])"
    r"|(?<![\w.,/-])(?P<b>\+[1-9]\d{0,2}(?:[ -]\(?|\()\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4})(?![\w.,/-])"
    r"|(?<![\w.,/-])(?P<c>\(\d{3}\)[ ]?\d{3}[-.]\d{4}|\d{3}[-.]\d{3}[-.]\d{4})(?![\w.,/-]))")
PROSE_ADDR = re.compile("(?<![A-Za-z0-9])(?P<city>(?:%s[ \t]+){0,2}%s),[ \t]+(?P<st>[A-Z]{2})[ \t]+(?P<zip>\\d{5}(?:-\\d{4})?)(?![A-Za-z0-9])" % (_CAPW, _CAPW))
LEGAL_SUFFIX = re.compile(r"(?:Inc|Incorporated|LLC|L\.L\.C|Ltd|Limited|GmbH|AG|KG|Corp|Corporation|S\.A|SA|S\.L|SL|S\.r\.l|Srl|SRL|"
                          r"B\.V|BV|N\.V|NV|Pty|PLC|plc|A/S|AB|Oy|Kft|s\.r\.o|Sdn Bhd|Pvt|LLP|e\.V)$")
DETERMINERS = set("their its his her our your the this that these those every each any all some no their's respective".split())
PROSE_STOP = set("""tolerancing interpret geometric protocol drawing drawings university technology challenges
key allen phillips hex torx socket wrench driver respective owners holders with without
drawing drawn title scale sheet size date name rev revision material finish weight tolerance tolerances
dimensions dimension unless otherwise specified third angle projection do not scale part number qty quantity item description
assembly section detail view top bottom front back left right side general notes note approved checked designed engineer
all rights reserved proprietary confidential information contained this sole property any reproduction written permission
prohibited next used application comments mfg appr eng debur break sharp edges surface angular fractional decimal place
places two three interpret geometric tolerancing per bend mach see table figure page chapter table contents index
north south east west new old main sub base cover plate bracket mount holder shaft bearing gear motor screw bolt nut washer
pin spring housing frame panel block body arm leg head cap ring clip clamp spacer support adapter adaptor cable wire board
stainless steel aluminum aluminium brass copper plastic nylon abs pla petg rubber iso din ansi asme jis metric imperial
inch inches mm millimeters millimetres degrees degree january february march april may june july august september october
november december monday tuesday wednesday thursday friday saturday sunday version model type class series standard
bill materials open source hardware license creative commons attribution share alike public domain mit gnu apache
data sheet datasheet user manual guide instructions introduction overview features specifications technical performance
electrical mechanical characteristics absolute maximum ratings pin configuration functional typical application
high low max min nom ref typ thru hole holes dia diameter radius depth length width height thickness
united states america china germany france italy spain japan canada kingdom alle aller allen des der die das den dem ein eine einer bei mit von vor nach aus auf ohne oder und nicht wenn nur bis alt neu
hinweis benennung werkstoff datum blatt zeichnung gezeichnet bearbeitet norm massstab ansicht schnitt
esta este esto estos estas cara con sin para por del las los una uno sol mar nombre fecha tipo modelo parte pieza plano vista corte
detalle escala dibujado revisado aprobado firma titulo cantidad denominacion proyecto conjunto
avec sans pour dans sur sous les une vue coupe echelle nom dessin ensemble
per sezione scala nome data disegno assieme particolare
definiert angegeben bemasst sonst falls
""".split())


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
        self._seen = set()
        self.numbers_ok = False   # all-digit part numbers may be replaced (names, drawing text)
        self.in_name = False      # a file / folder name is being rewritten
        self.scopes = None        # collections holding an identical copy of this document (content rules)
        self.local_seqs = []      # drawing numbers read from this document's title (replaced in its text)
        self.digit_spans = []     # phone numbers / postcodes found in the text being scrubbed
        self.notes = []
        self.hits = 0

    def found(self, kind, value):
        value = value.strip()
        if value and len(self.findings) < 400 and (kind, value) not in self._seen:
            self._seen.add((kind, value))
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

        word_parts = set(getattr(dictionary, "bounded", ()))
        for a in dictionary.strong:
            bounded = a in word_parts or not (len(a) >= 6 or (len(a) >= 5 and any(c.isdigit() for c in a) and any(c.isalpha() for c in a)))
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
        inner = [a for a in dictionary.strong
                 if a not in word_parts and
                 (len(a) >= 6 or (len(a) >= 5 and any(c.isdigit() for c in a) and any(c.isalpha() for c in a)))]
        for a, scopes in scoped.items():
            if a in dictionary.strong:
                continue
            if len(a) >= 7 and any(x in a for x in inner):
                # a project name built on a corpus-wide name ("acmerobot"): same everywhere
                add(a, _Entry("S", None, False, None, False))
                add(_u16(a), _Entry("S", None, False, None, True))
                continue
            add(a, _Entry("C", None, True, frozenset(scopes), False))
            add(_u16(a), _Entry("C", None, True, frozenset(scopes), True))
        # part / document numbers of each collection (see identities.add_codes)
        codes = {}
        for scope, atoms in getattr(dictionary, "codes", {}).items():
            for a in atoms:
                codes.setdefault(a, set()).add(scope)
        for a, scopes in codes.items():
            if a in dictionary.strong or a in scoped:
                continue
            kind = "D" if a.isdigit() else "K"
            add(a, _Entry(kind, None, True, frozenset(scopes), False))
            add(_u16(a), _Entry(kind, None, True, frozenset(scopes), True))
        # multi-part numbers ("2020-048-10"): as written and with one separator throughout
        cps = {}
        for scope, lits in getattr(dictionary, "code_phrases", {}).items():
            for lit in lits:
                atoms = ATOM_RE.findall(lit)
                for v in set([lit] + [sep.join(atoms) for sep in ("-", "_", " ", ".")]):
                    ent = cps.get(v)
                    if ent is None:
                        ent = cps[v] = (set(), atoms)
                    ent[0].add(scope)
        for v, (scopes, atoms) in cps.items():
            if v in pats:
                continue
            add(v, _Entry("Q", atoms, True, frozenset(scopes), False))
            add(_u16(v), _Entry("Q", atoms, True, frozenset(scopes), True))
        # words that only the documents of one collection use (see Dictionary.auto):
        # replaced in document text, never in file names or native files
        autos = {}
        for scope, atoms in getattr(dictionary, "auto", {}).items():
            for a in atoms:
                autos.setdefault(a, set()).add(scope)
        for a, scopes in autos.items():
            if a in dictionary.strong:
                continue
            # (a word that is a name of some other collection is still this one's document word)
            scopes = set(scopes) - scoped.get(a, set()) - codes.get(a, set())
            if not scopes:
                continue
            add(a, _Entry("A", None, True, frozenset(scopes), False))
        self.code_mode = os.environ.get("DEID_CODES", "1")
        self._pats = pats
        # distinctive names that are also matched inside longer words: a longer word that
        # is replaced as a whole keeps the same pseudonym for that part (see token())
        self._inner = sorted((a for a in dictionary.strong
                              if a not in word_parts and
                              (len(a) >= 6 or (len(a) >= 5 and any(c.isdigit() for c in a) and any(c.isalpha() for c in a)))),
                             key=len, reverse=True)
        self._inner_set = set(self._inner)
        self._inner_auto = None
        if ahocorasick is not None and self._inner:
            B = ahocorasick.Automaton()
            for a in self._inner:
                B.add_word(a, len(a))
            B.make_automaton()
            self._inner_auto = B
        self._tok_cache = {}
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

    def token(self, t):
        """Pseudonym of one whole word.  Parts that are corpus-wide names on their own
        ("acme" in "AcmeRobot") get exactly the pseudonym they get elsewhere."""
        low = t.lower()
        cuts = self._tok_cache.get(low)
        if cuts is None:
            cuts = []
            if low not in self._inner_set and len(low) >= 6:
                found = []
                if self._inner_auto is not None:
                    for end, n in self._inner_auto.iter(low):
                        found.append((end - n + 1, end + 1))
                else:
                    for a in self._inner:
                        i = low.find(a)
                        while i >= 0:
                            found.append((i, i + len(a)))
                            i = low.find(a, i + 1)
                found.sort(key=lambda c: (c[0], -(c[1] - c[0])))
                last = 0
                for a, b in found:
                    if a >= last:
                        cuts.append((a, b))
                        last = b
            if len(self._tok_cache) < 500000:
                self._tok_cache[low] = cuts
        if not cuts:
            return pseudo.atom(t)
        out = []
        i = 0
        for a, b in cuts:
            if a > i:
                out.append(pseudo.atom(t[i:a]))
            out.append(pseudo.atom(t[a:b]))
            i = b
        if i < len(t):
            out.append(pseudo.atom(t[i:]))
        return "".join(out)

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
        if entry.scopes is not None:
            if ctx.scopes is None or ctx.in_name:
                if ctx.scope not in entry.scopes:
                    return False
            elif entry.scopes.isdisjoint(ctx.scopes):
                return False
        if entry.kind == "A":
            if entry.wide or not ctx.numbers_ok or ctx.in_name:
                return False
        elif entry.kind in ("K", "D", "Q") and self.code_mode == "text" and (ctx.in_name or not ctx.numbers_ok):
            return False      # experiment: numbers replaced in document text only
        if entry.kind == "D" or (entry.kind == "Q" and (e - s) < (18 if entry.wide else 9)
                                 and not any(c.isalpha() for a in entry.atoms for c in a)):
            # (a long multi-part number such as 01-118-00-0 is distinctive enough anywhere)
            # an all-digit part number: only in names, drawing text and references to
            # files - never in free numeric data
            if entry.wide:
                if not _fileref_w(data, e):
                    return False
            else:
                if s > 1 and data[s - 1] in b".," and 0x30 <= data[s - 2] <= 0x39:
                    return False
                if e + 1 < n and data[e] in b".," and 0x30 <= data[e + 1] <= 0x39:
                    return False
                if not (ctx.numbers_ok or _fileref(data, e)):
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
        elif entry.kind == "S" and ((s > 0 and data[s - 1] in ALNUM) or (e < n and data[e] in ALNUM)):
            a, b = s, e
            while a > 0 and s - a < 24 and data[a - 1] in ALNUM:
                a -= 1
            while b < n and b - e < 24 and data[b] in ALNUM:
                b += 1
            tok = data[a:b].decode("latin-1").lower()
            if tok.isalpha() and (is_word(tok) or is_foreign_word(tok)):
                return False      # part of an ordinary word
            if _inside_word(data[a:b].decode("latin-1"), s - a, e - a):
                return False      # "stemIn" in "CIDSystemInfo", "drawin" in "DrawingNumber"
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
        if entry.kind == "P" or entry.kind == "Q":
            out = []
            i = 0
            code = entry.kind == "Q"
            for a in entry.atoms:
                while i < len(t) and not t[i].isalnum():
                    out.append(t[i])
                    i += 1
                piece = t[i:i + len(a)]
                if code:      # revision letters and plain version / size parts stay
                    keep = len(a) <= 2 or (not a.isdigit() and (CODE_GENERIC.match(a) or is_word(a)))
                else:
                    keep = a in STOP
                out.append(piece if keep else self.token(piece))
                i += len(a)
            out.append(t[i:])
            r = "".join(out)
        else:
            r = self.token(t)
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
    def detect(self, text, ctx, unit_spans, is_step=False, is_xml=False, is_dxf=False, is_ps=False, prose=False):
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
        if prose:
            self._prose(text, ctx, unit_spans)
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

    def _prose(self, text, ctx, unit_spans):
        """Names of people and organisations, phone numbers, web addresses and postal
        addresses in visible prose (PDF page text, title blocks)."""
        def cls(tok):
            low = tok.lower().strip(".")
            if low in PROSE_STOP or low in STOP or low in SOFTWARE or len(low) < 2:
                return "S"
            if is_first_name(low):
                return "F"
            if not is_word(low):
                return "N"
            if is_surname(low):
                return "L"
            return "W"

        # words of the file's own name are left to the corpus-wide rules, so a drawing's
        # title stays in step with the names of the model files it belongs to
        own = getattr(ctx, "_own", None)
        if own is None:
            if os.environ.get("DEID_PDF_OWNSKIP") == "0":
                own = ctx._own = set()      # names found in the text are replaced even when the file name repeats them
            else:
                own = ctx._own = set(x.lower() for x in ATOM_RE.findall(ctx.path.rsplit("/", 1)[-1]))

        def span(a, b, kind, person=False, only=None):
            value = text[a:b]
            atoms = [x.lower() for x in ATOM_RE.findall(value)]
            if not atoms or all(x in ctx.produced or x in STOP or x in PROSE_STOP or x in own for x in atoms):
                return
            for mm in re.finditer("[A-Za-z0-9\xc0-\xff]+", value):
                tok = mm.group(0)
                if tok.lower() in own or (only is not None and not only(tok)):
                    continue
                unit_spans.append((a + mm.start(), a + mm.end()))
            ctx.found(kind, value)
            self._learn(" ".join(x for x in ATOM_RE.findall(value) if x.lower() not in own), ctx, person=person)

        took = [False]
        retry = [False]

        def persons():
            # candidates may overlap ("DTU MPP Magnus Kjer"): after a rejected one, look
            # again from its second word
            pos = 0
            rejected_end = -1
            while True:
                mm = PROSE_PERSON.search(text, pos)
                if not mm:
                    return
                took[0] = False
                retry[0] = mm.start() < rejected_end       # overlaps a candidate just rejected
                yield mm
                if took[0]:
                    pos = mm.end()
                else:
                    pos = mm.start() + len(mm.group("first"))
                    rejected_end = max(rejected_end, mm.end())

        for m in persons():
            first, last = m.group("first"), m.group("last")
            lasts = last.split()
            c1 = cls(first)
            cl = [cls(x) for x in lasts]
            if len(first) < 3 or len(lasts[0]) < 3 or not (first + lasts[0]).isascii() and sum(ch.isascii() for ch in first + lasts[0]) < 4:
                continue
            if retry[0] and c1 != "S":
                # a second look (after a rejected overlapping candidate) only for a clear
                # "Given Family" pair in normal case
                if not (c1 == "F" and not (first.isupper() and lasts[0].isupper())
                        and (cl[0] in ("N", "F") or (cl[0] == "L" and not is_word(lasts[0].lower())))):
                    continue
            if c1 == "S" or cl[0] == "S":
                # "Drawn John Smith": try again without the leading generic word
                if c1 == "S" and len(lasts) == 2 and cl[0] == "F" and cl[1] in ("N", "F", "L"):
                    span(m.start("last"), m.end("last"), "pdf-person", True)
                    took[0] = True
                continue
            caps_all = first.isupper() and lasts[0].replace("-", "").isupper()
            mid = bool(m.group("mid").strip())
            ok = False
            if c1 == "F" and cl[0] in ("N", "F"):
                ok = True
            elif c1 == "F" and cl[0] == "L" and (not caps_all or mid):
                ok = True
            elif c1 == "F" and cl[0] == "W" and not caps_all and mid:
                ok = True
            elif c1 == "N" and cl[0] == "F" and not caps_all and len(first) >= 4:
                ok = True            # family name first
            elif (c1 == "F" and len(lasts) == 2 and cl[0] in ("W", "L") and cl[1] == "N" and len(lasts[1]) >= 4
                  and not caps_all and not mid):
                ok = True            # "Magnus Bolt Kjer": given name, middle name that is also a word, family name
            elif c1 in ("N", "L") and cl[0] in ("N", "L") and mid and not caps_all:
                ok = True            # Xxxx Q. Yyyy
            if not ok:
                continue
            took[0] = True
            if len(lasts) == 2 and cl[1] in ("N", "F", "L") and not (cl[1] == "L" and caps_all):
                span(m.start("first"), m.end("last"), "pdf-person", True)
            else:
                span(m.start("first"), m.start("last") + len(lasts[0]), "pdf-person", True)
        for m in PROSE_INITIAL.finditer(text):
            if cls(m.group("last")) == "N" and len(m.group("last")) >= 4:
                span(m.start("last"), m.end("last"), "pdf-person", True)
        for m in PROSE_DOTTED.finditer(text):
            # "J.MOSKWA", "M.Littmann": initial and family name written together
            if cls(m.group("last")) != "S" and len(set(m.group("last").lower())) > 2:
                span(m.start("last"), m.end("last"), "pdf-person", True)
        for m in PROSE_LABEL.finditer(text):
            toks = [t for t in m.group("name").split() if t not in NAME_PARTICLES]
            if not toks:
                continue
            if all(cls(t) in ("F", "N") for t in toks if len(t.strip(".")) > 1):
                span(m.start("name"), m.end("name"), "pdf-person", True, only=lambda t: t not in NAME_PARTICLES)
            elif (len(toks) >= 2 and len(toks) < len(m.group("name").split()) and cls(toks[0]) in ("F", "N")
                  and all(cls(t) in ("F", "N", "L") for t in toks[1:] if len(t.strip(".")) > 1)):
                # "made by: Menno van den Berg": a family name with particles
                span(m.start("name"), m.end("name"), "pdf-person", True, only=lambda t: t not in NAME_PARTICLES)
        for m in PROSE_SIGNED.finditer(text):
            v = m.group("name")
            low = v.lower()
            if low in PROSE_STOP or low in STOP or low in ("date", "name", "size", "sign", "none", "title", "sheet", "scale"):
                continue
            if v.islower() and (is_word(low) or is_foreign_word(low)):
                continue
            if v.isupper() and len(v) == 4 and is_word(low):
                continue
            span(m.start("name"), m.end("name"), "pdf-person", True)
        for m in PROSE_INITIALS_DATE.finditer(text):
            v = m.group("name")
            low = v.lower()
            if low in _NOT_INITIALS or low in PROSE_STOP or low in STOP or (len(v) >= 3 and is_word(low) and not is_first_name(low)):
                continue
            span(m.start("name"), m.end("name"), "pdf-person", True)
        for m in PROSE_BARE_PHONE.finditer(text):
            digits = sum(ch.isdigit() for ch in m.group("p"))
            if not 8 <= digits <= 13:
                continue
            a = text.rfind("\n", 0, max(0, m.start() - 1))
            b = text.find("\n", m.end() + 1)
            near = text[max(0, a if a >= 0 else 0):m.start()] + text[m.end():b if b >= 0 else len(text)]
            if "@" in near or "www." in near.lower():
                ctx.digit_spans.append(m.span("p"))
                ctx.found("pdf-phone", m.group("p"))
        for lit in ctx.local_seqs:
            # the document's own number (from its title), wherever the text repeats it
            rx = "[-_./ ]?".join(re.escape(x) for x in re.split("[-_./]", lit) if x)
            for m in re.finditer("(?i)(?<![A-Za-z0-9])" + rx + "(?![A-Za-z0-9])", text):
                span(m.start(), m.end(), "pdf-number", only=_partno_tok)
        for m in PROSE_PARTNO.finditer(text):
            # "DWG NO. 5BHMBA-DA-D-W2", "Zeichnungsnummer: 2020-048-10": the number after the label
            if _partno_ok(m.group("num")):
                span(m.start("num"), m.end("num"), "pdf-number", only=_partno_tok)
        for m in PROSE_ORG.finditer(text):
            toks = [t for t in m.group("name").split() if t[0].isupper() or t[0].isdigit()]
            if not toks:
                continue
            classes = [cls(t) for t in toks]
            if not any(len(t) >= 3 and t.isascii() and t[0].isalpha() for t in toks) or "\x00" in m.group("name"):
                continue
            if any(c in ("N", "F") for c in classes):
                span(m.start("name"), m.end("name"), "pdf-org", only=lambda t: cls(t) in ("N", "F", "L"))
            elif len(toks) >= 2 and all(c in ("W", "L") for c in classes) and LEGAL_SUFFIX.match(m.group("suf")):
                span(m.start("name"), m.end("name"), "pdf-org")
        for m in PROSE_ORG_PREFIX.finditer(text):
            toks = m.group("name").split()
            if cls(toks[0]) != "S" and toks[0][0].isupper() and len(toks[0]) >= 3 and toks[0].isascii():
                span(m.start("name"), m.end("name"), "pdf-org")
        for m in PROSE_COPYRIGHT.finditer(text):
            toks = [t for t in m.group("name").replace(",", " ").split()]
            if toks and toks[0][0].isalpha() and cls(toks[0]) != "S" and toks[0].lower() not in DETERMINERS:
                span(m.start("name"), m.end("name"), "pdf-org", only=lambda t: not t.isdigit())
        for m in PROSE_PROPERTY.finditer(text):
            v = m.group("name").strip()
            first = v.split()[0] if v.split() else ""
            if (first and first[0].isupper() and first.lower() not in DETERMINERS
                    and "insert" not in v.lower() and "company name" not in v.lower()):
                span(m.start("name"), m.end("name"), "pdf-org")
        for m in PROSE_URL.finditer(text):
            host = m.group("host").lower()
            if any(host == v or host.endswith("." + v) for v in VENDOR_DOMAINS) and not host.endswith((".github.io", ".gitlab.io")):
                continue
            labels = host.split(".")
            if labels[-1] not in TLDS:
                continue
            hs = m.start("host")
            keep = 2 if len(labels) >= 3 and labels[-2] in ("co", "com", "ac", "edu", "org", "gov", "net") and len(labels[-1]) == 2 else 1
            if host.endswith((".github.io", ".gitlab.io")):
                keep = 2
            end = hs + len(host) - sum(len(x) + 1 for x in labels[-keep:])
            start = hs + (4 if host.startswith("www.") else 0)
            if end > start:
                span(start, end, "pdf-url")
        for m in PROSE_PHONE.finditer(text):
            g = "a" if m.group("a") else "b" if m.group("b") else "c"
            digits = sum(ch.isdigit() for ch in m.group(g))
            if 9 <= digits <= 15:
                ctx.digit_spans.append(m.span(g))
                ctx.found("pdf-phone", m.group(g))
        for m in PROSE_ADDR.finditer(text):
            if m.group("st") in US_STATES:
                span(m.start("city"), m.end("city"), "pdf-place")
                ctx.digit_spans.append(m.span("zip"))

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
            flags = dict(is_step=kind == "step", is_xml=kind in ("xml", "svg"), is_dxf=kind == "dxf", is_ps=kind == "ps",
                         prose=kind == "prose")
            if not binary:
                ctx.digit_spans = []
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
        dspans = ctx.digit_spans if detect and not binary else []
        ctx.digit_spans = []
        if dry:
            return data
        occupied = [(s, e) for s, e, _ in plan]
        extra = []
        for (a, b) in dspans:
            # phone numbers and postcodes: new digits, same layout
            digs = [i for i in range(a, b) if 0x30 <= data[i] <= 0x39]
            if len(digs) < 4 or any(a < y and x < b for x, y in occupied):
                continue
            new = pseudo.atom("".join(chr(data[i]) for i in digs))
            if len(new) != len(digs):
                continue
            for i, ch in zip(digs, new):
                extra.append((i, i + 1, ch.encode("latin-1")))
            occupied.append((a, b))
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
        r = self.token(t)
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
                if not entry.wide and (entry.scopes is None or ctx.scope in entry.scopes
                                       or (ctx.scopes is not None and not entry.scopes.isdisjoint(ctx.scopes))):
                    return True
        return False

    # -- strings (paths, names, decoded metadata) ---------------------
    def rename(self, value, ctx):
        """Apply corpus-wide identities only (file and folder names, archive members)."""
        raw = value.encode("utf-8")
        keep, ctx.numbers_ok = ctx.numbers_ok, True
        keep_name, ctx.in_name = ctx.in_name, True
        try:
            plan = self._global_plan(raw, ctx, False)
        finally:
            ctx.numbers_ok = keep
            ctx.in_name = keep_name
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
        # mixed scripts: characters outside Latin-1 are held aside (as NUL) while the
        # rest is scrubbed, then put back at the same positions
        held = [(i, c) for i, c in enumerate(value) if ord(c) > 255]
        tmp = "".join(c if ord(c) < 256 else "\x00" for c in value)
        out = self.scrub(tmp.encode("latin-1"), ctx, False, kind).decode("latin-1")
        if len(out) == len(value):
            chars = list(out)
            for i, c in held:
                chars[i] = c
            return "".join(chars)
        raw = value.encode("utf-8")
        return self.scrub(raw, ctx, False, kind).decode("utf-8", "replace")

    def scrub_field(self, value, ctx, kind="field"):
        """De-identify a str that *is* an identity field (author, company ...)."""
        if not value or value.strip().lower() in FIELD_STOP:
            return value
        atoms0 = [a.lower() for a in ATOM_RE.findall(value)]
        if not atoms0 and pseudo.FOREIGN_RE.search(value):
            # a name written in a non-Latin script
            new = pseudo.foreign(value)
            if new != value and value not in ctx.produced:
                ctx.found(kind, value)
                ctx.produced.add(new)
                ctx.hits += 1
                return new
            return value
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
                seg_new = self.token(m.group(0))
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


_FILEREF = re.compile(rb"^(?:[A-Za-z0-9_\-.()+&'#~\[\]]| (?! )){0,48}?\.(?:sldprt|sldasm|slddrw|ipt|iam|idw|ipn|step|stp|stl|pdf|dwg|dxf|"
                      rb"igs|iges|x_t|x_b|sat|par|asm|psm|dft|prt|drw|catpart|catproduct|catdrawing|f3d|fcstd|3mf|obj|easm|eprt|"
                      rb"edrw|png|jpg|jpeg|zip)(?![A-Za-z0-9])", re.I)


def _fold(low):
    if low.isascii():
        return low
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFKD", low) if not unicodedata.combining(c))


_SEG = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


def _plain(w):
    w = w.lower()
    return len(w) >= 3 and w.isalpha() and (is_word(w) or is_foreign_word(w))


def _inside_word(tok, s, e):
    """True when tok[s:e] (a name found inside the longer token ``tok``) is not a name
    part of that token: it starts and ends in the middle of words, or the words it
    touches are ordinary ones (CIDSy[stemIn]fo, [Drawin]gNumber, [drawin]gs)."""
    segs = [(m.start(), m.end()) for m in _SEG.finditer(tok)]
    over = [(a, b) for a, b in segs if a < e and b > s]
    if not over:
        return False
    left = over[0][0] == s
    right = over[-1][1] == e
    if left and right:
        return False                      # exactly whole parts: Tesla|Turbine, BCN3D|Sigma
    if not left and not right:
        return True                       # strictly inside
    if all(_plain(tok[a:b]) for a, b in over):
        return True                       # the parts it touches are ordinary words
    low = tok.lower()
    if left:                              # an ordinary word that merely begins with the name
        a, b = over[-1]
        for k in range(e + 1, min(b, e + 9) + 1):
            if _plain(low[s:k]):
                return True
    else:                                 # ... or merely ends with it
        a, b = over[0]
        for k in range(max(a, s - 9), s):
            if _plain(low[k:e]):
                return True
    return False


def _fileref(data, e):
    """True if the bytes after position ``e`` complete a file name (…​.SLDPRT, …​.pdf)."""
    return _FILEREF.match(data[e:e + 90]) is not None


def _fileref_w(data, e):
    tail = data[e:e + 180]
    if len(tail) % 2:
        tail = tail[:-1]
    try:
        # characters outside Latin-1 (binary data after the name) end the name: they must
        # not be dropped, or the bytes after them would read as part of it
        t = tail.decode("utf-16-le", "replace").encode("latin-1", "replace")
    except Exception:
        return False
    return _FILEREF.match(t) is not None


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
