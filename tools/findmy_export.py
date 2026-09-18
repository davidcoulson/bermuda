#!/usr/bin/env python3
"""
Turn Apple's Find My records into the accessory JSON Bermuda asks for.

Bermuda tracks an AirTag by deriving the addresses it can currently be
advertising, which needs the pairing secrets Apple wrote when the tag was
set up: a master key, two shared secrets and the pairing date. Those live in
records that macOS keeps under ``~/Library/com.apple.icloud.searchpartyd``,
encrypted with a key called ``BeaconStore``, and in iCloud.

There is no single way to get at them, because Apple keeps closing the local
one. What this does is convert, from whichever of these you can produce:

* a **decrypted folder** in Apple's own layout - what the OpenTagViewer
  exporter writes, and what this tool writes with ``--decrypt-only``;
* an **OpenTagViewer zip**, which is that folder plus a manifest. Its
  exporter can read the records out of iCloud, so it is the only route that
  still works on macOS 15 and later;
* **this Mac's live records**, decrypted here, which needs the BeaconStore
  key and so works on macOS 14 and earlier.

Output is one JSON file per accessory, in the shape FindMy.py's
``FindMyAccessory.to_json()`` writes, which is what Bermuda's *Add FindMy
Accessory* box accepts unmodified.

**Every file it writes is a tracking secret.** Anyone holding one can follow
that tag for as long as it lives, and no rotation takes it back. Nothing is
printed to the terminal but names and models; the keys go to files, mode
0600, and belong somewhere you would keep a password.

Usage::

    python3 tools/findmy_export.py                      # this Mac's records
    python3 tools/findmy_export.py ~/AirTags.zip        # an OpenTagViewer zip
    python3 tools/findmy_export.py ~/plist_decrypt_output
    python3 tools/findmy_export.py --decrypt-only ~/decrypted

See docs/findmy.md for which of those applies to you.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

# Apple's directory names. ``MasterBeacons`` is what macOS 11 called
# ``OwnedBeacons``; both hold the same records.
OWNED_DIRS = ("OwnedBeacons", "MasterBeacons")
NAMING_DIR = "BeaconNamingRecord"
ALIGNMENT_DIR = "KeyAlignmentRecords"
RECORD_SUFFIXES = (".record", ".plist")

DEFAULT_RECORD_PATH = Path.home() / "Library" / "com.apple.icloud.searchpartyd"
KEYCHAIN_LABEL = "BeaconStore"

# For naming output files after their accessory without letting a name that
# came out of someone else's record name a path.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ExportError(Exception):
    """Something the user can act on. Printed without a traceback."""


class Record(NamedTuple):
    """One decrypted record and where it came from."""

    path: str  # relative to the source root, so the parent directory is readable
    data: dict[str, Any]


class Accessory(NamedTuple):
    """An owned beacon with whatever else was found for it."""

    identifier: str
    owned: dict[str, Any]
    naming: dict[str, Any] | None
    alignment: dict[str, Any] | None

    @property
    def name(self) -> str | None:
        """What the Find My app calls it, if a naming record was present."""
        name = (self.naming or {}).get("name")
        return name if isinstance(name, str) and name.strip() else None

    @property
    def model(self) -> str | None:
        """The model string Apple stored, eg ``A1234`` or ``AirTag``."""
        model = self.owned.get("model")
        return model if isinstance(model, str) else None


# --------------------------------------------------------------------------
# Reading records
# --------------------------------------------------------------------------


def beacon_store_key(label: str = KEYCHAIN_LABEL) -> bytes:
    """
    Read the AES key macOS encrypts the local records with.

    It is a generic password in the login keychain, so the first call puts up
    the "wants to use your confidential information" dialog - twice, because
    ``security`` asks once to find the item and once to read it.

    On macOS 15 and later this fails whatever you type: the item moved behind
    a keychain access group only Apple's own binaries hold, so nothing you can
    run unsigned will read it. That is not a password problem and retrying
    will not help; use the iCloud route instead.
    """
    try:
        done = subprocess.run(  # noqa: S603
            ["/usr/bin/security", "find-generic-password", "-l", label, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as err:  # pragma: no cover - only if /usr/bin/security is gone
        msg = f"Could not run the 'security' command: {err}"
        raise ExportError(msg) from err

    out = done.stdout.strip()
    if done.returncode != 0 or not out:
        detail = done.stderr.strip() or f"exit status {done.returncode}"
        msg = (
            f"No '{label}' key in your keychain ({detail}).\n"
            "On macOS 15 and later that key is not readable by anything but Apple's own\n"
            "software, so this Mac cannot decrypt its own records. Export from iCloud\n"
            "instead - see docs/findmy.md."
        )
        raise ExportError(msg)

    try:
        key = bytes.fromhex(out)
    except ValueError as err:
        msg = f"The '{label}' keychain item is not hex, so it is not the key this expects."
        raise ExportError(msg) from err
    if len(key) not in (16, 24, 32):
        msg = f"The '{label}' keychain item is {len(key)} bytes, which is not an AES key."
        raise ExportError(msg)
    return key


def key_from_file(path: Path) -> bytes:
    """
    Read the BeaconStore key out of a file, ignoring whatever else is in it.

    The extractor prints a line of prose before the key, so this looks for the
    key rather than insisting the file contain only that - which means its
    output can be redirected to a file and handed straight over, with the key
    never passing through a shell argument where the process list would show it.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        msg = f"Could not read the key file: {err}"
        raise ExportError(msg) from err

    for line in text.splitlines():
        candidate = line.strip().replace(":", "").replace(" ", "")
        if len(candidate) >= 32 and all(c in "0123456789abcdefABCDEF" for c in candidate):
            try:
                key = bytes.fromhex(candidate)
            except ValueError:
                continue
            if len(key) in (16, 24, 32):
                return key

    msg = f"No key in {path} - expected a line of 32, 48 or 64 hex characters."
    raise ExportError(msg)


