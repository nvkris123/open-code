# PDF Redact

Offline PDF name, SSN, and free-text redaction.

```bash
python3 pdf-redact.py return.pdf --name "Joe Victor Caffee" --ssn "123-45-6789" \
    --address "123 North Abel Street"
```

The default output is `return-redacted.pdf` in the input file's directory.

Requires PyMuPDF >= 1.24.4 and local Tesseract language data. [Encrypted config files](#encrypted-config-files) additionally need `cryptography`; nothing else does.

## Behavior

- Names are case-insensitive. `Joe Victor Caffee` also matches `Joe Caffee`, `Caffee Joe Victor`, `Caffee Joe`, `Joe V Caffee`, and `J V Caffee`, with common punctuation variations.
- SSNs are redacted only when supplied with `--ssn`. `--ssn 123456789` matches `123456789`, `123-45-6789`, `12-3456789`, and masked forms such as `XXX-XX-6789` or `xxx-xx-6789`.
- `--text` matches a case-insensitive literal phrase, tolerant of extra whitespace and line wraps, with no reordering or word substitution.
- `--address` is like `--text`, plus it also matches common USPS-style abbreviation/expansion pairs word-for-word: directionals (`North`/`N`, `Northeast`/`NE`, ...), street suffixes (`Street`/`St`, `Avenue`/`Ave`, `Boulevard`/`Blvd`, ...), and unit words (`Apartment`/`Apt`, `Suite`/`Ste`, ...). `--address "123 North Abel Street"` also matches `123 N Abel St`, `123 N. Abel St.`, etc. — whichever form you type, either spelling in the document matches.
- A target may wrap. `--name`, `--text`, and `--address` match across consecutive lines, so one value covering a whole mailing address still matches a page that sets it as several lines:
  ```
  address: 123 North Abel Street, Smallville, PA 81456
  ```
  matches a block printed as `123 North Abel Street` above `Smallville, PA 81456`. Only lines that actually sit against each other are joined, so text from elsewhere on the page is never assembled into a phrase that isn't there, and each line gets its own box — a wrapped match never blacks out the gap between its lines.
- `--address` also matches on each comma-separated piece of the value on its own, not just the whole thing — a form can print the street and the city/state/zip far enough apart (say, an employee-address box separate from an employer-address box) that they're not lines of the same wrap and the whole-phrase match above can't join them. `--address "123 North Abel Street, Smallville, PA 81456"` still finds a page that only has `123 North Abel Street` in one spot and `Smallville, PA 81456` somewhere else entirely.
- `--region` blacks out an area of the page whether or not anything matches inside it — the escape hatch for content no matcher can reach: barcodes, signature images, fonts with unusable `ToUnicode`, and known slots in a recurring statement format. See [Region coordinates](#region-coordinates). A `--region` on its own is enough to run; no other target is required.
- Barcodes are redacted automatically. A mail barcode encodes the delivery address and a 2-D symbol can carry the account number, so blacking out a printed address while leaving them intact does not de-identify the page. Recognised: USPS four-state (Intelligent Mail) runs, 2-D symbol module rows, and any span set in a barcode font. `--barcodes auto` (the default) removes them only on pages where something else was redacted, on the principle that a barcode beside a redaction encodes what was just removed; `--barcodes all` removes every one found; `--barcodes none` disables it.
- A redaction box never reaches into the line above or below its own. Word boxes are taller than the leading in tightly set documents, so padding a hit would otherwise delete part of an untargeted neighbour and leave the remainder visible — an address block redacted for its street line losing `60521-3459` down to a stray `59`. Text set vertically, such as a reference number printed down a margin, is clamped across columns instead. If a box still leaves part of a word behind, the run warns and names the fragment. These warnings can include untargeted text that a box partly removes. The check tracks that word's original characters, so an intact diagonal watermark produces no warning, and a clipped watermark's reported fragment contains only its surviving characters. Spaces separate surviving runs where characters were deleted.
- After the output is written, the run asks about identifying content that survived because no target named it — ZIP+4s, identifiers beside a label, long digit runs, and any remaining barcode:
  ```
  Still found in the output. Are you sure none of these is yours?
    page 1: long digit run  0002575361950257530
    page 2: Meter Number  272794609
    page 2: Choice ID  6564436917
    1 in what looks like the sender's own address block, not listed -- --pii-check all to include
  ```
  These are questions, not errors: the file is written either way, because plenty of documents legitimately keep an invoice number, and only you can say which of these is yours. Each finding is named by the label printed beside it — or, in a table, by its column header — and each value is asked about once however many checks recognise it. Values inside a block that looks like the sender's own address (a remit-to or PO box block) are counted but not listed, since a bill always carries the biller's ZIP; `--pii-check all` lists them anyway, and a value with a label of its own is always listed even in such a block. `--pii-check none` skips the check. `--strict` turns whatever would be listed, and any leftover fragment, into a failure that publishes nothing by default. `--keep-unverified` can retain final-verification failures, including these identifier findings; earlier fragment failures still block publication.
- `--config FILE` loads redaction targets from a file instead of (or alongside) the command line — one `key: value` per line, where `key` is `name`, `ssn`, `text`, `address`, `regex`, or `region`. Blank lines and lines starting with `#` are ignored. Repeatable, and combines with any targets also given directly on the command line:
  ```
  # family.redact
  name: Jane Q. Public
  ssn: 123-45-6789
  address: 123 North Abel Street
  region: all:480,5,590,30
  ```
  Keeping a config file also keeps PII out of your shell history and out of `ps` output while the command runs, which repeated `-n`/`-s`/`-t`/`-a` flags don't.
- Config files can be **encrypted at rest**, since they hold the very identifiers the tool exists to remove. `--config` accepts an encrypted file anywhere it accepts a plaintext one and decrypts it in memory; the plaintext is never written to disk. See [Encrypted config files](#encrypted-config-files).
- Every `--name`, `--ssn`, `--text`, `--address`, and `--regex` value is an optional match: if it isn't found anywhere in the document, the run still succeeds and the output is written — it's just reported as "not found" in the summary (see below) instead of failing the run.
- A summary line prints for every target after scanning, showing how many redactions were made and across how many pages, e.g. `name target #1: 3 redactions across 2 pages`, or `not found` when nothing matched.
- Tesseract is the local OCR engine used for image-based text. OCR defaults to English at 300 DPI; no cloud OCR is used.
- With OCR enabled, every final page is independently checked with rendered OCR when text targets are supplied. Complete embedded image and mask pixels are also checked and sanitized, including cropped, covered, shared, and nested images. Source-image findings are reported separately from visible page matches. Unsupported image replacement, embedded alpha without a separate mask, or failed OCR blocks publication.
- Text extraction excludes image bytes and is reused within each immutable page stage. Folder runs compile configuration once. Region-only native-text runs skip matching OCR, while nonblank unreadable pages retain their diagnostic.
- Output is first written to a temporary file and verified, then atomically moved to the final filename. On macOS, the temporary file's hidden flag is cleared after publishing so the PDF remains visible in Finder. Failed verification publishes nothing unless `--keep-unverified` is supplied; see [Keeping an output that fails verification](#keeping-an-output-that-fails-verification).
- Forms, hidden text, annotations, metadata, bookmarks, links, JavaScript, and attachments are removed to eliminate alternate copies of sensitive data. So is `/PieceInfo`, the private scratch space page-composition software writes into a page dictionary — on real statements it carries the print stream's batch, control and sequence numbers, and it survives an ordinary scrub.
- Python network and process-launch access is blocked while the script runs.
- A single-line progress bar shows pages redacted and verified.
- `--text-only` skips OCR for speed. It still writes a text-redacted PDF, but returns an error if images are present because complete redaction cannot be guaranteed.
- Passing a folder as `input` redacts every top-level `.pdf` in it instead of a single file, each to its own default output. A file already named like an output (ending in `-redacted`) is never treated as an input, so re-running a folder doesn't try to redact its own previous output. A file whose default output already exists is skipped with a message and the run continues; `--force` overwrites it and continues instead. An explicit output path (`output` or `-o`) can't be combined with a folder input unless `--recursive` is given. The run ends with a one-line summary (`Folder run complete: 2 redacted, 1 skipped, 0 failed`) and exits nonzero if any file failed.
- `--recursive` with a folder input also redacts the PDFs in every subfolder, and needs an output folder, which is created if missing. Each output lands at the same relative path under the output folder, so the input tree is recreated there:
  ```bash
  python3 pdf-redact.py statements/ redacted/ --recursive -c family.redact.enc
  ```
  ```
  statements/A.pdf              ->  redacted/A-redacted.pdf
  statements/folder 1/B.pdf     ->  redacted/folder 1/B-redacted.pdf
  ```
  A subfolder is created only when a PDF is written into it, so folders with no PDFs aren't mirrored, and `--dry-run` creates nothing. The output folder may sit inside the input folder; it is never walked into, so a re-run doesn't redact its own previous output. Skipping, `--force`, and the closing summary work as above.

## Keeping an output that fails verification

Add `--keep-unverified` to your usual command to keep the processed PDF even if the final verification finds remaining matches:

```bash
python3 pdf-redact.py return.pdf -c family.redact.enc --keep-unverified
```

The PDF is written to the usual output path, the terminal marks it **UNVERIFIED**, and every failed check is printed (including its page and target when available). The command still exits with status **2**, and folder runs count that file as failed while continuing with the other PDFs. `--force` is still required to replace an existing output. Without `--keep-unverified`, verification failures still prevent publication, but all failures are now listed rather than just the first five.

For example, `page 246 still matches exact SSN target #5` means the freshly saved PDF's 246th page still matches the fifth SSN supplied. Config SSNs come first, in config-file order, followed by command-line SSNs. The match can be the full number or a masked form with the same last four digits. It does not mean that the target was absent from the input. A missed or incompletely covered occurrence, content exposed by redaction, or OCR reading the final page differently can cause this. The message alone cannot distinguish these causes.

Inspect the named pages before sharing a retained PDF. You can add a `--region '246:x0,y0,x1,y1'` covering the surviving content and rerun on the original input. `--keep-unverified` only permits completed final checks with reported failures; it does not bypass OCR errors, unsupported source images, saving errors, or earlier `--strict` fragment failures. No file is written by `--dry-run`.

## Region coordinates

`--region PAGE:x0,y0,x1,y1` — for example `--region 1:26,92,105,140`, which is page 1 and the rectangle with corners (26, 92) and (105, 140).

**Units are PDF points: 1 point = 1/72 inch.** A US Letter page is 612 × 792 points, A4 is 595 × 842.

**The origin is the top-left corner of the page, with x increasing to the right and y increasing downward.** This is the same coordinate space the tool uses everywhere else. Note that it is flipped from the space you see when reading a PDF content stream by hand, where y counts *up* from the bottom of the page: a box at `y 652–694` in a raw content stream is `y 98–140` here.

| Part | Meaning |
|---|---|
| `x0` | left edge, points from the left edge of the page |
| `y0` | **top** edge, points from the **top** of the page |
| `x1` | right edge; must be greater than `x0` |
| `y1` | **bottom** edge; must be greater than `y0` |

`PAGE` is 1-based, matching the page numbers in the summary output. It may be a single page (`3:`), an inclusive range (`2-5:`), or `all:` for every page — useful when one slot repeats in every issue of the same statement format.

Region coordinates follow the displayed page, including its rotation, and are clipped to its visible bounds. They are transformed internally to the unrotated PDF content space. Unlike matched text hits, regions receive no padding or neighboring-line adjustment. Being a few points short silently leaves content exposed, so check the result before relying on it — `--dry-run` reports which pages a region covers, and verification fails if any text remains inside one.

```bash
# The address block on page 1, and the account number slot on every page.
python3 pdf-redact.py statement.pdf --region 1:26,92,105,140 --region all:480,5,590,30
```

## Encrypted config files

A config file holds the exact names, SSNs, and addresses you are trying to remove from a document, so it is the one artifact here worth encrypting. `--config` accepts an encrypted file transparently — it is decrypted in memory, and the plaintext never reaches the disk.

```bash
python3 pdf-redact.py --encrypt-config family.redact      # -> family.redact.enc
python3 pdf-redact.py statement.pdf -c family.redact.enc  # prompts for the passphrase
python3 pdf-redact.py --edit-config family.redact.enc     # $EDITOR, resealed on save
python3 pdf-redact.py --decrypt-config family.redact.enc  # -> family.redact
```

`--encrypt-config` leaves the plaintext original in place; remove it yourself once you have confirmed the encrypted copy decrypts. On APFS a plain `rm` does not reliably scrub the blocks, and local Time Machine snapshots may hold a copy, so treat "the plaintext existed on this disk" as true forever.

The passphrase is read from the `PDFREDACT_PASSPHRASE` environment variable when set, and otherwise prompted for without echo. It is never accepted as a command-line argument, where it would land in shell history and in `ps` output.

`--edit-config` is the only thing in this program that launches a child process, and it does so only in that mode, with no PDF open. Network access stays blocked unconditionally.

### What the encryption does and does not protect

It protects the file **at rest and in motion**: an accidental `git commit`, a cloud-sync folder, a backup on a less-trusted disk. It does **not** protect against someone who already has your unlocked session — if you keep the passphrase in `PDFREDACT_PASSPHRASE`, anything running as you can read it. Only typing the passphrase per run buys you that.

### Format

Documented so the plaintext is recoverable without this script. Anyone with the passphrase can reproduce the steps below in a few lines of Python.

```
-----BEGIN PDF-REDACT ENCRYPTED CONFIG-----
v1
<base64, wrapped at 64 columns>
-----END PDF-REDACT ENCRYPTED CONFIG-----
```

The base64 payload decodes to:

| Bytes | Field |
| --- | --- |
| 1 | format version (`0x01`) |
| 16 | scrypt salt |
| 12 | AES-GCM nonce |
| rest | AES-256-GCM ciphertext followed by its 16-byte tag |

- **Key:** `hashlib.scrypt(passphrase_utf8, salt=salt, n=2**15, r=8, p=1, dklen=32)`
- **Cipher:** AES-256-GCM, with the single version byte as additional authenticated data
- **Plaintext:** the UTF-8 config file, byte for byte

```python
import base64, hashlib
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

lines = [l.strip() for l in open("family.redact.enc") if l.strip()]
blob = base64.b64decode("".join(lines[2:-1]))
key = hashlib.scrypt(b"<passphrase>", salt=blob[1:17], n=2**15, r=8, p=1,
                     dklen=32, maxmem=256 * 1024 * 1024)
print(AESGCM(key).decrypt(blob[17:29], blob[29:], blob[:1]).decode())
```

Encryption needs the `cryptography` package (`pip install cryptography`); plaintext configs do not.


## Arguments

| Argument | Meaning |
|---|---|
| `input` | Input PDF path, or a folder to redact every top-level PDF in it (see [Behavior](#behavior)). |
| `output` | Optional output path. Cannot be combined with `-o`, or with a folder `input`. |
| `-o`, `--output-file PATH` | Alternative way to set the output path. |
| `-n`, `--name NAME` | Name to redact if found. Repeatable. |
| `-s`, `--ssn SSN` | Nine-digit SSN to redact if found. Matches compact, separated, and masked forms with the same last four digits. Repeatable. |
| `-t`, `--text PHRASE` | Case-insensitive literal phrase to redact if found. Repeatable. |
| `-a`, `--address ADDRESS` | Case-insensitive address to redact if found; also matches common directional/street-suffix abbreviations (`North`/`N`, `Street`/`St`, `Avenue`/`Ave`, ...). Repeatable. |
| `-r`, `--regex PATTERN` | Additional Python regular expression to redact if found. Repeatable. |
| `-R`, `--region PAGE:x0,y0,x1,y1` | Area to black out unconditionally, in PDF points from the top-left corner of the displayed page with y increasing downward, including page rotation. `PAGE` is 1-based, a `first-last` range, or `all`. See [Region coordinates](#region-coordinates). Repeatable. |
| `-c`, `--config FILE` | File of `key: value` redaction targets (`name`/`ssn`/`text`/`address`/`regex`/`region`), one per line; `#` comments and blank lines are ignored. Plaintext or encrypted. Repeatable, and combines with command-line targets. |
| `--encrypt-config FILE` | Encrypt a plaintext config to `FILE.enc`, or to `-o`. Takes no PDF. |
| `--decrypt-config FILE` | Write the plaintext of an encrypted config, mode `0600`. Takes no PDF. |
| `--edit-config FILE` | Decrypt an encrypted config into `$EDITOR` and reseal it on save. Takes no PDF. |
| `--barcodes auto\|all\|none` | Redact barcodes on pages that had another redaction (`auto`, the default), everywhere (`all`), or never (`none`). |
| `--pii-check auto\|all\|none` | After writing, ask about identifiers no target named (`auto`, the default), also ask about ones in the sender's own address block (`all`), or skip the check (`none`). |
| `--strict` | Treat the questions above and any leftover word fragment as failures, publishing nothing by default (see `--keep-unverified`). |
| `--ocr-dpi DPI` | OCR resolution from 150 to 600; default is 300. |
| `--ocr-language LANG` | Local Tesseract language code; default is `eng`. |
| `--text-only` | Skip OCR. If images exist, write the text-redacted output but exit with an error because it is not fully verified. |
| `--dry-run` | Report matches without writing a PDF. |
| `--force` | Replace an existing output file. |
| `--keep-unverified` | Keep the PDF when final verification reports failures, list all failed checks, and exit with status 2. Review the retained PDF before sharing. |
| `--debug` | Show MuPDF's and OCR's own diagnostic messages (e.g. `Image too small to scale!!`), suppressed by default since they don't indicate whether anything was found. |
| `-h`, `--help` | Show command help. |

`--name`, `--ssn`, `--text`, `--address`, `--regex`, and `--region` may each be supplied more than once, and none of the matching targets is required to match anything. At least one target or region must be given. No document data is sent over the network.
