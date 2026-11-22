"""Recall pass for drawing / document text (switch DEID_PDF_RECALL=1).

Three kinds of identifying strings that the corpus-wide dictionary does not know:

  * part, drawing and document numbers written in the text (parts lists, referenced
    documents, supplier order codes) - recognised by their shape, with the engineering
    notations that look similar (standards, alloys, threads, sizes, dates, fits) excluded;
  * suppliers and manufacturers named in parts lists and notes;
  * a family name standing alone (signature boxes, revision tables).

Everything here only decides *what* is a name; the pseudonyms come from the engine.
"""
import re

from .identities import CODE_EXTS, is_word

_STD = re.compile(
    r"(?i)^(?:iso|din|en|ansi|asme|astm|uni|jis|bs|mil|sae|iec|nf|gb|gost|aisi|unc|unf|unef|npt|bsp|bspp|bspt|ipc|ul|csa|"
    r"vde|nema|awg|rohs|ral|ip|dinen|eniso|cern|ohl)[\s\-_.:]*\d")
_REJECT = [re.compile(p) for p in (
    # a number with a unit, a size, a tolerance, a coordinate
    r"(?i)^[±+\-~<>≤≥=]?[ø⌀r]?\d{1,4}(?:[.,]\d+)?(?:mm|cm|m|in|inch|ft|kg|g|lb|lbs|n|nm|°|º|deg|v|vdc|vac|a|ma|w|kw|hz|khz|mhz|rpm|bar|"
    r"psi|mpa|gpa|kpa|hrc|hv|hb|um|µm|μm|s|sec|ms|min|h|hr|°c|ºc|°f|c|k|x|%|l|ml|nf|uf|pf|ohm|kn|mm2|mm3|cm3|ipm|sfm|th|st|nd|"
    r"rd|d|t|p|pcs|pc|off|places|pl|plcs|dpi|px|bit|gb|mb|kb|mah|ah|wh|lm|cd|db|ppm|awg|way|pin|pins|pos|teeth|z)$",
    r"^[±+\-~<>≤≥=(]?[ØøR⌀∅]?[\d.,]+(?:[xX×*][ØøR⌀]?[\d.,]+)+[a-zA-Z°º\"']{0,3}\)?$",
    r"^[±+\-~<>≤≥=]?[\d.,:;°º'\"+\-±()]+$",
    r"^[A-Za-z][\-=]?\d+[.,]\d+$",                      # X45.858, R1.310, Ra=6.3
    r"(?i)^r[az]=?[\d.,]+", r"(?i)^[a-z]{1,2}=[\d.,]+",
    # threads and fasteners
    r"(?i)^\d*-?m\d+(?:[.,]\d+)?(?:[x×*]\d+(?:[.,]\d+)?)*(?:-\d[a-z]\d?[a-z]?)?[a-z]{0,4}$",
    r"(?i)^#?\d+-\d+\s?(?:unc|unf|unef)?(?:-\d[ab])?$", r"(?i)^g\d", r"(?i)^(?:un[cf]|unef|nptf?|bsp[pt]?)-?\d[ab]?$",
    # alloys, tempers, plastics, steels
    r"^\d{4}-?[TtHhOo]\d{0,4}$", r"^(?:\d{3,5}(?:\.\d)?-)?\d{4}-T\d+$",
    r"(?i)^(?:aw|aa|aisi|ss|sae|sus|a2|a4|en|al|alu|st|ck|ht|q|a)-?\d{2,4}[a-z]{0,3}$",
    r"^[SECX]\d{2,3}[A-Z]{0,3}\d?(?:\+[A-Z0-9]+)?$",
    r"^AlMg|^AlSi|^AlCu|^AlZn|^CuZn|^CuSn|^X\d+Cr|^\d{2}[A-Z][a-z][A-Z][a-z]?\d|Cr\d+Ni\d|ERGAL",
    r"(?i)^(?:pa|pp|pe|pc|pom|pla|abs|petg?|ptfe|peek|pvc|pmma|tpu|fr|hdpe|ldpe|uhmw|nbr|epdm|fkm|fpm|pu)-?[a-z]?\d+",
    r"(?i)^(?:304|316|303|430|416|420|440|17-4|15-5|6061|7075|2024|5052|5083|6082|6063|1018|1045|4140|4340|8620)[a-z\-]{0,4}\d{0,3}$",
    # general tolerances and standards written without their prefix ("2768-mK", "Y14.5M-1994", "8015-E")
    r"^\d{3,5}(?:[.\-]\d{1,2})?-[A-Za-z]{1,2}$", r"^Y14\.", r"(?i)^mil-", r"^\d{3,5}(?:\.\d)?-\d{4}$",
    # dates
    r"^\d{1,2}[-/.](?:\d{1,2}|[A-Za-z]{3,9})[-/.]\d{2,4}$", r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$", r"^D:\d{8}", r"^\d{1,2}:\d{2}",
    r"(?i)^(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[-.,]?\d{2,4}$", r"\d{2}\.\d{2}\.(?:19|20)\d{2}",
    r"^(?:19|20)\d{2}$|^(?:19|20)\d{6}$", r"^\d+-\d+[A-Za-z]{1,3}$", r"(?i)^\d{1,4}-v\d+$",
    # labels with a counter, versions, grid references, fits
    r"(?i)^(?:scale|sheet|page|rev|size|qty|item|pos|fig|table|tab|note|sec|section|detail|view|step|no|nr|ver|version|v|r|"
    r"layer|option|type|class|grade|level|phase|stage|mk|mod|var|variant|case|test|run|lot|batch)[.:\-_ ]?\d+(?:[.:of\-]+\d+)*[a-z]?$",
    r"(?i)^v\d+(?:[._]\d+)*[a-z]?$", r"^[A-Z]\d$|^[A-Z]{1,2}\d{1,2}$", r"(?i)^[a-z]\d{1,2}-[a-z]\d{1,2}$",
    r"(?i)^[hgfjknpmde]s?\d{1,2}(?:/[hgfjknpmde]s?\d{1,2})?$", r"(?i)^it\d+$", r"(?i)^n\d{1,2}$",
    # electronics values, footprints, interfaces
    r"(?i)^\d{1,4}(?:[.,]\d+)?(?:k|m|r|kb|mb|gb|x|d|v|a|w|f|h|nf|uf|pf|mh|uh|ohm)(?:/\w+)?$", r"^0[2468]0[1-9]$|^1206$|^2512$",
    r"(?i)^(?:2d|3d|4d|5d|6dof|3dof|\dx)$",
    r"(?i)^(?:utf-?8|iso-?8859.*|cp1252|win32|x64|x86|amd64|arm64|usb\d?|rs-?232|rs-?485|i2c|spi|gpio\d*|ipv[46]|"
    r"h\.?26[45]|mp[34]|wifi|ws2812b?|esp32\w*|esp8266\w*|stm32\w*|atmega\w*|attiny\w*|rj45|pt100|pt1000|cat\d[a-e]?|ar\d+|"
    r"hrc\d+|mk\d|co2|h2o|n2|o2|h2|sf6)$",
    # designators of board-layout exports (pads, nets, components)
    r"^(?:PI|CO|PA|NL|PO)[A-Z]{1,5}\d+[A-Z0-9]*$", r"(?i)^conn_\d", r"^P\d\.\d+",
)]
_DATE = re.compile(r"^(?:\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})$")
_STD_WORDS = set("iso din en ansi asme astm uni jis bs mil sae iec nf gb gost aisi unc unf unef npt bsp ipc ul csa vde nema awg".split())
_SLASHED = re.compile(r"^[A-Za-z]{1,3}\d{2,5}(?:/[0-9A-Za-z.,]+){1,5}$")
_GROUPED = re.compile(r"^[A-Z0-9]+(?:[-_][A-Z0-9]+){2,}$")
_FEATURE = re.compile(r"^([A-Za-z]+?)(?:[-_ ]?\d+)+(?:_i\d+)?$")   # Sketch023, Dimension004, Link044_i10
_SHAPE_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_.+#]*[A-Za-z0-9]$")
_EXT = re.compile(r"\.([A-Za-z][A-Za-z0-9_]{1,8})$")


