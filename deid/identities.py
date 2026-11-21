"""Identity dictionary: which names are replaced, and where.

Three kinds of entry:
  * strong atoms   - distinctive tokens (account names, organisation names, domains)
                     replaced everywhere in the corpus;
  * phrases        - compounds such as ``Source-Robotics`` whose words are ordinary on
                     their own; replaced only when they appear together;
  * scoped atoms   - ordinary-looking names that are only replaced inside the
                     repository / collection they identify.

Per-file findings (author fields, user-profile paths, e-mail addresses ...) are handled
by ``deid.engine`` and logged so later runs can promote them to global entries.
"""
import gzip
import json
import os
import re

from . import pseudo

HERE = os.path.dirname(os.path.abspath(__file__))
ATOM_RE = re.compile(r"[A-Za-z0-9]+")
HEX12 = re.compile(r"^[0-9a-f]{12}$")

_WORDS = None


def words():
    global _WORDS
    if _WORDS is None:
        with gzip.open(os.path.join(HERE, "data", "words.txt.gz"), "rt") as f:
            _WORDS = set(f.read().split())
    return _WORDS


# Never treated as an identity: software, formats, platforms, generic account names,
# legal suffixes, engineering vocabulary.  (Lower case.)
STOP = set("""
user users admin administrator owner author authors unknown none anonymous anon default public guest
student students pc desktop laptop win windows home root test tester null name username temp tmp shared
all nobody everyone me my your local system service server client host localhost workstation computer
company organization organisation org inc llc ltd gmbh co kg corp corporation sa ag bv srl plc pty spa
the of and for von van der de la le el di da und et
solidworks sldworks swstep dassault systemes catia delmia enovia autodesk autocad inventor fusion fusion360
revit siemens nx unigraphics ugs solidedge parasolid ptc creo proe pro engineer parametric technology
onshape freecad opencascade open cascade occ occt kicad eeschema pcbnew blender rhino rhinoceros mcneel
sketchup cadquery build123d openscad step iges stp igs sat acis spatial interop translation framework
developer steptools tools st microsoft office word excel adobe acrobat distiller pdf postscript ghostscript
inkscape sodipodi illustrator coreldraw corel libreoffice openoffice chrome zemax shx
github gitlab bitbucket git dropbox onedrive icloud drive switchdrive documents downloads
linux ubuntu debian macos osx
hoops publish techsoft pdfcreator pscript reportlab qt cairo skia quartz
www http https com net org edu gov io dev app web cdn media static files assets public download downloads
api blob core cloud prod content images image docs doc
model models part parts assembly assemblies drawing drawings project projects design designs cad cam cae
hardware mechanical mechanics electronics software firmware library libraries lib tools tool files file
data dataset datasets examples example sample samples test tests demo docs documentation resources
master main release releases version versions final draft copy backup old new archive archives
""".split())

# Field values naming one of these are tool signatures, not people or organisations.
SOFTWARE = set("""
solidworks sldworks swstep dassault systemes catia delmia enovia autodesk autocad inventor fusion360 revit
siemens unigraphics ugs solidedge parasolid ptc creo parametric onshape freecad opencascade cascade kicad
eeschema pcbnew blender rhino rhinoceros mcneel sketchup cadquery build123d openscad steptools spatial
microsoft adobe acrobat distiller ghostscript inkscape sodipodi illustrator coreldraw libreoffice openoffice
hoops techsoft pdfcreator pscript reportlab zemax matplotlib cairo zwcad zwsoft sdrc ideas ezdxf
draftsight bricscad librecad qcad nanocad tinkercad shapr3d alibre ironcad turbocad vectorworks
""".split())

GENERIC_REPO = set("""cad files file hardware models model parts part design designs drawings drawing projects
project mechanical mechanics public library libraries docs solidworks freecad step stl resources assets
3d printed printing printer print open source opensource robot robotics machine machines kit kits build
main master repo repository practice portfolio collection data dataset examples tutorial tutorials course
engineering dev development test tests misc stuff things work my the of and for with a an to in on""".split())