def decrypt_record(raw: bytes, key: bytes) -> dict[str, Any]:
    """
    Decrypt one ``.record`` file.

    The file is a binary plist holding an array of exactly three data items -
    nonce, GCM tag, ciphertext - and the plaintext is another plist, this time
    the record itself. Apple stores the tag separately from the ciphertext;
    ``cryptography`` wants them joined, in that order.
    """
    from cryptography.exceptions import InvalidTag  # noqa: PLC0415
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415

    try:
        parts = plistlib.loads(raw)
    except Exception as err:  # plistlib raises a zoo of them
        msg = f"Not a property list: {err}"
        raise ExportError(msg) from err
    if not isinstance(parts, list) or len(parts) < 3:
        msg = "Expected an encrypted record (a plist array of nonce, tag and ciphertext)"
        raise ExportError(msg)

    nonce, tag, ciphertext = (bytes(part) for part in parts[:3])
    try:
        plain = AESGCM(key).decrypt(nonce, ciphertext + tag, None)
    except InvalidTag as err:
        msg = "The BeaconStore key did not decrypt this record - wrong key, or a newer format"
        raise ExportError(msg) from err

    record = plistlib.loads(plain)
    if not isinstance(record, dict):
        msg = "Decrypted, but the result is not a record"
        raise ExportError(msg)
    return record


def load_record(raw: bytes, key_source) -> dict[str, Any]:
    """
    Read a record whether or not it is encrypted.

    An already-decrypted export is a plist dict; a live macOS record is the
    three-item array. Telling them apart by shape rather than by filename
    means a folder of either works, and a mixture does too.

    ``key_source`` is called only if something actually needs decrypting, so
    converting an export never puts up a keychain prompt.
    """
    head = plistlib.loads(raw)
    if isinstance(head, dict):
        return head
    return decrypt_record(raw, key_source())


def _iter_source_files(source: Path):
    """Yield ``(relative path, bytes)`` for every record in a folder or zip."""
    if source.is_dir():
        for path in sorted(source.rglob("*")):
            if path.is_file() and path.suffix in RECORD_SUFFIXES:
                yield path.relative_to(source).as_posix(), path.read_bytes()
        return

    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for info in sorted(archive.infolist(), key=lambda i: i.filename):
                name = info.filename
                if info.is_dir() or not name.endswith(RECORD_SUFFIXES):
                    continue
                # A zip carries its own leading folder; the layout starts at
                # whichever component is one of Apple's directory names.
                parts = name.split("/")
                for index, part in enumerate(parts):
                    if part in (*OWNED_DIRS, NAMING_DIR, ALIGNMENT_DIR):
                        yield "/".join(parts[index:]), archive.read(info)
                        break
        return

    msg = f"{source} is neither a folder nor a zip file"
    raise ExportError(msg)