def split_ext(word):
    """Length of a trailing file extension (".STEP", ".slddrw") or 0."""
    m = _EXT.search(word)
    if m and ("." + m.group(1).lower()) in CODE_EXTS:
        return len(m.group(0))
    return 0


def generic_code(t):
    """True for a token shaped like a part / drawing / document / order number."""
    if len(t) < 5 or len(t) > 40 or "\\" in t or "@" in t:
        return False
    if "/" in t:
        # catalogue numbers of mould and tooling suppliers ("Z1055/1/24x56/8")
        return bool(_SLASHED.match(t))
    d = sum(c.isdigit() for c in t)
    if d == 0:
        return False
    a = sum(c.isalpha() for c in t)
    if not _SHAPE_OK.match(t) or not t.isascii():
        return False
    if _STD.match(t):
        return False
    if a == 0:
        # digit groups: an order or drawing number ("827-9999-903", "2020-048-10"), or one long number
        if _DATE.match(t):
            return False
        if re.match(r"^\d+(?:-\d+){2,}$", t) and d >= 7:
            return True
        return bool(re.match(r"^\d{7,12}$", t)) and not re.match(r"^(?:19|20)\d{6}$", t)
    for r in _REJECT:
        if r.search(t):
            return False
    m = _FEATURE.match(t)
    if m and len(m.group(1)) >= 4 and sum(c.isdigit() for c in t) <= 4 and is_word(m.group(1).lower()):
        return False                       # an ordinary word with a counter
    if a >= 2 and d >= 3:
        return True
    if a >= 1 and d >= 4:
        return True
    # project-prefixed codes in several groups ("OF-PP-SM2", "5BHMBA-DA-D-Z4")
    if a >= 3 and len(t) >= 8 and _GROUPED.match(t):
        groups = re.split(r"[-_]", t)
        if any(g.lower() in _STD_WORDS or (g.isalpha() and len(g) >= 3 and is_word(g.lower())) for g in groups):
            return False
        return not all(re.match(r"^M\d+$", g) for g in groups)
    return False