# Hosts whose names are format / tooling references, not corpus identities.
VENDOR_DOMAINS = (
    "w3.org", "adobe.com", "purl.org", "autodesk.com", "solidworks.com", "3ds.com", "ptc.com",
    "siemens.com", "sourceforge.net", "inkscape.org", "creativecommons.org", "steptools.com",
    "opencascade.com", "opencascade.org", "freecadweb.org", "freecad.org", "github.com",
    "githubusercontent.com", "gitlab.com", "microsoft.com", "openxmlformats.org", "xmlsoap.org",
    "apache.org", "gnu.org", "mozilla.org", "google.com", "googleapis.com", "apple.com",
    "iso.org", "aiim.org", "npes.org", "color.org", "iptc.org", "schema.org", "xml.org",
    "kicad.org", "kicad-pcb.org", "python.org", "example.com", "example.org", "localhost",
    "dublincore.org", "oasis-open.org", "khronos.org", "web3d.org", "opengis.net", "ogp.me",
)

# Organisations / people named in the collection titles and site names of the corpus
# layout (sources/catalog, sources/web, sources/existing).  Curated by reading the manifest.
CURATED_STRONG = """
thorlabs ligo caltech lulzbot faulhaber traceparts misumi festo motedis ooznest openppg
bcn3d icub ergocub robotcub iit iitk iitm nist usnistgov lbnl epfl ntnu rwth ukceh zsw hyfab
stiima adrena fenrir locilamp fabcity buildlog pipettin opensourceecology ose ohwr omnimech
vheliotech vhelio seedsovereignty openframe odri trillium takeme2space mesospim
sherline shopbottools haascnc gunt cortona3d ritron darley mantion valqua samcon marek
apogeerockets littlemachineshop machinistblog homemodelenginemachinist modelenginenews
modelengineeringwebsite modelenginebuilder modelesavapeur modelbouwatelier stuartmodels
journeymans soumard neme bourkeengines titansofcnc dmscncrouters vetcnc cncbasics
engineprofessional thetoolanddieguy engineeringforchange itwwelding nikkenren javada
tu-chemnitz chemnitz obuda gantep sti-beziers usst ncut aqvtc huayu zztrc utzmg unnoba uabc
utoronto missouri tecnico ulisboa mjcollege bharatskills transtutors pearsoncmg iutenligne
openhardware mechatronics nebomusic fingers-welt sariasan mobasi chengtudasai dihbu40
wto-tools crane-x7 parol6 keychron antmicro sciurus17 rt-net rtnet
southampton stuttgart ljubljana aachen delft aarhus kanpur hamburg
mcmaster-carr mcmaster edmund-optics misumi-ec banggood adafruit sparkfun pololu
aligo awi bam unh throwbot liotech powercube6 loong qingloong openloong janelia irnas
drexel cicirello regli guilherme bourjault locher ammar bilalmubdir mubdir sbrooks1 sbrooks2 rumainum
himalia13 himalia okika varsay wingxine changjiang purdue uiuc wsc2019 worldskills2019 skillsaustria
asimov asv1 popi popiv10 popiv9 nerell koruza sciurus17 stararm stararm102 xlerobot openarm pennypress
longmill olsk memsduino partcad cmpe2150 meb1302 twpatron emorph raptar cryoelec frosti k2tirf cytkit
openwater ampel360 triboslider hardgate crnhzmw tonegawa bosem topmass mycami2 kagnex honpine wuji huruy
ashiko trika maidigongjuji61 eduscol cnpm usli atom01 roboto rebot sienci willwinder qing lince
icub1 icub2 icub3 ecub ecub1 heriot-watt heriot
""".split()

# Suppliers and brands of bought-in parts.  Off by default (their part names are
# engineering content); enabled with DEID_VENDORS=1 to treat them as company identities.
VENDORS = """
wuerth wurth phoenixcontact molex amphenol samtec hirose vishay murata kemet belfuse fastron chilisin
sunlord coilcraft traco recom neosid standexmeder stocko wago schrack altech kubler nidec maxon moog igus
hiwin mitutoyo legris haas kuka emika banebots makerbeam openbuilds bluco rexnord torrington inafag balseal
koolance aavid tattu dremel ikea lego bentley gilson megatech carlstahl connecttech kingtek powerintegrations
batteryspace daratech dynamixel robotis feetech hitec futaba waveshare bondtech bambu bambulab phaetus voron
actuonix optotune qioptiq conoptics scanlab fisba photometrics gocator vicon omron panasonic cubemars
myactuator robstride damiao encos steadywin leaderdrive zltech hebi shapeoko jobin-yvon wollensak
""".split()