def read_source(source: Path, key_source) -> tuple[list[Record], list[Record], list[Record], list[str]]:
    """
    Read every record in a source, sorted into the three kinds.

    A record that will not parse is reported and skipped rather than stopping
    the run: one unreadable file out of forty should still export the other
    thirty-nine.
    """
    owned: list[Record] = []
    naming: list[Record] = []
    alignment: list[Record] = []
    problems: list[str] = []

    for relative, raw in _iter_source_files(source):
        parts = relative.split("/")
        bucket = None
        if parts[0] in OWNED_DIRS:
            bucket = owned
        elif parts[0] == NAMING_DIR:
            bucket = naming
        elif parts[0] == ALIGNMENT_DIR:
            bucket = alignment
        if bucket is None:
            continue
        try:
            bucket.append(Record(relative, load_record(raw, key_source)))
        except ExportError as err:
            problems.append(f"{relative}: {err}")
        except Exception as err:  # noqa: BLE001 - a corrupt file should not end the run
            problems.append(f"{relative}: {err}")

    if not owned:
        # Two very different situations end here, and saying the wrong one
        # sends someone looking for a folder that was never the problem. If
        # files were found and none of them could be read, the reason they
        # could not be read is the message.
        if problems:
            detail = "\n".join(f"  {line}" for line in problems[:5])
            msg = f"Found {len(problems)} record(s) under {source} and could read none of them:\n{detail}"
        else:
            msg = (
                f"No accessory records under {source}.\n"
                f"Expected a folder holding {OWNED_DIRS[0]}/ - see docs/findmy.md."
            )
        raise ExportError(msg)

    return owned, naming, alignment, problems


def collate(owned, naming, alignment) -> tuple[list[Accessory], list[str]]:
    """
    Match each owned beacon with its name and its key alignment.

    The two associations are made differently, which is the detail worth
    knowing: a naming record says which beacon it belongs to in a field, while
    an alignment record says so only by sitting in a directory named after it.
    Flatten the folder and that association is gone.
    """
    beacons: dict[str, dict[str, Any]] = {}
    skipped: list[str] = []

    for record in owned:
        identifier = record.data.get("identifier")
        if not isinstance(identifier, str):
            skipped.append(f"{record.path}: no identifier")
            continue
        if "privateKey" not in record.data:
            # Shared tags and some paired-but-not-owned records look like this.
            # Without the private key the accessory can never be located, so an
            # entry for it would do nothing.
            skipped.append(f"{identifier}: no private key, so it cannot be located")
            continue
        beacons[identifier] = record.data

    names: dict[str, dict[str, Any]] = {}
    for record in naming:
        associated = record.data.get("associatedBeacon")
        if isinstance(associated, str) and associated in beacons:
            names[associated] = record.data

    aligns: dict[str, dict[str, Any]] = {}
    for record in alignment:
        parts = record.path.split("/")
        if len(parts) >= 3 and parts[1] in beacons:  # KeyAlignmentRecords/<beacon>/<file>
            aligns[parts[1]] = record.data

    found = [
        Accessory(identifier, data, names.get(identifier), aligns.get(identifier))
        for identifier, data in sorted(beacons.items())
    ]
    return found, skipped


# --------------------------------------------------------------------------
# Converting
# --------------------------------------------------------------------------


def _key_data(record: dict[str, Any], field: str) -> bytes:
    """
    Pull one key out of a record.

    Key material is nested two levels deep - ``{"key": {"data": <bytes>}}`` -
    which carries no information and is simply how the framework wrote it.
    """
    try:
        data = record[field]["key"]["data"]
    except (KeyError, TypeError) as err:
        msg = f"Record has no usable '{field}'"
        raise ExportError(msg) from err
    if not isinstance(data, (bytes, bytearray)):
        msg = f"Field '{field}' is not key data"
        raise ExportError(msg)
    return bytes(data)


