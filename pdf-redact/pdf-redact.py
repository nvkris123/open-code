#!/usr/bin/env python3
"""Redact names, Social Security numbers, and free-text phrases without
using the network.

Requires PyMuPDF >= 1.24.4 and local Tesseract language data.
"""

import argparse
import base64
import contextlib
import copy
from dataclasses import dataclass, replace
from bisect import bisect_left, bisect_right
import getpass
import hashlib
import itertools
import math
import os
import re
import secrets
import shlex
import stat
import sys
import tempfile
import threading
import time

# Set only by --edit-config, which opens no PDF, so that it can hand the
# decrypted config to $EDITOR. Nothing on a redaction path ever sets it.
_EDITOR_ALLOWED = False

_SPAWN_EVENTS = frozenset(
    {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn"}
)

# Defense in depth: this program needs neither sockets nor child processes.
# The OCR engine is linked into PyMuPDF and reads local Tesseract data.
def _offline_only(event, _args):
    if event.startswith("socket."):
        raise PermissionError(f"offline mode blocked {event}")
    if event in _SPAWN_EVENTS and not _EDITOR_ALLOWED:
        raise PermissionError(f"offline mode blocked {event}")

sys.addaudithook(_offline_only)

try:
    import pymupdf
except ImportError:
    sys.exit("ERROR: install PyMuPDF 1.24.4 or newer: pip install pymupdf")

def _local_tessdata():
    candidates = (
        os.environ.get("TESSDATA_PREFIX"),
        "/opt/homebrew/share/tessdata",
        "/usr/local/share/tessdata",
        "/usr/share/tesseract-ocr/5/tessdata",
        "/usr/share/tessdata",
        r"C:\Program Files\Tesseract-OCR\tessdata",
    )
    return next((path for path in candidates if path and os.path.isdir(path)), None)

TESSDATA = _local_tessdata()
if TESSDATA:
    os.environ.setdefault("TESSDATA_PREFIX", TESSDATA)
_OCR_READY = set()

class RedactionError(RuntimeError):
    pass

@dataclass(frozen=True)
class Target:
    label: str
    pattern: re.Pattern
    literals: tuple = ()
    preview: str = ""

@dataclass(frozen=True)
class Hit:
    """One rectangle to redact, tagged with the target that produced it.

    `area` is False for content that must be found and verified but cannot
    be removed by covering an area -- a barcode font whose glyph metrics
    make its bounding box meaningless.
    """
    rectangle: pymupdf.Rect
    label: str
    area: bool = True
    source: str = "native"
    owner: tuple = ()
    matched: str = ""
    direction: tuple = (1.0, 0.0)
    origin: tuple = ()
    sources: tuple = ()

# Machine-readable copies of redacted content. A mail barcode encodes the
# delivery address; a 2-D symbol can carry the account number. Blacking out
# the printed address while leaving these intact does not de-identify a page,
# and both land in the text layer as ordinary extractable strings: 4-state
# barcodes as A/D/F/T runs, 2-D symbols drawn with a Type 3 font as one
# string of "1"/"0" per module row.
BARCODE_PATTERNS = (
    re.compile(r"(?<![A-Za-z])[ADFTadft]{20,}(?![A-Za-z])"),
    re.compile(r"(?<![01])[01]{12,}(?![01])"),
)
BARCODE_FONT_HINTS = (
    "imb", "barcode", "bar code", "code128", "code39", "3of9", "i2of5",
    "postnet", "planet", "intelligentmail", "datamatrix", "qrcode",
    "interleaved", "upca", "ean13",
)
BARCODE_LABEL = "barcode content"

# Padding added around every hit so nothing is left half-covered.
REDACTION_PAD = 1.0

def _preview(value, length=8):
    """First `length` characters of `value` for progress output, with
    internal whitespace/newlines collapsed so it stays on one line."""
    collapsed = " ".join(value.split())
    if len(collapsed) <= length:
        return collapsed
    return collapsed[:length] + "..."

# Flexible separators cover formatting and PDFs that split every digit.
SEP = r"[\s.\-/\u2010-\u2015]*"
M3 = rf"[Xx*\u2022]{SEP}[Xx*\u2022]{SEP}[Xx*\u2022]"
M2 = rf"[Xx*\u2022]{SEP}[Xx*\u2022]"

# Between-word separator shared by every flexible phrase match (names,
# free-text phrases): tolerates punctuation, line wraps splitting on
# whitespace, and PDFs that insert stray separators between words.
BETWEEN_WORDS = r"(?:[\s,.;:/\\\-\u2010-\u2015]+)"

def default_output(path):
    root, extension = os.path.splitext(path)
    return f"{root}-redacted{extension or '.pdf'}"

def _name_tokens(value):
    return tuple(re.findall(r"[^\W_]+(?:['\u2019-][^\W_]+)*", value))

def _flexible_word(word):
    """One word, tolerant of stray whitespace between its characters (some
    PDFs space out or hyphenate individual letters)."""
    return r"\s*".join(map(re.escape, word))

def _sequence_pattern(sequence):
    """One regex alternative matching `sequence`'s words in order, tolerant
    of extra whitespace within and between words."""
    return BETWEEN_WORDS.join(_flexible_word(word) for word in sequence)

# USPS Publication 28-style equivalences: each group is a set of
# interchangeable spellings for one address word. Periods after an
# abbreviation ("St.", "N.") are already handled by BETWEEN_WORDS/the
# trailing word boundary, so groups only need the bare letters.
_ADDRESS_WORD_GROUPS = (
    ("north", "n"), ("south", "s"), ("east", "e"), ("west", "w"),
    ("northeast", "ne"), ("northwest", "nw"),
    ("southeast", "se"), ("southwest", "sw"),
    ("street", "st"), ("avenue", "ave", "av"), ("boulevard", "blvd"),
    ("drive", "dr"), ("lane", "ln"), ("road", "rd"), ("court", "ct"),
    ("place", "pl"), ("circle", "cir"), ("terrace", "ter"),
    ("parkway", "pkwy"), ("highway", "hwy"), ("square", "sq"),
    ("trail", "trl"), ("plaza", "plz"), ("point", "pt"), ("ridge", "rdg"),
    ("crescent", "cres"), ("expressway", "expy"), ("freeway", "fwy"),
    ("turnpike", "tpke"), ("alley", "aly"), ("crossing", "xing"),
    ("apartment", "apt"), ("suite", "ste"), ("building", "bldg"),
    ("floor", "fl"), ("room", "rm"),
)
_ADDRESS_SYNONYMS = {
    variant: group for group in _ADDRESS_WORD_GROUPS for variant in group
}

def _address_sequence_pattern(tokens):
    """Like _sequence_pattern, but each token also accepts its address
    abbreviation/expansion equivalents (Street <-> St, North <-> N, ...)."""
    words = []
    for token in tokens:
        variants = _ADDRESS_SYNONYMS.get(token.lower(), (token,))
        words.append(
            "(?:" + "|".join(_flexible_word(variant) for variant in variants) + ")"
        )
    return BETWEEN_WORDS.join(words)

def _compile_name_target(value, number):
    tokens = _name_tokens(value)
    if not tokens:
        raise RedactionError(f"--name value #{number} has no searchable text")

    sequences = [tokens]
    if len(tokens) > 1:
        first, last = tokens[0], tokens[-1]
        sequences += [(first, last), (last, first, *tokens[1:-1]), (last, first)]
        if len(tokens) > 2:
            middle_initials = tuple(word[0] for word in tokens[1:-1])
            sequences += [
                (first, *middle_initials, last),
                (first[0], *middle_initials, last),
            ]
    sequences = tuple(dict.fromkeys(sequences))

    alternatives = []
    literals = [value]
    for sequence in sequences:
        alternatives.append(_sequence_pattern(sequence))
        literals += [" ".join(sequence), f"{sequence[0]}, {' '.join(sequence[1:])}"]

    return Target(
        f"name target #{number}",
        re.compile(rf"(?<!\w)(?:{'|'.join(alternatives)})(?!\w)", re.I),
        tuple(dict.fromkeys(filter(str.strip, literals))),
        _preview(value),
    )

def _compile_text_target(value, number):
    """Case-insensitive literal phrase match (e.g. an address) with the same
    whitespace tolerance as names, but no name-specific reordering."""
    tokens = _name_tokens(value)
    if not tokens:
        raise RedactionError(f"--text value #{number} has no searchable text")

    return Target(
        f"text target #{number}",
        re.compile(rf"(?<!\w)(?:{_sequence_pattern(tokens)})(?!\w)", re.I),
        (value, " ".join(tokens)),
        _preview(value),
    )

def _compile_address_target(value, number):
    """Like a text target, but directionals and street-suffix words also
    match their common abbreviation (Street/St, North/N, Avenue/Ave, ...).

    A mailing address is routinely typeset with its street on one line and
    its city/state/zip on another -- not just wrapped within one text
    block, where _find_hits already joins lines, but in a wholly separate
    block (e.g. a W-2's employee and employer address boxes), which no
    line-wrap join can bridge. Comma-separated pieces of the address are
    matched on their own as well as the whole thing, so a piece that lands
    alone in its own block is still found.
    """
    tokens = _name_tokens(value)
    if not tokens:
        raise RedactionError(f"--address value #{number} has no searchable text")

    # Comma pieces only widen the regex pass (matched per text run, so a
    # piece alone in its own block still finds its own, tightly-fit box);
    # they're left out of `literals` -- that fallback exists to bridge a
    # phrase split across blocks, not to re-find what a piece already
    # matches on its own, which would just double up its redaction box.
    segments = [value] + value.split(",")
    seen, alternatives = set(), []
    for segment in segments:
        segment_tokens = _name_tokens(segment)
        if not segment_tokens or segment_tokens in seen:
            continue
        seen.add(segment_tokens)
        alternatives.append(_address_sequence_pattern(segment_tokens))

    return Target(
        f"address target #{number}",
        re.compile(rf"(?<!\w)(?:{'|'.join(alternatives)})(?!\w)", re.I),
        (value, " ".join(tokens)),
        _preview(value),
    )

def _compile_ssn_target(value, number):
    digits = re.sub(r"\D", "", value)
    if len(digits) != 9:
        raise RedactionError(
            f"--ssn value #{number} must contain exactly nine digits"
        )
    full = SEP.join(map(re.escape, digits))
    last_four = SEP.join(map(re.escape, digits[-4:]))
    pattern = rf"(?:{full}|{M3}{SEP}{M2}{SEP}{last_four})"
    return Target(
        f"exact SSN target #{number}",
        re.compile(
            rf"(?<![A-Za-z0-9*\u2022]){pattern}(?![A-Za-z0-9])"
        ),
        (value, f"{digits[:3]}-{digits[3:5]}-{digits[5:]}", digits),
        f"\u2022\u2022\u2022-\u2022\u2022-{digits[-4:]}",
    )

@dataclass(frozen=True)
class Region:
    """An absolute area of the page to black out whether or not anything
    matches inside it. The escape hatch for content no matcher can reach:
    barcodes, signature images, fonts with no usable ToUnicode, and known
    slots in a recurring statement format."""
    label: str
    first: int          # 1-based and inclusive; 0 means every page
    last: int
    rectangle: pymupdf.Rect
    preview: str = ""

    def covers(self, number):
        return not self.first or self.first <= number <= self.last

_REGION = re.compile(
    r"^(all|\d+(?:\s*-\s*\d+)?)\s*:"
    r"\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)$"
)

def _compile_region(value, number):
    """Parse 'PAGE:x0,y0,x1,y1'. Coordinates are PDF points (1/72 inch)
    measured from the top-left corner of the page as displayed, with y
    increasing downward -- the same space page.rect reports."""
    match = _REGION.match(" ".join(value.split()))
    if not match:
        raise RedactionError(
            f"--region value #{number} must look like 'PAGE:x0,y0,x1,y1' "
            f"(page, or a 'first-last' range, or 'all'), got {value!r}"
        )
    pages, *edges = match.groups()
    x0, y0, x1, y1 = (float(edge) for edge in edges)
    if x1 <= x0 or y1 <= y0:
        raise RedactionError(
            f"--region value #{number} is empty: x1 must exceed x0 and y1 y0"
        )
    if pages == "all":
        first = last = 0
    else:
        bounds = [int(part) for part in pages.split("-")]
        first, last = bounds[0], bounds[-1]
        if first < 1 or last < first:
            raise RedactionError(
                f"--region value #{number} has an invalid page range: {pages!r}"
            )
    return Region(
        f"region #{number}", first, last, pymupdf.Rect(x0, y0, x1, y1),
        _preview(value, 16),
    )

# --- Encrypted config files -------------------------------------------------
#
# A config file holds the very identifiers the tool exists to remove, so it is
# the one artifact here worth encrypting. The format is deliberately small and
# fully documented in README.md: anyone holding the passphrase can recover the
# plaintext in a few lines of Python without this script.
#
#   armor:   BEGIN line, "v<N>", base64 payload wrapped at 64 columns, END line
#   payload: version(1) || salt(16) || nonce(12) || AES-256-GCM(ciphertext+tag)
#   key:     scrypt(passphrase, salt, n=2**15, r=8, p=1, dklen=32)
#   AAD:     the version byte, so a header edit cannot survive verification
#
# Base64 armor keeps the file text, so it is safe to commit, paste, and diff as
# an opaque blob, and it is obvious at a glance that it is not plaintext.

_ENC_BEGIN = "-----BEGIN PDF-REDACT ENCRYPTED CONFIG-----"
_ENC_END = "-----END PDF-REDACT ENCRYPTED CONFIG-----"
_ENC_VERSION = 1
_ENC_SUFFIX = ".enc"
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2 ** 15, 8, 1
_SALT_BYTES, _NONCE_BYTES, _KEY_BYTES = 16, 12, 32
_PASSPHRASE_ENV = "PDFREDACT_PASSPHRASE"

def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as error:
        raise RedactionError(
            "encrypted config files need the 'cryptography' package: "
            "pip install cryptography"
        ) from error
    return AESGCM

def _derive_key(passphrase, salt):
    # maxmem must clear 128 * r * n bytes or OpenSSL refuses the parameters.
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
        p=_SCRYPT_P, dklen=_KEY_BYTES, maxmem=256 * 1024 * 1024,
    )