CURATED_PHRASES = [
    "Aleph Objects", "Open Source Ecology", "Open Lab Automata", "Curious Community Labs",
    "Muzi Works", "Dominique Lachiver", "Maximilian Behm", "Maxwell Lohss", "Tiny Oat Collider",
    "RT CORPORATION", "RT Corp", "TU Delft", "IIT Kanpur", "Ulrich Rapp", "ulrich-rapp",
    "Edmund Optics", "Teenage Engineering", "teenage.engineering", "Stuart Models",
    "Little Machine Shop", "Model Engineer", "Sienci Labs", "Precious Plastic", "One Army",
]


_NAMES = None


def is_first_name(low):
    global _NAMES
    if _NAMES is None:
        with gzip.open(os.path.join(HERE, "data", "names.txt.gz"), "rt") as f:
            _NAMES = set(f.read().split())
    return low in _NAMES


def is_word(low):
    return low in words()


def strong_atom(low):
    """True if ``low`` is distinctive enough to replace corpus-wide."""
    if len(low) < 4 or low in STOP or low.isdigit():
        return False
    if is_word(low):
        return False
    if len(low) < 5 and low.isalpha():
        return False  # 4-letter non-words are too collision-prone
    return True


def scope_of(path):
    """Group a corpus path into the repository / collection it belongs to."""
    p = path.split("/")
    if p[0] == "archives" and len(p) > 1:
        return "repo:" + _repo_key(os.path.splitext(p[1])[0])
    if p[0] == "sources" and len(p) > 2:
        kind = p[1]
        if kind in ("github", "gitlab"):
            return "repo:" + _repo_key(p[2])
        if kind == "existing":
            return "existing:" + "/".join(p[2:5])
        return kind + ":" + p[2]
    return "top:" + p[0]


def _repo_key(component):
    parts = component.split("__")
    if len(parts) > 2 and HEX12.match(parts[-1]):
        parts = parts[:-1]
    return "__".join(parts).lower()


def split_repo_component(component):
    """owner__repo[__sha] or gitlab.com__group__...__repo -> (owner parts, repo)."""
    parts = [x for x in component.split("__")]
    if len(parts) > 2 and HEX12.match(parts[-1]):
        parts = parts[:-1]
    if parts and parts[0].lower() in ("gitlab.com", "github.com"):
        parts = parts[1:]
    parts = [x for x in parts if x]
    if not parts:
        return [], ""
    if len(parts) == 1:
        return parts, ""
    return parts[:-1], parts[-1]