def _serial_number(stable) -> str | None:
    """
    The hardware serial, which Apple buries in ``stableIdentifier``.

    Observed tails: ``2006~#<hwid>~#<serial>`` for an AirTag,
    ``a:/<uuid>~#<serial>`` for a third-party tag, and for AirPods a
    structured tail whose third section is the serial as hex ASCII, because
    one record covers two buds and a case.
    """
    if not stable or not isinstance(stable, list) or not isinstance(stable[0], str):
        return None
    tail = stable[0].split("~#")[-1]
    if tail.startswith("\u00b6"):  # AirPods-style
        sections = tail.split("\u00a7")
        if len(sections) >= 3:
            try:
                return bytes.fromhex(sections[2]).decode("ascii")
            except (ValueError, UnicodeDecodeError):
                return None
        return None
    return tail if tail != stable[0] else None


def _utc(value) -> datetime:
    """Apple writes these naive; they are UTC."""
    if not isinstance(value, datetime):
        msg = f"Expected a date, got {type(value).__name__}"
        raise ExportError(msg)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def to_bermuda_json(accessory: Accessory) -> dict[str, Any]:
    """
    Convert one accessory into the JSON Bermuda accepts.

    Deliberately the same field names and shape as FindMy.py's
    ``FindMyAccessory.to_json()``, so anything that reads one reads the other.
    """
    owned = accessory.owned

    master_key = _key_data(owned, "privateKey")[-28:]
    skn = _key_data(owned, "sharedSecret")
    # An AirTag carries a secondary shared secret; an iPhone or a Watch calls
    # the same thing by another name.
    secondary_field = "secondarySharedSecret" if "secondarySharedSecret" in owned else "secureLocationsSharedSecret"
    sks = _key_data(owned, secondary_field)

    try:
        paired_at = _utc(owned["pairingDate"])
    except KeyError as err:
        msg = "Record has no pairingDate"
        raise ExportError(msg) from err

    alignment_date = None
    alignment_index = None
    if accessory.alignment:
        observed = accessory.alignment.get("lastIndexObservationDate")
        index = accessory.alignment.get("lastIndexObserved")
        if isinstance(observed, datetime) and isinstance(index, int):
            alignment_date = _utc(observed).isoformat()
            alignment_index = index

    return {
        "type": "accessory",
        "master_key": master_key.hex(),
        "skn": skn.hex(),
        "sks": sks.hex(),
        "paired_at": paired_at.isoformat(),
        "name": accessory.name,
        "model": accessory.model,
        "identifier": accessory.identifier,
        "group_identifier": owned.get("groupIdentifier"),
        "serial_number": _serial_number(owned.get("stableIdentifier")),
        "alignment_date": alignment_date,
        "alignment_index": alignment_index,
    }


def output_name(accessory: Accessory, taken: set[str]) -> str:
    """A filename from the accessory's own name, distinct from the others."""
    base = _UNSAFE.sub("-", accessory.name or accessory.model or accessory.identifier).strip("-")
    base = base[:60] or "accessory"
    candidate = base
    suffix = 2
    while candidate.lower() in taken:
        candidate = f"{base}-{suffix}"
        suffix += 1
    taken.add(candidate.lower())
    return f"{candidate}.json"