TOKEN = re.compile(r"[^\s\x00]{5,48}")
STRIP = ".,;:()[]{}\"'<>|=*!?\u2018\u2019\u201c\u201d"

_VENDOR_NAMES = """
mcmaster-carr mcmaster misumi thorlabs newport edmund-optics skf igus bosch-rexroth rexroth bosch festo hiwin thk nsk
schaeffler timken maxon faulhaber nanotec oriental-motor omron keyence smc parker swagelok digikey digi-key mouser farnell
rs-components newark grainger fastenal openbuilds makerbeam e3d-online e3d bondtech pololu sparkfun adafruit stepperonline
leadshine trinamic murata molex amphenol harting phoenix-contact wago weidmuller schneider-electric allen-bradley
beckhoff mitsubishi fanuc kuka yaskawa universal-robots sandvik kennametal mitutoyo starrett renishaw zeiss hamamatsu
basler keysight tektronix loctite henkel dupont nord-lock helicoil heli-coil penn-engineering southco carr-lane jergens
destaco smalley lee-spring  sdp-si stock-drive-products boston-gear nachi koyo kaydon barden  pbc-linear
bishop-wisecarver velmex zaber physik-instrumente aerotech applied-motion anaheim-automation lin-engineering portescap
pittman dunkermotoren harmonic-drive nabtesco neugart wittenstein misumi-usa item24 maytec bossard wurth würth hilti
norelem ganter elesa halder kipp essentra rs-pro traco meanwell mean-well tdk-lambda vishay bourns panasonic samsung
texas-instruments analog-devices microchip stmicroelectronics infineon nxp onsemi maxim-integrated linear-technology
honeywell te-connectivity tyco-electronics jst hirose samtec wurth-elektronik kemet yageo nichicon rubycon epcos
ebm-papst sunon delta-electronics noctua nidec mabuchi johnson-electric dynamixel robotis hitec futaba savox
ultimaker prusa makerbot creality lulzbot formlabs stratasys markforged raise3d bambu duet3d bigtreetech
vention minitec 8020 80/20 bosch-rexroth tslots misumi-europa rose+krieger kanya alutec motedis dold-mechatronik
  ntn iko rbc  ewellix schneeberger rollon igus-gmbh haydon-kerk kollmorgen moog bodine baldor sew-eurodrive
lenze nord-drivesystems bonfiglioli sumitomo apex-dynamics  stober  tsubaki renold martin-sprocket optibelt
contitech habasit forbo ametek mts instron kistler hbm futek omega-engineering honeywell-sensing  balluff
pepperl+fuchs turck ifm baumer banner-engineering cognex flir allied-vision ximea optotune  newport-corporation
opto-engineering qioptiq linos melles-griot cvi  ophir  owis newmark sutter narishige  marzhauser
 eppendorf  rainin masterflex cole-parmer  upchurch valco  bio-chem takasago clippard
""".split()