def is_encrypted_config(text):
    return text.lstrip().startswith(_ENC_BEGIN)

def encrypt_config(plaintext, passphrase):
    """Armor `plaintext` (a str) into the encrypted config format."""
    if not passphrase:
        raise RedactionError("refusing to encrypt with an empty passphrase")
    salt = secrets.token_bytes(_SALT_BYTES)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    version = bytes([_ENC_VERSION])
    sealed = _aesgcm()(_derive_key(passphrase, salt)).encrypt(
        nonce, plaintext.encode("utf-8"), version
    )
    payload = base64.b64encode(version + salt + nonce + sealed).decode("ascii")
    lines = [_ENC_BEGIN, f"v{_ENC_VERSION}"]
    lines += [payload[at:at + 64] for at in range(0, len(payload), 64)]
    lines.append(_ENC_END)
    return "\n".join(lines) + "\n"

def decrypt_config(armored, passphrase, path="<config>"):
    """Recover the plaintext of an armored config, or raise RedactionError."""
    lines = [line.strip() for line in armored.strip().splitlines()]
    lines = [line for line in lines if line]
    if len(lines) < 3 or lines[0] != _ENC_BEGIN or lines[-1] != _ENC_END:
        raise RedactionError(f"{path}: malformed encrypted config (bad armor)")
    if not re.fullmatch(r"v(\d+)", lines[1]):
        raise RedactionError(f"{path}: malformed encrypted config (no version)")
    version = int(lines[1][1:])
    if version != _ENC_VERSION:
        raise RedactionError(
            f"{path}: encrypted config is version {version}, "
            f"this build reads version {_ENC_VERSION}"
        )
    try:
        payload = base64.b64decode("".join(lines[2:-1]), validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise RedactionError(f"{path}: malformed encrypted config: {error}") from error

    head = 1 + _SALT_BYTES + _NONCE_BYTES
    if len(payload) <= head:
        raise RedactionError(f"{path}: malformed encrypted config (truncated)")
    if payload[0] != version:
        raise RedactionError(f"{path}: encrypted config header is inconsistent")
    salt = payload[1:1 + _SALT_BYTES]
    nonce = payload[1 + _SALT_BYTES:head]
    try:
        plaintext = _aesgcm()(_derive_key(passphrase, salt)).decrypt(
            nonce, payload[head:], payload[:1]
        )
    except Exception as error:            # InvalidTag, and anything below it
        raise RedactionError(
            f"{path}: wrong passphrase, or the file has been altered"
        ) from error
    try:
        return plaintext.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RedactionError(f"{path}: decrypted config is not UTF-8") from error

def config_passphrase(path, confirm=False):
    """The passphrase for `path`, from the environment or an interactive
    prompt. Never echoed, and never passed on the command line, where it
    would land in shell history and in `ps` output."""
    from_env = os.environ.get(_PASSPHRASE_ENV)
    if from_env:
        return from_env
    if not sys.stdin.isatty():
        raise RedactionError(
            f"need a passphrase for {path} but stdin is not a terminal; "
            f"set {_PASSPHRASE_ENV}"
        )
    name = os.path.basename(path)
    passphrase = getpass.getpass(f"Passphrase for {name}: ")
    if not passphrase:
        raise RedactionError("empty passphrase")
    if confirm and passphrase != getpass.getpass("Confirm passphrase: "):
        raise RedactionError("passphrases did not match")
    return passphrase

# Compatibility builders can read a config independently. Remember decrypted
# text so they never prompt twice; the normal immutable batch plan merges and
# compiles every config exactly once before writing any output.
_DECRYPTED = {}

def read_config_text(path):
    """The plaintext of a config file, decrypting it first if it is armored."""
    if not os.path.isfile(path):
        raise RedactionError(f"no such config file: {path}")
    key = os.path.realpath(path)
    if key in _DECRYPTED:
        return _DECRYPTED[key]
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    if not is_encrypted_config(text):
        return text
    _DECRYPTED[key] = decrypt_config(text, config_passphrase(path), path)
    return _DECRYPTED[key]

def _write_private(path, text):
    """Write `text` to `path` readable only by its owner, without ever
    leaving a world-readable window between create and chmod."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
    except BaseException:
        os.unlink(path)
        raise
    os.chmod(path, 0o600)
    return directory

# One target per non-blank, non-comment "key: value" line; keys match the
# CLI flags that take a redaction target.
_CONFIG_KEYS = ("name", "ssn", "text", "address", "regex", "region")

def _load_config(path):
    """Parse a --config file into {key: [values]}. Encrypted configs are
    decrypted in memory first; the plaintext never reaches the disk."""
    entries = {key: [] for key in _CONFIG_KEYS}
    for number, raw_line in enumerate(read_config_text(path).splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if not sep or key not in entries or not value:
            raise RedactionError(
                f"{path}:{number}: expected "
                f"'{'|'.join(_CONFIG_KEYS)}: value', got {line!r}"
            )
        entries[key].append(value)
    return entries

def _merge_values(args):
    # --config entries come first, so any target also given directly on the
    # command line (e.g. one you forgot to add to the file) is appended
    # after, but otherwise behaves identically.
    values = {key: [] for key in _CONFIG_KEYS}
    for config_path in args.config:
        for key, entries in _load_config(config_path).items():
            values[key].extend(entries)
    for key in _CONFIG_KEYS:
        values[key] += list(getattr(args, key, None) or [])
    return values

def build_regions(args):
    return [
        _compile_region(value, number)
        for number, value in enumerate(_merge_values(args)["region"], 1)
    ]

def build_targets(args):
    return _targets_from_values(_merge_values(args))

def _targets_from_values(values):

    targets = []
    for number, value in enumerate(values["regex"], 1):
        try:
            targets.append(
                Target(f"custom regex #{number}", re.compile(value), preview=_preview(value))
            )
        except re.error as error:
            raise RedactionError(f"invalid --regex #{number}: {error}") from error
    targets += [
        _compile_ssn_target(value, number)
        for number, value in enumerate(values["ssn"], 1)
    ]
    targets += [
        _compile_name_target(value, number)
        for number, value in enumerate(values["name"], 1)
    ]
    targets += [
        _compile_text_target(value, number)
        for number, value in enumerate(values["text"], 1)
    ]
    targets += [
        _compile_address_target(value, number)
        for number, value in enumerate(values["address"], 1)
    ]
    return targets

@dataclass(frozen=True)
class TargetPlan:
    targets: tuple
    regions: tuple
    literals: tuple


def _literal_queries(targets):
    queries = {}
    for target in targets:
        for literal in target.literals:
            if target not in queries.setdefault(literal, []):
                queries[literal].append(target)
    return tuple((literal, tuple(labels)) for literal, labels in queries.items())


def _compile_plan(args):
    values = _merge_values(args)
    targets = tuple(_targets_from_values(values))
    regions = []
    for number, value in enumerate(values["region"], 1):
        region = _compile_region(value, number)
        regions.append(replace(region, rectangle=tuple(region.rectangle)))
    regions = tuple(regions)
    return TargetPlan(targets, regions, _literal_queries(targets))


def _native_bounds(page):
    """Unrotated coordinates used by extraction and PDF redactions."""
    return page.rect * page.derotation_matrix


def _region_box(page, region):
    displayed = pymupdf.Rect(region.rectangle) & page.rect
    if displayed.is_empty:
        return pymupdf.Rect()
    return (displayed * page.derotation_matrix) & _native_bounds(page)


def _bbox_union(boxes):
    boxes = list(boxes)
    return (min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes))


def _overlaps(left, right):
    return (min(left[2], right[2]) > max(left[0], right[0])
            and min(left[3], right[3]) > max(left[1], right[1]))


class _SpatialIndex:
    """Grid candidate lookup with bounded storage for oversized rectangles."""
    def __init__(self, boxes, cell=32.0):
        self.boxes = tuple(tuple(box) for box in boxes)
        self.cell, self.buckets, self.overflow = cell, {}, []
        for number, box in enumerate(self.boxes):
            cells = self._cells(box)
            if cells is None:
                self.overflow.append(number)
            else:
                for key in cells:
                    self.buckets.setdefault(key, []).append(number)

    def _cells(self, box):
        if not all(math.isfinite(value) for value in box):
            return None
        x0, y0, x1, y1 = (math.floor(value / self.cell) for value in box)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > 256:
            return None
        return itertools.product(range(x0, x1 + 1), range(y0, y1 + 1))

    def query(self, rectangle):
        box = tuple(rectangle)
        cells = self._cells(box)
        if cells is None:
            candidates = range(len(self.boxes))
        else:
            candidates = set(self.overflow)
            for key in cells:
                candidates.update(self.buckets.get(key, ()))
        return [number for number in sorted(candidates)
                if _overlaps(self.boxes[number], box)]


class PageAnalysis:
    """Lazy text-only extraction for one representation of one page stage.

    Discard this object before any PDF mutation. OCR representations get their
    own instance; output verification constructs fresh instances.
    """
    def __init__(self, page, textpage=None):
        self.page = page
        self.textpage = textpage if textpage is not None else page.get_textpage(
            flags=pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
        self._cache = {}

    def _get(self, kind, extract):
        if kind not in self._cache:
            self._cache[kind] = extract()
        return self._cache[kind]

    @property
    def words(self):
        return self._get("words", self.textpage.extractWORDS)

    @property
    def text(self):
        return self._get("text", self.textpage.extractText)

    @property
    def dict(self):
        return self._get("dict", self.textpage.extractDICT)

    @property
    def rawdict(self):
        return self._get("rawdict", self.textpage.extractRAWDICT)

    @property
    def blocks(self):
        return self._get("blocks", self.textpage.extractBLOCKS)


def _text_lines(content):
    for block in content.get("blocks", ()):
        if block.get("type", 0) == 0:
            yield from block.get("lines", ())


def _text_spans(content):
    for line in _text_lines(content):
        yield from line.get("spans", ())


def _line_box(words):
    return pymupdf.Rect(_bbox_union(word[:4] for word in words))

def _wrap_adjacent(previous, current):
    """Whether `current` reads as the continuation of `previous`.

    Lines of one paragraph touch or nearly touch: they overlap across the
    text flow and are at most a line apart along it. Tightly set lines
    already overlap before any padding, so a small negative gap still counts.
    """
    vertical = (
        previous.height > 2 * previous.width and current.height > 2 * current.width
    )
    if vertical:                     # text down a margin stacks left to right
        overlap = min(previous.y1, current.y1) - max(previous.y0, current.y0)
        gap = current.x0 - previous.x1
        limit = max(previous.width, current.width)
    else:
        overlap = min(previous.x1, current.x1) - max(previous.x0, current.x0)
        gap = current.y0 - previous.y1
        limit = max(previous.height, current.height)
    return overlap > 0 and -limit < gap <= limit

def _wrap_runs(lines):
    """A block's lines split into runs a phrase may wrap across.

    A shared block is not by itself proof of nearness -- MuPDF will put text
    from opposite corners of a page in one block -- and joining distant lines
    invents matches that black out untargeted content.
    """
    runs = []
    for number in sorted(lines):
        box = _line_box(lines[number])
        if runs and _wrap_adjacent(runs[-1][-1][1], box):
            runs[-1].append((number, box))
        else:
            runs.append([(number, box)])
    return [[number for number, _box in run] for run in runs]

def _normalised(text):
    return " ".join(text.split()).casefold()

def _run_hits(run, targets, source, directions, origins=None, dehyphenate=False):
    origins = origins or {}
    pieces, spans, length = [], [], 0
    for line_number, (identity, words) in enumerate(run):
        for position, word in enumerate(sorted(words, key=lambda item: item[7])):
            value = word[4]
            separator = "\n" if position == len(words) - 1 else " "
            if (dehyphenate and position == len(words) - 1 and line_number + 1 < len(run)
                    and len(value) > 1 and value[-1] in ("-", "\u00ad") and value[-2].isalpha()
                    and min(run[line_number + 1][1], key=lambda item: item[7])[4][0].isalpha()):
                value, separator = value[:-1], ""
            pieces.append(value)
            spans.append((length, length + len(value), identity, tuple(word[:4])))
            pieces.append(separator)
            length += len(value) + len(separator)
    text = "".join(pieces)
    starts, ends = [item[0] for item in spans], [item[1] for item in spans]
    hits = []
    for target in targets:
        for match in target.pattern.finditer(text):
            boxes = {}
            first, last = bisect_right(ends, match.start()), bisect_left(starts, match.end())
            for _start, _end, identity, box in spans[first:last]:
                boxes.setdefault(identity, []).append(box)
            for identity, word_boxes in boxes.items():
                hits.append(Hit(pymupdf.Rect(_bbox_union(word_boxes)), target.label,
                                source=source, owner=identity,
                                matched=_normalised(match.group()),
                                direction=directions.get(identity, (1.0, 0.0)),
                                origin=origins.get(identity, ())))
    if not dehyphenate and any(word[4].endswith(("-", "\u00ad")) for _identity, words in run for word in words):
        hits += _run_hits(run, [target for target in targets if target.literals],
                          source, directions, origins, dehyphenate=True)
    return hits


def _find_hits(page, targets, textpage=None, analysis=None, source="native", queries=None):
    """Regex validates every occurrence, including literal search candidates.

    Full source lines retain boundary context. Literal rectangles only identify
    nearby cross-block line paths; they never independently authorize deletion.
    """
    analysis = analysis or PageAnalysis(page, textpage)
    blocks, line_words, directions, origins = {}, {}, {}, {}
    for word in analysis.words:
        identity = (word[5], word[6])
        blocks.setdefault(word[5], {}).setdefault(word[6], []).append(word)
        line_words.setdefault(identity, []).append(word)
    for block in analysis.dict.get("blocks", ()):
        for number, line in enumerate(block.get("lines", ())):
            identity = (block.get("number", 0), number)
            directions[identity] = tuple(line.get("dir", (1.0, 0.0)))
            spans = line.get("spans", ())
            if spans and spans[0].get("origin"):
                origins[identity] = tuple(spans[0]["origin"])
    hits = []
    for block_number, lines in blocks.items():
        for run in _wrap_runs(lines):
            hits += _run_hits([((block_number, number), lines[number]) for number in run],
                              targets, source, directions, origins)

    identities = list(line_words)
    line_boxes = [_line_box(line_words[identity]) for identity in identities]
    index = _SpatialIndex(line_boxes)
    # A hyphenated continuation across separate nearby blocks needs the same
    # full-line boundary validation, even when the literal search TextPage
    # preserves its original hyphen.
    for number, identity in enumerate(identities):
        final_word = max(line_words[identity], key=lambda item: item[7])[4]
        if not final_word.endswith(("-", "\u00ad")):
            continue
        box = line_boxes[number]
        window = (box.x0, box.y0, box.x1, box.y1 + box.height)
        for candidate in index.query(window):
            other = identities[candidate]
            if other[0] != identity[0] and _wrap_adjacent(box, line_boxes[candidate]):
                hits += _run_hits([(identity, line_words[identity]), (other, line_words[other])],
                                  [target for target in targets if target.literals],
                                  source, directions, origins)
    for literal, validators in (queries if queries is not None else _literal_queries(targets)):
        try:
            rectangles = page.search_for(literal, textpage=analysis.textpage)
        except ValueError:
            # Standalone OCR TextPages may belong to an in-memory image page.
            rectangles = analysis.textpage.search(literal, quads=False)
        selected = sorted({candidate for rect in rectangles for candidate in index.query(rect)})
        # Search order represents occurrence order; require actual geometrical
        # continuation and validate unclipped context for each connected run.
        groups = []
        for candidate in selected:
            if groups and _wrap_adjacent(line_boxes[groups[-1][-1]], line_boxes[candidate]):
                groups[-1].append(candidate)
            else:
                groups.append([candidate])
        for group in groups:
            if len({identities[item][0] for item in group}) < 2:
                continue
            candidate_hits = _run_hits([(identities[item], line_words[identities[item]])
                                        for item in group], validators, source, directions, origins)
            hits += [hit for hit in candidate_hits
                     if any(_overlaps(hit.rectangle, rect) for rect in rectangles)]
    return _unique_hits(hits, 1)

def _unique_hits(hits, digits=0):
    """Drop duplicates: the regex and literal passes, and the native and OCR
    passes, often find the same rectangle."""
    unique = {}
    for hit in hits:
        key = (hit.label, *(round(value, digits) for value in hit.rectangle))
        unique[key] = hit
    return list(unique.values())

def _sane_box(box, page):
    """True when `box` is a plausible piece of page geometry.

    Barcode and other synthetic fonts routinely report nonsense glyph
    metrics: an Intelligent Mail span on a real utility bill claims a
    bounding box over 10,000 points above the top of the page. Boxes like
    that must never drive layout decisions or be redacted wholesale.
    """
    bounds = _native_bounds(page)
    return (not box.is_empty and not box.is_infinite
            and box.y0 >= bounds.y0 - 1 and box.x0 >= bounds.x0 - 1
            and box.y1 <= bounds.y1 + 1 and box.x1 <= bounds.x1 + 1)

def _is_barcode_span(span):
    text = span.get("text", "")
    font = span.get("font", "").casefold()
    return any(hint in font for hint in BARCODE_FONT_HINTS) or any(
        pattern.search(text) for pattern in BARCODE_PATTERNS
    )

def _barcode_hits(page, textpage=None, analysis=None):
    """Spans holding a machine-readable copy of page content: 4-state mail
    barcodes, 2-D symbol module rows, and anything set in a barcode font.

    Spans whose glyph metrics are unusable are reported but left for
    _blank_barcode_text(); apply_redactions() cannot reach them.
    """
    hits = []
    content = (analysis or PageAnalysis(page, textpage)).dict
    for span in _text_spans(content):
        if not _is_barcode_span(span):
            continue
        box = pymupdf.Rect(span["bbox"])
        hits.append(Hit(box if _sane_box(box, page) else box & _native_bounds(page),
                        BARCODE_LABEL, area=_sane_box(box, page)))
    return _unique_hits(hits, 1)

def _barcode_font_names(page, analysis=None):
    """Resource names of fonts drawing barcodes with unusable glyph metrics.

    apply_redactions() deletes a character only when the redaction rectangle
    contains that character's own bounding box. A font claiming a box
    thousands of points tall therefore cannot be redacted by area without
    taking a whole column of the page with it, so its show-text operators
    are removed from the content stream instead.
    """
    wanted = set()
    for span in _text_spans((analysis or PageAnalysis(page)).dict):
        if _is_barcode_span(span) and not _sane_box(pymupdf.Rect(span["bbox"]), page):
            wanted.add(span.get("font", "").casefold())
    if not wanted:
        return frozenset()
    names = set()
    for font in page.get_fonts(full=True):
        basefont, refname = font[3], font[4]
        # Subset fonts are named like "ABCDEF+IMB".
        stem = basefont.split("+")[-1].casefold()
        if stem in wanted or any(stem.startswith(name) for name in wanted):
            names.add("/" + refname)
    return frozenset(names)

_WHITESPACE = b" \t\r\n\f\x00"
_DELIMITERS = b" \t\r\n\f\x00/[]()<>{}%"

def _content_tokens(data):
    """Yield (kind, start, end) for each content-stream token.

    Written out rather than pattern-matched because a literal string may
    contain unbalanced-looking bytes behind a backslash escape, and mistaking
    those for operators would corrupt the page.
    """
    index, size = 0, len(data)
    while index < size:
        char = data[index:index + 1]
        if char in _WHITESPACE:
            index += 1
        elif char == b"%":
            end = data.find(b"\n", index)
            index = size if end < 0 else end + 1
        elif char == b"(":
            depth, cursor = 1, index + 1
            while cursor < size and depth:
                current = data[cursor:cursor + 1]
                if current == b"\\":
                    cursor += 2
                    continue
                depth += (current == b"(") - (current == b")")
                cursor += 1
            yield "string", index, cursor
            index = cursor
        elif char == b"<":
            if data[index:index + 2] == b"<<":
                index += 2
                continue
            end = data.find(b">", index)
            end = size if end < 0 else end + 1
            yield "string", index, end
            index = end
        elif char == b">":
            index += 2 if data[index:index + 2] == b">>" else 1
        elif char in b"[]":
            yield ("array" if char == b"[" else "arrayend"), index, index + 1
            index += 1
        elif char in b"{}":
            index += 1
        else:
            cursor = index + (1 if char == b"/" else 0)
            while cursor < size and data[cursor:cursor + 1] not in _DELIMITERS:
                cursor += 1
            cursor = max(cursor, index + 1)
            yield ("name" if char == b"/" else "token"), index, cursor
            index = cursor

def _blank_barcode_text(page, names):
    """Empty the show-text operands drawn in any font in `names`.

    Returns the number of operators emptied.
    """
    if not names:
        return 0
    page.clean_contents()
    xrefs = page.get_contents()
    if len(xrefs) != 1:
        return 0
    document = page.parent
    data = document.xref_stream(xrefs[0])
    if not data:
        return 0

    edits = []
    font, name, string, array = None, None, None, None
    for kind, start, end in _content_tokens(data):
        if kind == "name":
            name = data[start:end].decode("latin-1")
        elif kind == "string":
            string = (start, end)
        elif kind == "array":
            array = start
        elif kind == "arrayend" and array is not None:
            array = (array, end)
        elif kind == "token":
            operator = data[start:end]
            if operator == b"Tf":
                font = name
            elif font in names:
                if operator in (b"Tj", b"'", b'"') and string:
                    edits.append((*string, b"()"))
                elif operator == b"TJ" and isinstance(array, tuple):
                    edits.append((*array, b"[]"))
            if operator in (b"Tj", b"'", b'"', b"TJ"):
                string, array = None, None

    if not edits:
        return 0
    out, cursor = [], 0
    for start, end, replacement in sorted(edits):
        out.append(data[cursor:start])
        out.append(replacement)
        cursor = end
    out.append(data[cursor:])
    document.update_stream(xrefs[0], b"".join(out))
    return len(edits)

def _summarize_hits(count, page_count):
    if not count:
        return "not found"
    redactions = "redaction" if count == 1 else "redactions"
    pages = "page" if page_count == 1 else "pages"
    return f"{count} {redactions} across {page_count} {pages}"

def _progress(stage, current, total):
    """Update one terminal line; stay silent when output is redirected."""
    if not sys.stderr.isatty():
        return
    width = 28
    ratio = current / total if total else 1
    filled = round(width * ratio)
    bar = "#" * filled + "-" * (width - filled)
    sys.stderr.write(
        f"\r{stage:<10} [{bar}] {current}/{total} {ratio:>6.1%}"
    )
    sys.stderr.flush()

def _clear_progress():
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * 72 + "\r")
        sys.stderr.flush()

@contextlib.contextmanager
def _silence_native_stderr():
    """Swallow OCR's direct-to-fd stderr writes (e.g. "Image too small to
    scale!!", "Line cannot be recognized!!").

    Tesseract and Leptonica print these straight to the process's stderr
    file descriptor from C, never through MuPDF's own error/warning hooks,
    so mupdf_display_errors/mupdf_display_warnings can't reach them.
    Redirecting the fd is the only way to stop them; nothing of ours writes
    to stderr while this is open (no progress update runs during an OCR
    call), and the real fd is restored before this returns.
    """
    stderr_fd = sys.stderr.fileno()
    saved_fd = os.dup(stderr_fd)
    try:
        with open(os.devnull, "w") as devnull:
            sys.stderr.flush()
            os.dup2(devnull.fileno(), stderr_fd)
        yield
    finally:
        sys.stderr.flush()
        os.dup2(saved_fd, stderr_fd)
        os.close(saved_fd)

class _Spinner:
    """Indeterminate progress for calls with no per-item hook, e.g.
    PyMuPDF's scrub() and save(): a spinner plus elapsed time so the
    terminal keeps moving instead of sitting silent."""

    _FRAMES = "|/-\\"

    def __init__(self, stage):
        self._stage = stage
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        if sys.stderr.isatty():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_exc_info):
        self._stop.set()
        if self._thread:
            self._thread.join()
            _clear_progress()
        return False

    def _run(self):
        start = time.monotonic()
        for frame in itertools.cycle(self._FRAMES):
            if self._stop.is_set():
                return
            elapsed = time.monotonic() - start
            sys.stderr.write(f"\r{self._stage:<10} {frame} {elapsed:6.1f}s")
            sys.stderr.flush()
            self._stop.wait(0.15)

def _ocr(page, args):
    if not TESSDATA:
        raise RedactionError(
            "local Tesseract data not found; set TESSDATA_PREFIX"
        )
    if args.ocr_language not in _OCR_READY:
        missing = [
            language
            for language in args.ocr_language.split("+")
            if not os.path.isfile(os.path.join(TESSDATA, f"{language}.traineddata"))
        ]
        if missing:
            raise RedactionError(f"local OCR language data not found: {missing}")
        _OCR_READY.add(args.ocr_language)
    silence = contextlib.nullcontext() if args.debug else _silence_native_stderr()
    try:
        with silence:
            return page.get_textpage_ocr(
                language=args.ocr_language,
                dpi=args.ocr_dpi,
                full=True,
                tessdata=TESSDATA,
            )
    except Exception as error:
        raise RedactionError(
            f"OCR failed on page {page.number + 1}: {error}. "
            "No output was published"
        ) from error

def _reconcile_hits(native, ocr):
    """Use independently matched native geometry only for unique local pairs."""
    pairs = {}
    native_index = _SpatialIndex([hit.rectangle for hit in native])
    for number, hit in enumerate(ocr):
        candidates = []
        for index in native_index.query(hit.rectangle):
            other = native[index]
            if (hit.label != other.label or hit.matched != other.matched
                    or any(abs(a - b) > .01 for a, b in zip(hit.direction, other.direction))):
                continue
            left, right = hit.rectangle, other.rectangle
            vertical = abs(hit.direction[1]) > abs(hit.direction[0])
            if vertical:
                overlap = min(left.y1, right.y1) - max(left.y0, right.y0)
                aligned = (abs(hit.origin[0] - other.origin[0]) <= max(2, right.width)
                           if hit.origin and other.origin
                           else abs(left.x1 - right.x1) <= max(2, right.width))
            else:
                overlap = min(left.x1, right.x1) - max(left.x0, right.x0)
                aligned = (abs(hit.origin[1] - other.origin[1]) <= max(2, right.height)
                           if hit.origin and other.origin
                           else abs(left.y1 - right.y1) <= max(2, right.height))
            if overlap > 0 and aligned and _overlaps(left, right):
                candidates.append(index)
        if len(candidates) == 1:
            pairs[number] = candidates[0]
    counts = {}
    for index in pairs.values():
        counts[index] = counts.get(index, 0) + 1
    reconciled = {index for index in pairs.values() if counts[index] == 1}
    native = [replace(hit, sources=("native", "ocr")) if index in reconciled else hit
              for index, hit in enumerate(native)]
    return _unique_hits(native + [hit for number, hit in enumerate(ocr)
                                 if number not in pairs or counts[pairs[number]] != 1])


def _scan(page, targets, args, use_ocr=True, check_readability=False, analysis=None, queries=None):
    native = analysis or PageAnalysis(page)
    native_hits = _find_hits(page, targets, analysis=native, queries=queries) if targets else []
    if args.text_only or not use_ocr:
        return native_hits, False, False
    need_readability = check_readability and not native.text.strip()
    if not targets and not need_readability:
        return native_hits, False, False
    if need_readability:
        pixmap = page.get_pixmap(dpi=36, colorspace=pymupdf.csGRAY, alpha=False)
        samples = pixmap.samples
        nonblank = bool(samples and sum(value < 245 for value in samples) / len(samples) > .0002)
        if not targets and not nonblank:
            return native_hits, False, False
    ocr = PageAnalysis(page, _ocr(page, args))
    ocr_hits = _find_hits(page, targets, analysis=ocr, source="ocr", queries=queries) if targets else []
    unreadable = need_readability and nonblank and not ocr.text.strip()
    return _reconcile_hits(native_hits, ocr_hits), bool(ocr_hits), bool(unreadable)


class _LineBands(list):
    def __init__(self):
        super().__init__()
        self.owners = {}


def _line_bands(page, pad=REDACTION_PAD, analysis=None):
    """Bound line padding locally; diagonal watermarks cannot own form lines."""
    analysis = analysis or PageAnalysis(page)
    lines = []
    for block in analysis.dict.get("blocks", ()):
        for number, line in enumerate(block.get("lines", ())):
            box = pymupdf.Rect(line["bbox"])
            direction = line.get("dir", (1.0, 0.0))
            if not _sane_box(box, page) or (abs(direction[0]) > .01 and abs(direction[1]) > .01):
                continue
            lines.append((box, abs(direction[1]) > abs(direction[0]),
                          (block.get("number", 0), number)))
    index = _SpatialIndex([box for box, _vertical, _identity in lines])
    bands = _LineBands()
    for number, (box, vertical, identity) in enumerate(lines):
        limits = [box.x0 - pad, box.y0 - pad, box.x1 + pad, box.y1 + pad]
        for candidate in index.query(limits):
            other, other_vertical, _ = lines[candidate]
            if candidate == number or other_vertical != vertical:
                continue
            if vertical:
                if other.y1 <= box.y0 or other.y0 >= box.y1:
                    continue
                if other.x0 < box.x0 and other.x1 <= box.x1:
                    limits[0] = max(limits[0], other.x1)
                if other.x0 >= box.x0 and other.x1 > box.x1:
                    limits[2] = min(limits[2], other.x0)
            else:
                if other.x1 <= box.x0 or other.x0 >= box.x1:
                    continue
                if other.y0 < box.y0 and other.y1 <= box.y1:
                    limits[1] = max(limits[1], other.y1)
                if other.y0 >= box.y0 and other.y1 > box.y1:
                    limits[3] = min(limits[3], other.y0)
        band = pymupdf.Rect(*limits)
        bands.append((box, band))
        bands.owners[identity] = band
    return bands


def _redaction_box(page, rectangle, bands, hit=None):
    grown = pymupdf.Rect(rectangle.x0 - REDACTION_PAD, rectangle.y0 - REDACTION_PAD,
                         rectangle.x1 + REDACTION_PAD, rectangle.y1 + REDACTION_PAD)
    band = (getattr(bands, "owners", {}).get(hit.owner)
            if hit is not None and hit.owner and hit.source == "native" else None)
    if band is None and (hit is None or not hit.owner) and (hit is None or hit.source == "native"):
        owners = [(box, band) for box, band in bands if _overlaps(box, rectangle)]
        if owners:
            band = max(owners, key=lambda owner: (owner[0] & rectangle).get_area())[1]
    if band is not None:
        clamped = grown & band
        if not clamped.is_empty:
            grown = clamped
    return grown & _native_bounds(page)

@dataclass(frozen=True)
class _Glyph:
    c: str
    origin: tuple
    bbox: tuple
    font: str
    size: float
    direction: tuple

@dataclass(frozen=True)
class _NativeWord:
    text: str
    bbox: tuple
    glyph_indices: tuple

def _text_snapshot(page, include_words=True, analysis=None):
    """Native characters and words, joined across spans within each line."""
    glyphs, words = [], []
    for block in (analysis or PageAnalysis(page)).rawdict.get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            indices = []

            def flush_word():
                if not indices or not include_words:
                    return
                box = _bbox_union(glyphs[index].bbox for index in indices)
                words.append(_NativeWord(
                    "".join(glyphs[index].c for index in indices),
                    tuple(box), tuple(indices),
                ))
                indices.clear()

            for span in line.get("spans", ()):
                for char in span.get("chars", ()):
                    if char["c"].isspace():
                        flush_word()
                        continue
                    if include_words:
                        indices.append(len(glyphs))
                    glyphs.append(_Glyph(
                        char["c"], tuple(char["origin"]), tuple(char["bbox"]),
                        span["font"], span["size"], tuple(line["dir"]),
                    ))
            flush_word()
    return glyphs, words

# Native text rewriting can round coordinates slightly. This is far below
# ordinary glyph spacing; writing direction uses a dimensionless tolerance.
_GLYPH_GEOMETRY_TOLERANCE = 0.01
_GLYPH_DIRECTION_TOLERANCE = 1e-5

def _glyph_distance(before, after):
    """Normalized distance, or None when records cannot be the same glyph."""
    if before.c != after.c or before.font != after.font:
        return None
    distance = 0.0
    for left, right, tolerance in (
        (before.origin, after.origin, _GLYPH_GEOMETRY_TOLERANCE),
        (before.bbox, after.bbox, _GLYPH_GEOMETRY_TOLERANCE),
        ((before.size,), (after.size,), _GLYPH_GEOMETRY_TOLERANCE),
        (before.direction, after.direction, _GLYPH_DIRECTION_TOLERANCE),
    ):
        for original, survivor in zip(left, right):
            delta = abs(original - survivor)
            if delta > tolerance:
                return None
            distance += (delta / tolerance) ** 2
    return distance

def _glyph_survival(before, after):
    """Match original glyphs one-to-one, independent of extraction order."""
    survivors = [False] * len(before)
    available = set(range(len(after)))
    exact = {}
    for index, glyph in enumerate(after):
        exact.setdefault(glyph, []).append(index)
    # Reserve all exact identities globally before a nearby removed glyph
    # can borrow a surviving neighbour during the tolerant fallback.
    for index, glyph in enumerate(before):
        matches = exact.get(glyph)
        if matches:
            available.remove(matches.pop())
            survivors[index] = True

    def bucket(glyph):
        return tuple(math.floor(value / _GLYPH_GEOMETRY_TOLERANCE)
                     for value in glyph.origin)

    buckets = {}
    for index in sorted(available):
        glyph = after[index]
        x, y = bucket(glyph)
        buckets.setdefault((glyph.c, glyph.font, x, y), []).append(index)
    for index, glyph in enumerate(before):
        if survivors[index]:
            continue
        x, y = bucket(glyph)
        best = None
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for candidate in buckets.get((glyph.c, glyph.font, x + dx, y + dy), ()):
                    if candidate not in available:
                        continue
                    distance = _glyph_distance(glyph, after[candidate])
                    if distance is not None:
                        choice = (distance, candidate)
                        if best is None or choice < best:
                            best = choice
        if best is not None:
            available.remove(best[1])
            survivors[index] = True
    return survivors

def _fragments(page, boxes, before):
    """Words a redaction box only partly covered whose remainder survived.

    A leftover piece of an identifier is still identifying -- a ZIP+4 clipped
    down to its last two digits reveals the same delivery point. This runs
    after apply_redactions() and compares against the words seen before it.
    """
    glyphs, words = before
    after, _words = _text_snapshot(page, include_words=False)
    box_index = _SpatialIndex(boxes)
    survivors = _glyph_survival(glyphs, after)
    findings = []
    for word in words:
        box = pymupdf.Rect(word.bbox)
        if not _sane_box(box, page):
            continue      # a font with unusable metrics, not a real fragment
        if not box_index.query(word.bbox):
            continue                               # nothing touched this word
        count = sum(survivors[index] for index in word.glyph_indices)
        if not 0 < count < len(word.glyph_indices):
            continue                               # fully deleted or unchanged
        runs, current = [], []
        for index in word.glyph_indices:
            if survivors[index]:
                current.append(glyphs[index].c)
            elif current:
                runs.append("".join(current))
                current = []
        if current:
            runs.append("".join(current))
        findings.append((word.text, " ".join(runs)))
    return findings

def _redact_page(page, hits, region_boxes=(), analysis=None):
    # PyMuPDF paints redaction fills with a Shape whose transformation matrix
    # omits a shifted CropBox origin on rotated pages. All our geometry is
    # already unrotated: applying at rotation zero preserves both deletion
    # and the actual black fill. Restore the display rotation on every path.
    rotation = page.rotation
    try:
        if rotation:
            page.set_rotation(0)
        return _redact_page_unrotated(page, hits, region_boxes, analysis)
    finally:
        if rotation:
            page.set_rotation(rotation)


def _redact_page_unrotated(page, hits, region_boxes=(), analysis=None):
    """Apply `hits` and `region_boxes` to `page`; return any partly-covered
    word fragments. Regions are used exactly as given -- the operator chose
    those coordinates, so they are neither padded nor clamped."""
    analysis = analysis or PageAnalysis(page)
    barcode_fonts = (
        _barcode_font_names(page, analysis)
        if any(hit.label == BARCODE_LABEL for hit in hits)
        else frozenset()
    )
    if not hits and not region_boxes:
        return []
    bands = _line_bands(page, analysis=analysis) if hits else []
    boxes = []
    for hit in hits:
        if not hit.area:
            continue
        box = _redaction_box(page, hit.rectangle, bands, hit)
        if not box.is_empty:
            boxes.append(box)
    boxes += [box for box in region_boxes if not box.is_empty]
    fragments = []
    if boxes:
        boxes = list({tuple(box): box for box in boxes}.values())
        for box in boxes:
            page.add_redact_annot(box, fill=(0, 0, 0), cross_out=False)
        before = _text_snapshot(page, analysis=analysis)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
            graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
        fragments = _fragments(page, boxes, before)
    _blank_barcode_text(page, barcode_fonts)
    return fragments

def _pdf_references(value):
    """Indirect references in a PDF dictionary, excluding strings/comments."""
    data = value.encode("latin-1", errors="replace")
    previous = []
    references = []
    for kind, start, end in _content_tokens(data):
        token = data[start:end]
        if (kind == "token" and token == b"R" and len(previous) == 2
                and all(re.fullmatch(rb"\d+", item) for item in previous)):
            references.append(int(previous[0]))
        previous.append(token if kind == "token" else b"")
        previous = previous[-2:]
    return references


def _source_images(doc):
    """Images reachable from the catalog/page resources, including Patterns/Type3 fonts.

    Enumerating displayed images omits clipped or unpainted Pattern/CharProc
    resources that remain recoverable. Follow dictionaries rather than paint
    operations, but never scan unreferenced objects dropped by the final save.
    """
    pending, streams = [doc.pdf_catalog()], set()
    pending.extend(_pdf_references(doc.xref_get_key(-1, "Info")[1]))
    for page in doc:
        streams.update(page.get_contents())
        pending.extend(page.get_contents())
        ancestor, visited = page.xref, set()
        while ancestor and ancestor not in visited:
            visited.add(ancestor)
            kind, resources = doc.xref_get_key(ancestor, "Resources")
            if kind != "null":
                pending.extend(_pdf_references(resources))
                break
            kind, parent = doc.xref_get_key(ancestor, "Parent")
            ancestor = int(parent.split()[0]) if kind == "xref" else 0
    reached, images = set(), set()
    while pending:
        xref = pending.pop()
        if not xref or xref in reached:
            continue
        reached.add(xref)
        try:
            dictionary = doc.xref_object(xref)
            pending.extend(_pdf_references(dictionary))
            subtype = doc.xref_get_key(xref, "Subtype")[1]
            if subtype == "/Image" or xref in images:
                images.add(xref)
                for key in ("SMask", "Mask"):
                    kind, value = doc.xref_get_key(xref, key)
                    if kind == "xref":
                        mask = int(value.split()[0])
                        images.add(mask)
                        pending.append(mask)
            elif subtype == "/Form" or doc.xref_get_key(xref, "PatternType")[1] == "1":
                streams.add(xref)
            elif subtype == "/Type3":
                kind, charprocs = doc.xref_get_key(xref, "CharProcs")
                if kind == "xref":
                    charprocs = doc.xref_object(int(charprocs.split()[0]))
                streams.update(_pdf_references(charprocs))
        except Exception as error:
            raise RedactionError(f"cannot inventory retained PDF resource {xref}: {error}") from error
    for stream in streams:
        data = doc.xref_stream(stream) or b""
        if any(kind == "token" and data[start:end] == b"BI"
               for kind, start, end in _content_tokens(data)):
            raise RedactionError("complete-source verification of inline images is unsupported; "
                                 "no output was published")
    return sorted(images)


def _image_hits(doc, xref, targets, args, queries=None):
    """OCR decoded full source pixels, independent of every page placement."""
    try:
        pixmap = pymupdf.Pixmap(doc, xref)
        if pixmap.alpha and pixmap.colorspace is not None:
            # JPX can keep an identifying alpha channel inside its color
            # stream, with no separately enumerable SMask. Dropping alpha
            # would silently verify only the color samples. Refuse this
            # format until independent alpha sanitation is implemented.
            raise RedactionError(f"source image {xref}: embedded alpha without a separate "
                                 "mask is unsupported; no output was published")
        if pixmap.colorspace is None:
            decoded = pymupdf.Pixmap(pymupdf.csGRAY, pixmap.width, pixmap.height, pixmap.samples, False)
        else:
            decoded = pixmap
        ocr_pixmap = decoded
        if decoded.colorspace.n != 3:
            ocr_pixmap = pymupdf.Pixmap(pymupdf.csRGB, decoded)
        if ocr_pixmap.alpha:
            ocr_pixmap = pymupdf.Pixmap(ocr_pixmap, 0)
        ocr_pixmap.set_dpi(args.ocr_dpi, args.ocr_dpi)
        silence = contextlib.nullcontext() if args.debug else _silence_native_stderr()
        with silence:
            data = ocr_pixmap.pdfocr_tobytes(language=args.ocr_language, tessdata=TESSDATA)
        with pymupdf.open(stream=data, filetype="pdf") as image_doc:
            page = image_doc[0]
            hits = _find_hits(page, targets, queries=queries)
            scale = pymupdf.Matrix(pixmap.width / page.rect.width,
                                   pixmap.height / page.rect.height)
            pixel_boxes = [hit.rectangle * scale for hit in hits]
        return pixmap, pixel_boxes
    except RedactionError:
        raise
    except Exception as error:
        raise RedactionError(f"cannot verify complete source image {xref}: {error}; "
                             "no output was published") from error


def _replace_source_image(doc, xref, pixmap):
    """Reencode decoded device samples at the original shared image xref.

    Public dictionary/stream operations avoid a painted donor or encoder
    palette optimization, which would violate a grayscale mask's semantics.
    """
    keep = {}
    for key in ("SMask", "Mask", "Interpolate", "Matte", "Intent", "OC"):
        kind, value = doc.xref_get_key(xref, key)
        if kind != "null":
            keep[key] = value
    if doc.xref_get_key(xref, "ImageMask")[1] == "true":
        raise RedactionError(f"source image {xref}: image-mask replacement is unsupported; "
                             "no output was published")
    if pixmap.colorspace is None or pixmap.alpha:
        raise RedactionError(f"source image {xref}: unsupported decoded pixel format")
    kind, original_space = doc.xref_get_key(xref, "ColorSpace")
    encoded_space = original_space
    if kind == "xref":
        original_space = doc.xref_object(int(original_space.split()[0])).strip()
    device_space = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(pixmap.n)
    icc = re.fullmatch(r"\[\s*/ICCBased\s+(\d+)\s+0\s+R\s*\]", original_space)
    supported_icc = icc is not None and doc.xref_get_key(int(icc.group(1)), "N")[1] == str(pixmap.n)
    if device_space is None or (original_space != device_space and not supported_icc):
        raise RedactionError(f"source image {xref}: unsupported color-space replacement")
    replacement_space = encoded_space if supported_icc else device_space
    if doc.xref_get_key(xref, "Mask")[0] == "array":
        # Color-key ranges refer to encoded samples. They remain valid only
        # for unchanged 8-bit device samples with the standard decode mapping.
        decode = doc.xref_get_key(xref, "Decode")[1]
        expected = "[" + " ".join(["0 1"] * pixmap.n) + "]"
        if doc.xref_get_key(xref, "BitsPerComponent")[1] != "8" or decode not in ("null", expected):
            raise RedactionError(f"source image {xref}: unsupported color-key mask encoding")
    doc.update_object(xref, f"<</Type/XObject/Subtype/Image/Width {pixmap.width} "
                      f"/Height {pixmap.height}/BitsPerComponent 8/ColorSpace {replacement_space}>>")
    doc.update_stream(xref, pixmap.samples, compress=True)
    for key, value in keep.items():
        doc.xref_set_key(xref, key, value)


def _sanitize_source_images(doc, targets, args, queries=None):
    if args.text_only or not targets:
        return 0
    count = 0
    for xref in _source_images(doc):
        pixmap, boxes = _image_hits(doc, xref, targets, args, queries)
        if not boxes:
            continue
        count += len(boxes)
        if args.dry_run:
            continue
        for box in boxes:
            # Conservative rounding covers every source pixel touched by OCR.
            rectangle = pymupdf.IRect(math.floor(box.x0) - 1, math.floor(box.y0) - 1,
                                     math.ceil(box.x1) + 1, math.ceil(box.y1) + 1) & pixmap.irect
            black = (0, 0, 0, 255) if pixmap.colorspace is not None and pixmap.colorspace.n == 4 else (0,) * pixmap.n
            pixmap.set_rect(rectangle, black)
        _replace_source_image(doc, xref, pixmap)
    return count


def _verify_source_images(doc, targets, args, queries=None):
    if args.text_only or not targets:
        return []
    failures = []
    # Saving with garbage=4 already dropped unreachable objects. Enumerate
    # every retained image independently of the pre-save resource graph as a
    # second backstop, while that graph still supplies mask and inline checks.
    images = set(_source_images(doc))
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Subtype")[1] == "/Image":
            images.add(xref)
    for xref in sorted(images):
        _pixmap, boxes = _image_hits(doc, xref, targets, args, queries)
        if boxes:
            failures.append(f"source image {xref} still matches a redaction target")
    return failures


def _scrub(doc):
    doc.set_toc([])
    doc.set_page_labels([])
    # scrub() defaults remove attachments, hidden data, scripts, links, and
    # metadata. Leave images alone here: matched hits already had their image
    # pixels redacted in _redact_page(), including hits inside hidden text.
    # This pass only strips leftover invisible text that matched nothing, and
    # blanking images wherever unrelated hidden text happens to sit is
    # collateral damage, not redaction.
    doc.scrub(
        redact_images=pymupdf.PDF_REDACT_IMAGE_NONE,
        javascript=False,
        xml_metadata=False,
    )
    _scrub_objects(doc)
    for page in doc:
        for annotation in list(page.annots() or []):
            page.delete_annot(annotation)
    _strip_private_data(doc)

def _scrub_objects(doc):
    """scrub()'s JavaScript and XML-metadata pass, minus its abort on an
    unreadable object. Some generators leave a gap in the xref table, an
    object number inside /Size that no subsection lists and nothing
    references, and scrub() raises on it. An object MuPDF cannot load holds
    nothing it can write, so the garbage-collecting save drops it anyway."""
    doc.del_xml_metadata()
    for xref in range(1, doc.xref_length()):
        try:
            doc.xref_object(xref)
        except RuntimeError:
            continue
        if doc.xref_get_key(xref, "S")[1] == "/JavaScript":
            doc.update_object(xref, "<</S/JavaScript/JS()>>")
        elif doc.xref_get_key(xref, "Type")[1] == "/Metadata":
            doc.update_object(xref, "<<>>")
            doc.update_stream(xref, b"deleted", new=True)
        elif doc.xref_get_key(xref, "Metadata")[0] != "null":
            doc.xref_set_key(xref, "Metadata", "null")

# scrub() does not touch these. /PieceInfo is the private scratch space
# page-composition software writes into a page dictionary, and on real
# statements it carries the print stream's batch, control and sequence
# numbers; /AF names associated files; /LastModified dates the composition.
_PRIVATE_KEYS = ("PieceInfo", "AF", "LastModified")

def _private_data(doc, page_xrefs=None):
    """(xref, key) pairs of private application data still in `doc`."""
    remaining = []
    for xref in [doc.pdf_catalog()] + (page_xrefs if page_xrefs is not None else [page.xref for page in doc]):
        for key in _PRIVATE_KEYS:
            try:
                if doc.xref_get_key(xref, key)[0] != "null":
                    remaining.append((xref, key))
            except Exception:
                continue
    return remaining

def _strip_private_data(doc):
    for xref, key in _private_data(doc):
        try:
            doc.xref_set_key(xref, key, "null")
        except Exception:
            continue

# Residual-risk scan: patterns that identify a person or an account even
# though no target named them. Reported rather than failed, because plenty
# of documents legitimately keep them -- and because on a statement most of
# them belong to the sender, not to the person being protected.
_RESIDUAL_CHECKS = (
    # Labelled first: it wins the dedup, so a value printed beside "Meter
    # Number" is reported under that name rather than as a bare digit run.
    (
        "identifier beside a label",
        re.compile(
            r"(?i)\b(?P<label>(?:account|meter|customer|policy|member|choice"
            r"|invoice|reference|subscriber)(?:\s+(?:number|no\.?|#|id))?)"
            r"\b[^\n\d]{0,24}(?P<value>\d[\d\s-]{5,})"
        ),
    ),
    ("ZIP+4", re.compile(r"(?P<value>\b\d{5}-\d{4}\b)")),
    ("long digit run", re.compile(r"(?P<value>(?<!\d)\d{9,}(?!\d))")),
)

# Words that introduce a value on a printed form, used to name a finding the
# checks above caught without a label of their own.
_LABEL_NEARBY = re.compile(
    r"(?i)\b(?:account|meter|customer|policy|member|choice|invoice|reference"
    r"|subscriber|confirmation|tracking|service)(?:\s+(?:number|no\.?|#|id))?\b"
)

# A block carrying one of these is the sender's own: the remit-to address on
# a bill, not the addressee's. Its ZIP and phone are the utility's, and
# reporting them every run buries the findings that are actually the reader's.
_SENDER_MARKERS = re.compile(
    r"(?i)\b(?:p\.?\s?o\.?\s+box|remit(?:tance)?|make\s+checks?\s+payable"
    r"|payable\s+to|return\s+service\s+requested"
    r"|address\s+service\s+requested)\b"
)

@dataclass(frozen=True)
class Finding:
    page: int
    label: str
    value: str = ""
    sender: bool = False        # sits in the sender's own address block

    def __str__(self):
        if not self.value:
            return f"page {self.page}: {self.label}"
        return f"page {self.page}: {self.label}  {_preview(self.value, 28)}"

def _label_before(text, position):
    """The label printed beside a value, if there is one: the last label word
    in the run of text leading up to it."""
    window = text[max(0, position - 120):position]
    labels = list(_LABEL_NEARBY.finditer(window))
    return " ".join(labels[-1].group(0).split()) if labels else ""

def _label_above(words, rectangle, index=None):
    """Nearest aligned header, with cached numeric boxes and local candidates."""
    box = tuple(rectangle)
    height = box[3] - box[1]
    candidates = []
    if index is None:
        index = _SpatialIndex([word[:4] for word in words])
    window = (box[0], box[1] - 3 * height, box[2], box[1] + 1.000001)
    for number in index.query(window):
        word = words[number]
        if word[3] <= box[1] + 1 and box[1] - word[3] < 3 * height:
            candidates.append((tuple(word[:4]), word[4]))
    if not candidates:
        return ""
    baseline = max(candidate[3] for candidate, _text in candidates)
    header = " ".join(text for candidate, text in sorted(candidates, key=lambda pair: pair[0][0])
                      if abs(candidate[3] - baseline) < 2)
    return header if _LABEL_NEARBY.search(header) else ""


def _residual_page(page, scope="auto", analysis=None):
    if scope == "none":
        return []
    analysis = analysis or PageAnalysis(page)
    number, findings, seen = page.number + 1, [], set()
    if _barcode_hits(page, analysis=analysis):
        findings.append(Finding(number, "barcode content"))
    words = analysis.words
    word_index = _SpatialIndex([word[:4] for word in words])
    digit_boxes = {}
    for word in words:
        digits = re.sub(r"\D", "", word[4])
        if digits:
            digit_boxes.setdefault(digits, []).append(tuple(word[:4]))
    for block in analysis.blocks:
        if len(block) > 6 and block[6] != 0:
            continue
        text = block[4]
        sender = bool(_SENDER_MARKERS.search(text))
        labels = list(_LABEL_NEARBY.finditer(text))
        label_ends = [label.end() for label in labels]
        for description, pattern in _RESIDUAL_CHECKS:
            for match in pattern.finditer(text):
                value = " ".join(match.group("value").split())
                digits = re.sub(r"\D", "", value)
                if not digits or digits in seen:
                    continue
                seen.add(digits)
                label = match.groupdict().get("label")
                if not label:
                    previous = bisect_right(label_ends, match.start()) - 1
                    if previous >= 0 and labels[previous].start() >= max(0, match.start() - 120):
                        label = labels[previous].group()
                if not label and digits in digit_boxes:
                    label = _label_above(words, digit_boxes[digits][0], word_index)
                findings.append(Finding(number, " ".join(label.split()) if label else description,
                                        value, sender and not label))
    return findings


def _residual_risks(doc, scope="auto"):
    return [finding for page in doc for finding in _residual_page(page, scope)] if scope != "none" else []

def _reportable(findings, scope):
    """The findings a run actually shows: everything under --pii-check all,
    otherwise all but the sender's own."""
    return [
        finding for finding in findings
        if scope == "all" or not finding.sender
    ]

def verify(path, targets, args, ocr_pages=None, barcode_pages=(), regions=(), queries=None):
    """Fresh rendered and complete-source verification after every mutation.

    Initial OCR hit pages are diagnostics only, never verification coverage.
    """
    failures, warnings = [], []
    barcode_pages = set(barcode_pages)
    queries = _literal_queries(targets) if queries is None else queries
    doc = pymupdf.open(path)
    widgets = annotations = links = 0
    page_xrefs = []
    try:
        for page in doc:
            analysis = PageAnalysis(page)
            page_xrefs.append(page.xref)
            hits, _ocr_hit, _ = _scan(page, targets, args, analysis=analysis, queries=queries)
            if hits:
                labels = ", ".join(sorted({hit.label for hit in hits}))
                failures.append(f"page {page.number + 1} still matches {labels}")
            if page.number in barcode_pages and _barcode_hits(page, analysis=analysis):
                failures.append(f"page {page.number + 1} still has barcode content")
            for region in regions:
                if not region.covers(page.number + 1):
                    continue
                box = _region_box(page, region)
                if not box.is_empty and page.get_text("text", clip=box).strip():
                    failures.append(f"page {page.number + 1} still has text inside {region.label}")
            widgets += sum(1 for _ in page.widgets())
            annotations += sum(1 for _ in (page.annots() or []))
            links += len(page.get_links())
            warnings += _residual_page(page, getattr(args, "pii_check", "auto"), analysis)
            _progress("Verifying", page.number + 1, doc.page_count)
        if _private_data(doc, page_xrefs):
            failures.append("private application data remains")
        if widgets:
            failures.append("form fields remain")
        if annotations:
            failures.append("annotations remain")
        if links:
            failures.append("links remain")
        if doc.embfile_names():
            failures.append("embedded files remain")
        if doc.get_toc():
            failures.append("bookmarks remain")
        metadata = {key: value for key, value in (doc.metadata or {}).items()
                    if value and key not in ("format", "encryption")}
        if metadata or doc.get_xml_metadata():
            failures.append("metadata remains")
        failures += _verify_source_images(doc, targets, args, queries)
    finally:
        _clear_progress()
        doc.close()
    return failures, warnings

def _xfa(doc):
    try:
        return doc.xref_get_key(
            doc.pdf_catalog(), "AcroForm/XFA"
        )[0] != "null"
    except Exception:
        return False

def _temp_path(output):
    directory = os.path.dirname(os.path.abspath(output))
    if not os.path.isdir(directory):
        raise RedactionError(f"output directory does not exist: {directory}")
    # No leading dot: a dot-prefixed name sits on disk (while doc.save()
    # writes it) long enough for macOS to flag it UF_HIDDEN, and that flag
    # survives the rename to the final, non-dot output path.
    descriptor, path = tempfile.mkstemp(
        prefix=f"{os.path.basename(output)}.", suffix=".tmp.pdf", dir=directory
    )
    os.close(descriptor)
    os.unlink(path)
    return path

def _delete(path):
    if path and os.path.exists(path):
        try:
            os.unlink(path)
        except OSError:
            pass

def _clear_hidden_flag(path):
    """Keep a dot-prefixed temporary file from publishing as hidden on macOS."""
    if not hasattr(os, "chflags") or not hasattr(stat, "UF_HIDDEN"):
        return
    flags = os.stat(path).st_flags
    if flags & stat.UF_HIDDEN:
        os.chflags(path, flags & ~stat.UF_HIDDEN)

@dataclass
class Run:
    """What one pass over the document found."""
    counts: dict
    hit_pages: dict
    unreadable: list
    image_pages: list
    ocr_pages: set
    barcode_pages: set
    fragments: list

def _process_pages(doc, targets, regions, args, queries=None):
    """Scan every page, apply redactions, and return run diagnostics."""
    labels = [target.label for target in targets]
    labels += [region.label for region in regions] + [BARCODE_LABEL]
    counts = {label: 0 for label in labels}
    hit_pages = {label: set() for label in labels}
    unreadable, image_pages = [], []
    ocr_pages, barcode_pages, fragments = set(), set(), []
    stage = "Scanning" if args.dry_run else "Redacting"
    try:
        for page in doc:
            number = page.number + 1
            if args.text_only and page.get_image_info():
                image_pages.append(number)
            analysis = PageAnalysis(page)
            hits, ocr_hit, failed_ocr = _scan(
                page, targets, args, check_readability=True, analysis=analysis, queries=queries
            )
            if ocr_hit:
                ocr_pages.add(page.number)
            if failed_ocr:
                unreadable.append(number)

            region_boxes = []
            for region in regions:
                if not region.covers(number):
                    continue
                box = _region_box(page, region)
                if box.is_empty:
                    continue
                region_boxes.append(box)
                counts[region.label] += 1
                hit_pages[region.label].add(number)

            # "auto" redacts barcodes only where something else matched, on
            # the principle that a barcode beside a redaction encodes what
            # was just removed. Elsewhere it is ordinary page furniture.
            if args.barcodes == "all" or (
                args.barcodes == "auto" and (hits or region_boxes)
            ):
                found = _barcode_hits(page, analysis=analysis)
                if found:
                    barcode_pages.add(page.number)
                    counts[BARCODE_LABEL] += len(found)
                    hit_pages[BARCODE_LABEL].add(number)
                    hits = hits + found

            for hit in hits:
                if hit.label == BARCODE_LABEL:
                    continue
                counts[hit.label] += 1
                hit_pages[hit.label].add(number)
            if not args.dry_run:
                fragments += [
                    (number, original, remainder)
                    for original, remainder in _redact_page(
                        page, hits, region_boxes, analysis=analysis
                    )
                ]
            _progress(stage, number, doc.page_count)
    finally:
        _clear_progress()
    return Run(
        counts, hit_pages, unreadable, image_pages,
        ocr_pages, barcode_pages, fragments,
    )

def _report_findings(findings, scope):
    """Ask about what survived. These are not errors -- a statement is
    entitled to carry an invoice number -- so the run has already written the
    file, and the reader is the only one who can say which of these matter.
    """
    shown = _reportable(findings, scope)
    withheld = len(findings) - len(shown)
    if not shown and not withheld:
        return
    print(
        "Still found in the output. Are you sure none of these is yours?",
        file=sys.stderr,
    )
    for finding in shown[:10]:
        print(f"  {finding}", file=sys.stderr)
    if len(shown) > 10:
        print(f"  ...and {len(shown) - 10} more", file=sys.stderr)
    if withheld:
        # Named, not silently dropped: the rule that calls a block the
        # sender's is a guess, and a wrong guess hides the reader's own data.
        print(
            f"  {withheld} in what looks like the sender's own address block, "
            "not listed -- --pii-check all to include",
            file=sys.stderr,
        )
    print(
        "  Redact one by adding a target for it; pass --pii-check none to "
        "stop asking.",
        file=sys.stderr,
    )

def redact(args, plan=None):
    # MuPDF's own diagnostics (e.g. "cannot find XObject resource", or
    # "Image too small to scale!!" from the readability check's low-dpi
    # render) go straight to stderr from C code and don't indicate whether
    # a page's matches were found; they're noise unless someone's actually
    # debugging.
    pymupdf.TOOLS.mupdf_display_errors(args.debug)
    pymupdf.TOOLS.mupdf_display_warnings(args.debug)

    plan = plan or _compile_plan(args)
    targets, regions = plan.targets, plan.regions
    if not targets and not regions:
        raise RedactionError("nothing to redact")
    output = args.output or default_output(args.input)
    candidate = None
    try:
        doc = pymupdf.open(args.input)
    except Exception as error:
        raise RedactionError(f"cannot open input PDF: {error}") from error

    try:
        if doc.needs_pass:
            raise RedactionError("PDF is password protected; decrypt it first")
        if not doc.is_pdf:
            raise RedactionError("input is not a PDF")
        if _xfa(doc):
            raise RedactionError("print this XFA form to a new PDF before redacting")

        widgets = sum(1 for page in doc for _ in page.widgets())
        if widgets:
            if not hasattr(doc, "bake"):
                raise RedactionError("upgrade to PyMuPDF 1.24.4 or newer")
            doc.bake(annots=False, widgets=True)

        print(f"Opened {args.input} ({doc.page_count} pages)")
        run = _process_pages(doc, targets, regions, args, plan.literals)

        previews = {target.label: target.preview for target in targets}
        previews.update({region.label: region.preview for region in regions})
        for label, count in sorted(run.counts.items()):
            if label == BARCODE_LABEL and not count:
                continue        # only worth a line when something was found
            tag = f" '{previews[label]}'" if previews.get(label) else ""
            summary = _summarize_hits(count, len(run.hit_pages[label]))
            print(f"  {label}{tag}: {summary}")
        if run.fragments:
            sys.stdout.flush()      # keep warnings in step with the summary
        for number, original, remainder in run.fragments[:10]:
            print(
                f"  WARNING: page {number}: a redaction box left part of "
                f"{original!r} visible as {remainder!r}",
                file=sys.stderr,
            )
        if run.unreadable:
            raise RedactionError(
                f"OCR found no text on nonblank page(s) {run.unreadable}; "
                "no output was published"
            )
        if args.strict and run.fragments:
            raise RedactionError(
                f"{len(run.fragments)} partly-covered word(s) left a visible "
                "fragment; no output was published"
            )
        source_hits = _sanitize_source_images(doc, targets, args, plan.literals)
        if source_hits:
            action = "found" if args.dry_run else "sanitized"
            print(f"  complete source images: {source_hits} target area(s) {action}")
        if args.dry_run:
            print("Dry run complete - no file written.")
            return 0

        with _Spinner("Scrubbing"):
            _scrub(doc)
        candidate = _temp_path(output)
        try:
            with _Spinner("Saving"):
                doc.save(candidate, garbage=4, deflate=True, clean=True)
        except Exception:
            _delete(candidate)
            candidate = None
            raise
    finally:
        doc.close()

    try:
        failures, warnings = verify(
            candidate, targets, args, run.ocr_pages, run.barcode_pages, regions, queries=plan.literals
        )
        if args.strict:
            failures += [
                str(finding) for finding in _reportable(warnings, args.pii_check)
            ]
            warnings = []
        if failures:
            failure_message = "verification failed:\n  " + "\n  ".join(failures)
            if not args.keep_unverified:
                raise RedactionError(failure_message)
        os.replace(candidate, output)
        _clear_hidden_flag(output)
    except Exception:
        _delete(candidate)
        raise

    if failures:
        print(f"ERROR: {failure_message}", file=sys.stderr)
        print(f"Wrote {output} (UNVERIFIED; review the failed checks before sharing)", flush=True)
    else:
        checked = "text" if args.text_only else "text, final-page OCR, and complete-source image OCR"
        print(f"Verification passed: {checked}, forms, annotations, and metadata")
        print(f"Wrote {output}", flush=True)
    _report_findings(warnings, args.pii_check)
    if args.text_only and run.image_pages:
        pages = ", ".join(map(str, run.image_pages[:12]))
        if len(run.image_pages) > 12:
            pages += ", ..."
        print(
            "ERROR: text redaction completed, but the PDF contains images on "
            f"page(s) {pages}; complete redaction cannot be guaranteed",
            file=sys.stderr,
        )
        return 2 if failures else 3
    return 2 if failures else 0

def _is_batch_input(name):
    """A PDF that is not itself a redacted output."""
    return (
        name.lower().endswith(".pdf")
        and not os.path.splitext(name)[0].casefold().endswith("-redacted")
    )

def _batch_candidates(folder):
    """(input, output) for each top-level PDF in `folder`, sorted for a
    deterministic run order, each written beside its input."""
    names = sorted(filter(_is_batch_input, os.listdir(folder)))
    paths = [os.path.join(folder, name) for name in names]
    return [(path, default_output(path)) for path in paths]

def _tree_candidates(folder, output_root):
    """(input, output) for every PDF anywhere under `folder`, each output at
    the same relative path under `output_root`. An output tree inside
    `folder` is pruned from the walk, so a re-run never redacts its own
    previous output."""
    pruned = os.path.realpath(output_root)
    jobs = []
    for directory, subdirectories, names in os.walk(folder):
        subdirectories[:] = sorted(
            name for name in subdirectories
            if os.path.realpath(os.path.join(directory, name)) != pruned
        )
        mirror = os.path.normpath(
            os.path.join(output_root, os.path.relpath(directory, folder))
        )
        for name in sorted(filter(_is_batch_input, names)):
            jobs.append((
                os.path.join(directory, name),
                default_output(os.path.join(mirror, name)),
            ))
    return jobs

def redact_folder(args):
    """Redact every PDF directly inside args.input, or with --recursive every
    PDF in its subfolders too, mirrored under the output folder. A file whose
    output already exists is skipped unless --force, so a folder can be
    re-run after adding new statements without redoing old ones."""
    if args.recursive:
        jobs = _tree_candidates(args.input, args.output)
    else:
        jobs = _batch_candidates(args.input)
    if not jobs:
        print(f"no PDFs to redact in {args.input}")
        return 0

    plan = _compile_plan(args)
    if not plan.targets and not plan.regions:
        raise RedactionError("nothing to redact")
    processed = skipped = failed = 0
    for path, output in jobs:
        if os.path.exists(output) and not args.force:
            print(f"skipping {path}: {output} already exists (use --force to overwrite)")
            skipped += 1
            continue

        file_args = copy.copy(args)
        file_args.input = path
        file_args.output = output
        print(f"--- {path} ---")
        try:
            if not args.dry_run:
                os.makedirs(os.path.dirname(output), exist_ok=True)
            code = redact(file_args, plan)
        except Exception as error:      # one bad file must not end the batch
            print(f"ERROR: {path}: {error}", file=sys.stderr)
            failed += 1
            continue
        if code:
            failed += 1
        else:
            processed += 1

    print(f"Folder run complete: {processed} redacted, {skipped} skipped, {failed} failed")
    return 2 if failed else 0

# --- Config-management commands ---------------------------------------------

_CONFIG_COMMANDS = ("--encrypt-config", "--decrypt-config", "--edit-config")

def _target_path(source, args, encrypting):
    if args.output:
        return args.output
    if encrypting:
        return source + _ENC_SUFFIX
    if source.endswith(_ENC_SUFFIX):
        return source[:-len(_ENC_SUFFIX)]
    return source + ".plain"

def _check_output(path, args):
    if os.path.exists(path) and not args.force:
        raise RedactionError(f"{path} exists; use --force to replace it")
    return path

def _read_existing(path):
    if not os.path.isfile(path):
        raise RedactionError(f"no such config file: {path}")
    with open(path, encoding="utf-8") as handle:
        return handle.read()

def _run_editor(path):
    """Open `path` in $EDITOR and wait. This is the one place the program
    launches a child process, and it runs only when no PDF is open."""
    global _EDITOR_ALLOWED
    command = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    import subprocess
    _EDITOR_ALLOWED = True
    try:
        completed = subprocess.run(shlex.split(command) + [path], check=False)
    except OSError as error:
        raise RedactionError(f"could not start editor {command!r}: {error}") from error
    finally:
        _EDITOR_ALLOWED = False
    if completed.returncode != 0:
        raise RedactionError(
            f"editor {command!r} exited with status {completed.returncode}; "
            "leaving the encrypted config untouched"
        )

def manage_config(args):
    if args.encrypt_config:
        source = args.encrypt_config
        plaintext = _read_existing(source)
        if is_encrypted_config(plaintext):
            raise RedactionError(f"{source} is already encrypted")
        _load_config(source)          # refuse to seal a file that will not parse
        output = _check_output(_target_path(source, args, True), args)
        armored = encrypt_config(plaintext, config_passphrase(output, confirm=True))
        _write_private(output, armored)
        print(f"encrypted {source} -> {output}")
        print(f"{source} is still plaintext; remove it once you have "
              f"confirmed {output} decrypts.")
        return 0

    if args.decrypt_config:
        source = args.decrypt_config
        armored = _read_existing(source)
        if not is_encrypted_config(armored):
            raise RedactionError(f"{source} is not an encrypted config")
        plaintext = decrypt_config(armored, config_passphrase(source), source)
        output = _check_output(_target_path(source, args, False), args)
        _write_private(output, plaintext)
        print(f"decrypted {source} -> {output} (mode 0600)")
        return 0

    source = args.edit_config
    armored = _read_existing(source)
    if not is_encrypted_config(armored):
        raise RedactionError(f"{source} is not an encrypted config")
    passphrase = config_passphrase(source)
    plaintext = decrypt_config(armored, passphrase, source)
    return _edit_decrypted(source, plaintext, passphrase)

def _edit_decrypted(source, plaintext, passphrase):
    """Hand the plaintext to $EDITOR on a 0600 temp file, then reseal it.

    The temp file is the one moment this config exists in the clear on disk,
    so it is created private, kept beside the encrypted original rather than
    in a shared /tmp, and removed on every path out of here."""
    directory = os.path.dirname(os.path.abspath(source)) or "."
    handle, scratch = tempfile.mkstemp(
        dir=directory, prefix=".", suffix=".redact-edit"
    )
    os.close(handle)
    try:
        _write_private(scratch, plaintext)
        _run_editor(scratch)
        with open(scratch, encoding="utf-8") as stream:
            edited = stream.read()
        if edited == plaintext:
            print(f"{source} unchanged")
            return 0
        try:
            _load_config(scratch)     # a bad edit must not become the new config
        except RedactionError as error:
            raise RedactionError(
                f"{str(error).replace(scratch, 'edited config')}; "
                f"{source} was left unchanged"
            ) from None
        _write_private(source, encrypt_config(edited, passphrase))
    finally:
        try:
            os.unlink(scratch)
        except FileNotFoundError:
            pass
    print(f"updated {source}")
    return 0

def parse_config_args(argv):
    parser = argparse.ArgumentParser(
        prog="pdf-redact.py",
        description="Create, inspect, and edit encrypted redaction configs.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--encrypt-config", metavar="FILE",
                      help="encrypt a plaintext config to FILE.enc")
    mode.add_argument("--decrypt-config", metavar="FILE",
                      help="write the plaintext of an encrypted config")
    mode.add_argument("--edit-config", metavar="FILE",
                      help="decrypt into $EDITOR and reseal on save")
    parser.add_argument("-o", "--output-file", dest="output")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Offline name, SSN, and free-text redaction for PDFs."
    )
    parser.add_argument(
        "input",
        help="PDF to redact, or a folder to redact every top-level PDF in "
             "it (skipping any that already has a '-redacted' counterpart "
             "unless --force is given)",
    )
    parser.add_argument(
        "output", nargs="?",
        help="output PDF, or with --recursive the folder to mirror into",
    )
    parser.add_argument("-o", "--output-file")
    parser.add_argument("-n", "--name", action="append", default=[])
    parser.add_argument("-s", "--ssn", action="append", default=[])
    parser.add_argument("-t", "--text", action="append", default=[])
    parser.add_argument("-a", "--address", action="append", default=[])
    parser.add_argument("-r", "--regex", action="append", default=[])
    parser.add_argument(
        "-R", "--region", action="append", default=[], metavar="PAGE:x0,y0,x1,y1",
        help="area to black out unconditionally. Coordinates are PDF points "
             "(1/72 inch) from the top-left corner of the page, y downward. "
             "PAGE is 1-based, a 'first-last' range, or 'all'. Repeatable",
    )
    parser.add_argument(
        "-c", "--config", action="append", default=[],
        help="file of 'key: value' redaction targets, plaintext or encrypted "
             "(see --encrypt-config). Repeatable",
    )
    # Listed so they show in --help; handled by parse_config_args, which runs
    # instead of this parser because these modes take no PDF.
    config_modes = parser.add_argument_group("config management (no PDF)")
    config_modes.add_argument("--encrypt-config", metavar="FILE",
                              help="encrypt a plaintext config to FILE.enc")
    config_modes.add_argument("--decrypt-config", metavar="FILE",
                              help="write the plaintext of an encrypted config")
    config_modes.add_argument("--edit-config", metavar="FILE",
                              help="decrypt into $EDITOR and reseal on save")
    parser.add_argument(
        "--barcodes", choices=("auto", "all", "none"), default="auto",
        help="redact barcodes: on pages that had another redaction (auto, "
             "the default), everywhere (all), or never (none)",
    )
    parser.add_argument(
        "--pii-check", choices=("auto", "all", "none"), default="auto",
        help="report identifiers left in the output that no target named "
             "(auto), also report ones inside the sender's own address block "
             "(all), or skip the check (none)",
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="treat residual-risk warnings and leftover fragments as failures",
    )
    parser.add_argument("--ocr-dpi", type=int, default=300)
    parser.add_argument("--ocr-language", default="eng")
    parser.add_argument("--text-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--keep-unverified", action="store_true",
        help="write the output even when final verification reports failures; "
             "list every failed check and still exit with an error",
    )
    parser.add_argument(
        "--recursive", action="store_true",
        help="with a folder input, also redact the PDFs in every subfolder, "
             "recreating the folder structure under the output folder, which "
             "is required",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="show underlying MuPDF/OCR diagnostic messages (suppressed by default)",
    )
    args = parser.parse_args(argv)

    if args.output and args.output_file:
        parser.error("use positional output or --output-file, not both")
    args.output = args.output or args.output_file
    if not 150 <= args.ocr_dpi <= 600:
        parser.error("--ocr-dpi must be between 150 and 600")

    if os.path.isdir(args.input):
        if args.recursive:
            if not args.output:
                parser.error("--recursive needs an output folder")
            if os.path.exists(args.output) and not os.path.isdir(args.output):
                parser.error(f"{args.output} exists and is not a folder")
        elif args.output:
            parser.error(
                "a folder input redacts each file to its own default "
                "output; an explicit output path needs --recursive"
            )
        return args
    if args.recursive:
        parser.error("--recursive needs a folder input")
    if not os.path.isfile(args.input):
        parser.error(f"no such file or directory: {args.input}")

    output = args.output or default_output(args.input)
    if os.path.abspath(output) == os.path.abspath(args.input):
        parser.error("refusing to overwrite the input")
    if os.path.exists(output) and not args.force and not args.dry_run:
        parser.error(f"{output} exists; use --force to replace it")
    return args

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if any(arg.partition("=")[0] in _CONFIG_COMMANDS for arg in argv):
            return manage_config(parse_config_args(argv))
        args = parse_args(argv)
        return redact_folder(args) if os.path.isdir(args.input) else redact(args)
    except RedactionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2

if __name__ == "__main__":
    sys.exit(main())