def _open_secret(path: Path) -> int:
    """
    A descriptor on a file only its owner can read - BEFORE anything is in it.

    O_CREAT applies the mode only to a file that did not exist, so a rerun over
    a file left at a looser mode has to be tightened too, and tightened first:
    chmod after the write leaves the new keys readable for as long as the write
    takes. The directory is made private as well; the file names in it are the
    accessories' names, which say what someone owns.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    return fd


def write_secret(path: Path, payload: str) -> None:
    """Write a file only its owner can read, without a readable moment first."""
    with os.fdopen(_open_secret(path), "w", encoding="utf-8") as handle:
        handle.write(payload)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def decrypt_only(source: Path, out_dir: Path, key_source) -> int:
    """
    Write the decrypted records out in Apple's own layout, converting nothing.

    Useful on a Mac that can still read its own key: decrypt here, and the
    result feeds this tool, FindMy.py or anything else that speaks plists.
    """
    written = 0
    for relative, raw in _iter_source_files(source):
        parts = relative.split("/")
        if parts[0] not in (*OWNED_DIRS, NAMING_DIR, ALIGNMENT_DIR):
            continue
        try:
            record = load_record(raw, key_source)
        except Exception as err:  # noqa: BLE001
            print(f"  skipped {relative}: {err}", file=sys.stderr)
            continue
        target = (out_dir / relative).with_suffix(".plist")
        with os.fdopen(_open_secret(target), "wb") as handle:
            plistlib.dump(record, handle, fmt=plistlib.FMT_XML)
        written += 1
    print(f"Wrote {written} decrypted record(s) to {out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The command line, described where the help text can reach it."""
    parser = argparse.ArgumentParser(
        prog="findmy_export.py",
        description="Convert Apple Find My accessory records into Bermuda's accessory JSON.",
        epilog="Everything this writes is a tracking secret. See docs/findmy.md.",
    )
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        help=(
            "An OpenTagViewer zip, a folder of decrypted records, or a folder of encrypted ones. "
            f"Defaults to this Mac's own records ({DEFAULT_RECORD_PATH})."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("findmy-keys"),
        help="Where to write the JSON files (default: ./findmy-keys)",
    )
    parser.add_argument(
        "--key",
        help="The BeaconStore key as hex, if you extracted it yourself instead of reading the keychain.",
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        help=(
            "Read the BeaconStore key from a file instead of the command line, which keeps it out "
            "of your shell history and out of the process list. Any line containing 32 or more hex "
            "characters is taken as the key, so the extractor's own output can be piped straight in."
        ),
    )
    parser.add_argument(
        "--decrypt-only",
        action="store_true",
        help="Decrypt the records into --out as plists and stop, converting nothing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the conversion, reporting what happened in a way a person can act on."""
    args = build_parser().parse_args(argv)

    source = args.source or DEFAULT_RECORD_PATH
    if not source.exists():
        if args.source is None:
            print(
                f"This Mac has no Find My records at {source}.\n"
                "Recent macOS keeps them in iCloud rather than on disk, so there is nothing\n"
                "here to convert. Export from iCloud and pass the zip - see docs/findmy.md.",
                file=sys.stderr,
            )
        else:
            print(f"{source} does not exist.", file=sys.stderr)
        return 2

    # Resolved at most once, and only if an encrypted record turns up.
    cached: dict[str, bytes] = {}

    def key_source() -> bytes:
        if "key" not in cached:
            if args.key:
                cached["key"] = bytes.fromhex(args.key)
            elif args.key_file:
                cached["key"] = key_from_file(args.key_file)
            else:
                cached["key"] = beacon_store_key()
        return cached["key"]

    try:
        if args.decrypt_only:
            return decrypt_only(source, args.out, key_source)

        owned, naming, alignment, problems = read_source(source, key_source)
        accessories, skipped = collate(owned, naming, alignment)
    except ExportError as err:
        print(str(err), file=sys.stderr)
        return 2

    taken: set[str] = set()
    rows: list[tuple[str, str, str, str]] = []
    failures = list(problems)
    for accessory in accessories:
        try:
            payload = to_bermuda_json(accessory)
        except ExportError as err:
            failures.append(f"{accessory.name or accessory.identifier}: {err}")
            continue
        filename = output_name(accessory, taken)
        write_secret(args.out / filename, json.dumps(payload, indent=2) + "\n")
        rows.append(
            (
                accessory.name or "(unnamed)",
                accessory.model or "",
                "yes" if payload["alignment_index"] is not None else "no",
                filename,
            )
        )

    if not rows:
        print("Nothing could be converted.", file=sys.stderr)
        for line in failures + skipped:
            print(f"  {line}", file=sys.stderr)
        return 1

    widths = [max(len(row[col]) for row in rows) for col in range(3)]
    print(f"Wrote {len(rows)} accessory file(s) to {args.out}:\n")
    for name, model, aligned, filename in rows:
        print(f"  {name:<{widths[0]}}  {model:<{widths[1]}}  aligned:{aligned:<{widths[2]}}  {filename}")

    for line in skipped:
        print(f"\n  skipped {line}", file=sys.stderr)
    for line in failures:
        print(f"\n  failed  {line}", file=sys.stderr)

    print(
        "\nEach file is that tag's location secret: anyone who copies it can follow the\n"
        "tag for as long as it exists, and re-pairing is the only way to take it back.\n"
        "Paste one into Bermuda under Configure -> FindMy Accessories, then delete it."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