class Dictionary(object):
    def __init__(self):
        self.strong = set()      # lower-case atoms replaced everywhere
        self.phrases = {}        # canonical lower-case phrase -> list of atoms
        self.scoped = {}         # scope -> set(lower-case atoms)
        self.scoped_phrases = {}  # scope -> list of atom lists (project names made of ordinary words)

    # -- construction -------------------------------------------------
    def add_name(self, name, scope=None, force_scoped=False):
        """Register a name (account, organisation, repo ...)."""
        atoms = [a for a in ATOM_RE.findall(name)]
        lows = [a.lower() for a in atoms]
        sig = [a for a in lows if a not in STOP and len(a) >= 2]
        if not sig:
            return
        any_strong = False
        for a in sig:
            if strong_atom(a):
                self.strong.add(a)
                any_strong = True
            elif scope and force_scoped and len(a) >= 3 and not is_word(a):
                self.scoped.setdefault(scope, set()).add(a)
        if len(lows) > 1 and not all(strong_atom(a) or a in STOP for a in lows):
            # compound with ordinary words: replace as a whole phrase
            if sum(len(a) for a in lows) >= 6:
                self.phrases["-".join(lows)] = lows
        elif len(lows) == 1 and not any_strong and scope and len(lows[0]) >= 3:
            self.scoped.setdefault(scope, set()).add(lows[0])

    def add_manifest_paths(self, paths):
        seen = set()
        for path in paths:
            p = path.split("/")
            comp = None
            if p[0] == "archives" and len(p) > 1:
                comp = os.path.splitext(p[1])[0]
            elif p[0] == "sources" and len(p) > 2 and p[1] in ("github", "gitlab"):
                comp = p[2]
            if comp is None or comp in seen:
                continue
            seen.add(comp)
            scope = "repo:" + _repo_key(comp)
            owners, repo = split_repo_component(comp)
            for o in owners:
                self.add_name(o, scope, force_scoped=True)
            if repo:
                self.add_repo_name(repo, scope)

    def add_repo_name(self, repo, scope):
        # Project names are replaced inside their own repository only: many repo
        # names are technical terms (nema17, esp32 ...) that must survive elsewhere.
        lows = [a.lower() for a in ATOM_RE.findall(repo)]
        for a in lows:
            if a not in STOP and len(a) >= 4 and not a.isdigit() and not is_word(a):
                self.scoped.setdefault(scope, set()).add(a)
        # a multi-word project name ("open-source-rover") is replaced as a whole phrase,
        # inside its own repository only, unless it is purely generic ("cad-files")
        if len(lows) >= 2 and sum(len(a) for a in lows) >= 8 and not all(a in GENERIC_REPO or a in STOP or a.isdigit() for a in lows):
            self.scoped_phrases.setdefault(scope, []).append(lows)

    def add_curated(self):
        toks = list(CURATED_STRONG)
        if os.environ.get("DEID_VENDORS") == "1":
            toks += VENDORS
        for tok in toks:
            lows = [a.lower() for a in ATOM_RE.findall(tok)]
            if len(lows) == 1:
                if len(lows[0]) >= 3:
                    self.strong.add(lows[0])
            else:
                self.phrases["-".join(lows)] = lows
                for a in lows:
                    if strong_atom(a):
                        self.strong.add(a)
        for ph in CURATED_PHRASES:
            lows = [a.lower() for a in ATOM_RE.findall(ph)]
            if len(lows) == 1:
                self.strong.add(lows[0])
            else:
                self.phrases["-".join(lows)] = lows

    def add_web_domains(self, paths):
        seen = set()
        for path in paths:
            p = path.split("/")
            if len(p) > 2 and p[0] == "sources" and p[1] in ("web", "catalog") and "." in p[2] and " " not in p[2]:
                if p[2] in seen:
                    continue
                seen.add(p[2])
                self.add_domain(p[2])

    def add_domain(self, host):
        labels = host.lower().split(".")
        if any(host.lower().endswith(v) for v in VENDOR_DOMAINS):
            return
        for lab in labels[:-1]:
            for a in ATOM_RE.findall(lab):
                if strong_atom(a) and len(a) >= 5:
                    self.strong.add(a)
            if "-" in lab:
                lows = ATOM_RE.findall(lab)
                if sum(len(a) for a in lows) >= 6 and not all(a in STOP for a in lows):
                    self.phrases["-".join(lows)] = lows

    def add_discovered(self, obj):
        """Names promoted from identity fields seen in earlier runs (see report.promote)."""
        if isinstance(obj, list):
            obj = {"strong": obj, "scoped": {}}
        for a in obj.get("strong", []):
            a = a.lower()
            if strong_atom(a) and len(a) >= 5 and a not in SOFTWARE:
                self.strong.add(a)
        for scope, atoms in obj.get("scoped", {}).items():
            for a in atoms:
                a = a.lower()
                if a not in STOP and len(a) >= 4:
                    self.scoped.setdefault(scope, set()).add(a)

    # -- persistence (private mapping; never uploaded) ----------------
    def to_json(self):
        return {"strong": sorted(self.strong), "phrases": self.phrases,
                "scoped": {k: sorted(v) for k, v in self.scoped.items()},
                "scoped_phrases": self.scoped_phrases}

    @classmethod
    def from_json(cls, obj):
        d = cls()
        d.strong = set(obj.get("strong", []))
        d.phrases = dict(obj.get("phrases", {}))
        d.scoped = {k: set(v) for k, v in obj.get("scoped", {}).items()}
        d.scoped_phrases = dict(obj.get("scoped_phrases", {}))
        return d

    def save(self, path):
        with open(path, "w") as f:
            json.dump(self.to_json(), f)

    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls.from_json(json.load(f))

    def mapping_table(self):
        """Private original->pseudonym table (for audit; keep outside the submission)."""
        rows = {a: pseudo.atom(a) for a in sorted(self.strong)}
        for ph, atoms in self.phrases.items():
            rows[ph] = "-".join(pseudo.atom(a) for a in atoms)
        return rows