def _vendor_rx():
    alts = []
    for v in sorted(set(_VENDOR_NAMES), key=len, reverse=True):
        if len(v) < 3:
            continue
        parts = [re.escape(p) for p in re.split(r"[-]", v) if p]
        alts.append(r"[\s\-]?".join(parts))
    return re.compile(r"(?i)(?<![A-Za-z0-9\xc0-\xff])(?:" + "|".join(alts) + r")(?![A-Za-z0-9\xc0-\xff])")


VENDORS = _vendor_rx()
# short supplier names that are also ordinary strings: only in capitals or with a capital
_VENDOR_CASE = set("gates item24 delta ina fag ntn iko rbc gam nook sick prior standa ametek omega hamilton idex vici "
                   "edmund thomson parker barden nachi koyo martin vogel moog samsung".split())

# family names that are units, scales, tools or standards in engineering text
EPONYMS = set("""allen phillips morse rockwell brinell vickers shore whitworth torx pozidriv belleville woodruff vernier
newton pascal kelvin celsius fahrenheit watt volt ampere ohm hertz joule coulomb tesla weber henry farad siemens young
poisson hooke euler bernoulli reynolds mohr charpy izod knoop barcol hall zener schottky darlington wheatstone
kirchhoff laplace fourier gauss bode nyquist stirling diesel otto wankel peltier seebeck venturi pitot bourdon geneva
cardan oldham acme seeger bowden kapton teflon delrin nylon viton mylar lexan plexiglas plexiglass perspex tyvek
nitronic inconel monel hastelloy invar kovar zerodur macor vespel ultem torlon rulon turcite oilite dural duralumin
cartesian gaussian boolean lambert stokes mach abbe brewster fresnel snell rayleigh raman bragg faraday kerr pockels
schmidt cassegrain newtonian galilean keplerian maxwell boltzmann planck avogadro curie becquerel rontgen roentgen
geiger doppler coriolis foucault archimedes pythagoras helmholtz hall dewar bunsen erlenmeyer petri pasteur luer
bayonet jacobs weldon morse browne sharpe brown starrett mitutoyo unified american british german french italian
spanish chinese japanese korean swiss dutch swedish danish norwegian finnish russian polish czech english metric
january february march april june july august september october november december monday tuesday wednesday thursday
friday saturday sunday""".split())

# words that put a name next to them in a title block or revision table
NAME_CONTEXT = re.compile(
    "(?i)(?:drawn|drwn|dwn|checked|chk'?d?|approved|appr?'?d?|appv'?d|designed|design|engineer|eng|author|released|"
    "reviewed|prepared|created|modified|issued|signed|dibujado|aprobado|revisado|dise\xf1ado|comprobado|gezeichnet|"
    "gepr\xfcft|gepr|gez|bearb|erstellt|dessin\xe9|v\xe9rifi\xe9|dessinateur|disegnato|controllato|approvato|"
    "dibuixat|dissenyat|comprovat|aprovat|name|by|von|par|por)[ \t.:;/\\-]*$")
LONE_NAME = re.compile(
    "(?<![A-Za-z\xc0-\xff0-9_.@/\\\\\\-])(?P<w>[A-Z\xc0-\xde][a-z\xdf-\xff]{3,}|[A-Z\xc0-\xde]{4,})(?![A-Za-z\xc0-\xff0-9_@/\\\\\\-])")
UK_POSTCODE = re.compile(
    "(?<![A-Za-z0-9])(?:(?P<town>[A-Z][a-z]{3,})[ \t,]+)?(?P<out>[A-Z]{1,2}[0-9][A-Z0-9]?)[ \t](?P<in>[0-9][A-Z]{2})(?![A-Za-z0-9])")
