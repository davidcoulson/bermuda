"""
Tests for tools/findmy_export.py, the Find My record converter.

No real accessory records exist to test against - they are tracking secrets and
cannot be checked in - so these build records in Apple's shape and assert that
what comes out the far end is something Bermuda's own parser accepts. That
round trip is the thing worth guarding: the format is undocumented, and a field
read from the wrong place produces a file that looks right and tracks nothing.
"""

from __future__ import annotations

import importlib.util
import json
import plistlib
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from custom_components.bermuda.bermuda_findmy import FindMyAccessoryKeys

_SPEC = importlib.util.spec_from_file_location(
    "findmy_export",
    Path(__file__).resolve().parents[1] / "tools" / "findmy_export.py",
)
findmy_export = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(findmy_export)


BEACON_ID = "1E1CF4E1-2EAD-4B2B-9E12-9C1E2DAAFEED"
OTHER_ID = "2E1CF4E1-2EAD-4B2B-9E12-9C1E2DAAFEED"
PAIRED_AT = datetime(2024, 3, 1, 12, 0, 0)  # naive, as Apple writes it
STORE_KEY = bytes(range(32))


def _wrapped(data: bytes) -> dict:
    """Key material as the records hold it: two levels of wrapping, no meaning."""
    return {"key": {"data": data}}


def owned_record(
    identifier: str = BEACON_ID,
    *,
    model: str = "AirTag",
    secondary_field: str = "secondarySharedSecret",
    stable: list[str] | None = None,
    private_key: bytes | None = None,
    include_private_key: bool = True,
) -> dict:
    """One owned beacon, with the fields the converter reads."""
    record: dict = {
        "identifier": identifier,
        "model": model,
        "pairingDate": PAIRED_AT,
        "sharedSecret": _wrapped(bytes([1]) * 32),
        secondary_field: _wrapped(bytes([2]) * 32),
        "stableIdentifier": stable if stable is not None else ["2006~#HW1234~#HHXXAB0CD1EF"],
        "groupIdentifier": "GROUP-1",
    }
    if include_private_key:
        # Apple stores more than the 28 bytes that matter; the master key is
        # the tail, so a converter that takes the head silently produces
        # rubbish. Pad the front to keep that honest.
        key = private_key or bytes([3]) * 28
        record["privateKey"] = _wrapped(b"\x00\x00\x00\x00" + key)
    return record


def naming_record(name: str = "Keys", associated: str = BEACON_ID) -> dict:
    return {"name": name, "emoji": "🔑", "associatedBeacon": associated}


def alignment_record(index: int = 1234) -> dict:
    return {
        "lastIndexObserved": index,
        "lastIndexObservationDate": datetime(2026, 9, 1, 6, 30, 0),
    }


def write_plain(root: Path, *, naming: bool = True, alignment: bool = True, owned: dict | None = None) -> Path:
    """A folder of decrypted records, as an export writes them."""
    (root / "OwnedBeacons").mkdir(parents=True, exist_ok=True)
    (root / "OwnedBeacons" / f"{BEACON_ID}.plist").write_bytes(plistlib.dumps(owned or owned_record()))
    if naming:
        named = root / "BeaconNamingRecord" / BEACON_ID
        named.mkdir(parents=True, exist_ok=True)
        (named / "AAAA.plist").write_bytes(plistlib.dumps(naming_record()))
    if alignment:
        aligned = root / "KeyAlignmentRecords" / BEACON_ID
        aligned.mkdir(parents=True, exist_ok=True)
        (aligned / "BBBB.plist").write_bytes(plistlib.dumps(alignment_record()))
    return root


def encrypt(record: dict, key: bytes = STORE_KEY) -> bytes:
    """A record in the on-disk encrypted form: plist of nonce, tag, ciphertext."""
    nonce = b"\x01" * 12
    sealed = AESGCM(key).encrypt(nonce, plistlib.dumps(record), None)
    ciphertext, tag = sealed[:-16], sealed[-16:]
    return plistlib.dumps([nonce, tag, ciphertext])


def write_encrypted(root: Path) -> Path:
    """A folder in the live macOS form, .record files and all."""
    (root / "OwnedBeacons").mkdir(parents=True, exist_ok=True)
    (root / "OwnedBeacons" / f"{BEACON_ID}.record").write_bytes(encrypt(owned_record()))
    named = root / "BeaconNamingRecord" / BEACON_ID
    named.mkdir(parents=True, exist_ok=True)
    (named / "AAAA.record").write_bytes(encrypt(naming_record()))
    return root


def convert(source: Path, out: Path, argv_extra: list[str] | None = None) -> int:
    return findmy_export.main([str(source), "--out", str(out), *(argv_extra or [])])


def only_file(out: Path) -> dict:
    files = sorted(out.glob("*.json"))
    assert len(files) == 1, f"expected one file, got {[f.name for f in files]}"
    return json.loads(files[0].read_text())


def test_converts_a_decrypted_folder(tmp_path):
    """The everyday case: an export folder in, one JSON file per accessory out."""
    out = tmp_path / "out"
    assert convert(write_plain(tmp_path / "src"), out) == 0

    payload = only_file(out)
    assert payload["type"] == "accessory"
    assert payload["name"] == "Keys"
    assert payload["model"] == "AirTag"
    assert payload["identifier"] == BEACON_ID
    assert payload["paired_at"] == "2024-03-01T12:00:00+00:00"  # naive input read as UTC
    assert payload["serial_number"] == "HHXXAB0CD1EF"
    assert payload["group_identifier"] == "GROUP-1"


def test_output_is_what_bermuda_accepts(tmp_path):
    """The point of the whole tool: the file pastes into Bermuda unmodified."""
    out = tmp_path / "out"
    convert(write_plain(tmp_path / "src"), out)

    keys = FindMyAccessoryKeys.from_json(sorted(out.glob("*.json"))[0].read_text())

    assert keys.name == "Keys"
    assert keys.identifier == BEACON_ID
    assert keys.alignment_index == 1234
    assert keys.address == f"findmy_{BEACON_ID.replace('-', '').lower()}"
    # And it can actually derive addresses, which is what the keys are for.
    assert keys.macs_for_window(datetime(2026, 9, 1, 7, 0, tzinfo=UTC))


def test_master_key_is_the_tail_of_the_private_key(tmp_path):
    """The stored private key is longer than the 28 bytes that matter."""
    out = tmp_path / "out"
    convert(write_plain(tmp_path / "src"), out)
    assert only_file(out)["master_key"] == (bytes([3]) * 28).hex()


def test_alignment_comes_from_the_directory_name(tmp_path):
    """An alignment record names its beacon only by where it sits."""
    out = tmp_path / "out"
    src = write_plain(tmp_path / "src")
    convert(src, out)
    assert only_file(out)["alignment_index"] == 1234

    # Moved under another beacon's directory, it belongs to that beacon, so
    # this one has no alignment at all.
    misfiled = tmp_path / "src2"
    write_plain(misfiled, alignment=False)
    stray = misfiled / "KeyAlignmentRecords" / OTHER_ID
    stray.mkdir(parents=True)
    (stray / "BBBB.plist").write_bytes(plistlib.dumps(alignment_record()))

    out2 = tmp_path / "out2"
    convert(misfiled, out2)
    payload = only_file(out2)
    assert payload["alignment_index"] is None
    assert payload["alignment_date"] is None


def test_idevice_secondary_secret_is_read_under_its_other_name(tmp_path):
    """An iPhone or Watch calls the secondary secret something else."""
    src = write_plain(
        tmp_path / "src",
        owned=owned_record(model="iPhone15,2", secondary_field="secureLocationsSharedSecret"),
    )
    out = tmp_path / "out"
    assert convert(src, out) == 0
    assert only_file(out)["sks"] == (bytes([2]) * 32).hex()


def test_airpods_serial_is_hex_ascii_in_a_structured_tail(tmp_path):
    """AirPods put three devices in one record and encode the serial."""
    stable = ["a:/" + OTHER_ID + "~#\u00b6A2698\u00a7HW9\u00a7" + b"H1XYZ23ABCDE".hex() + "\u00a70"]
    src = write_plain(tmp_path / "src", owned=owned_record(model="AirPods", stable=stable))
    out = tmp_path / "out"
    convert(src, out)
    assert only_file(out)["serial_number"] == "H1XYZ23ABCDE"


def test_record_without_a_private_key_is_skipped(tmp_path, capsys):
    """A shared tag cannot be located, so an entry for it would do nothing."""
    src = write_plain(tmp_path / "src", owned=owned_record(include_private_key=False))
    out = tmp_path / "out"
    assert convert(src, out) == 1
    assert "cannot be located" in capsys.readouterr().err


def test_decrypts_live_records_with_the_store_key(tmp_path):
    """The macOS 14-and-earlier route, with the keychain key supplied by hand."""
    out = tmp_path / "out"
    assert convert(write_encrypted(tmp_path / "src"), out, ["--key", STORE_KEY.hex()]) == 0
    payload = only_file(out)
    assert payload["name"] == "Keys"
    assert payload["master_key"] == (bytes([3]) * 28).hex()


def test_wrong_store_key_is_reported_not_crashed(tmp_path, capsys):
    """A bad key is a thing the user typed, so it gets a sentence, not a stack."""
    out = tmp_path / "out"
    assert convert(write_encrypted(tmp_path / "src"), out, ["--key", (b"\xff" * 32).hex()]) == 2
    assert "did not decrypt" in capsys.readouterr().err


def test_decrypted_folder_never_asks_for_the_keychain(tmp_path, monkeypatch):
    """Converting an export must not put up a macOS password prompt."""

    def explode(*_args, **_kwargs):
        raise AssertionError("the keychain was consulted for an already-decrypted export")

    monkeypatch.setattr(findmy_export, "beacon_store_key", explode)
    assert convert(write_plain(tmp_path / "src"), tmp_path / "out") == 0


def test_reads_an_opentagviewer_zip(tmp_path):
    """The zip carries a leading folder; the layout starts inside it."""
    src = write_plain(tmp_path / "src")
    bundle = tmp_path / "AirTags.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("OPENTAGVIEWER.yml", "format_version: 0.0.2\n")
        for path in sorted(src.rglob("*.plist")):
            archive.write(path, f"export/{path.relative_to(src).as_posix()}")

    out = tmp_path / "out"
    assert convert(bundle, out) == 0
    assert only_file(out)["name"] == "Keys"
    assert only_file(out)["alignment_index"] == 1234


def test_files_are_named_after_the_accessory_and_readable_only_by_owner(tmp_path):
    """Two tags, two files, and neither is world-readable."""
    src = write_plain(tmp_path / "src")
    (src / "OwnedBeacons" / f"{OTHER_ID}.plist").write_bytes(plistlib.dumps(owned_record(OTHER_ID)))
    second = src / "BeaconNamingRecord" / OTHER_ID
    second.mkdir(parents=True)
    (second / "CCCC.plist").write_bytes(plistlib.dumps(naming_record("Meg's Cafe/../tag", OTHER_ID)))

    out = tmp_path / "out"
    assert convert(src, out) == 0

    names = sorted(path.name for path in out.glob("*.json"))
    assert names == ["Keys.json", "Meg-s-Cafe-..-tag.json"]
    for path in out.glob("*.json"):
        assert path.stat().st_mode & 0o077 == 0


def test_two_tags_with_one_name_get_distinct_files(tmp_path):
    """Find My does not stop anyone calling two tags the same thing."""
    src = write_plain(tmp_path / "src")
    (src / "OwnedBeacons" / f"{OTHER_ID}.plist").write_bytes(plistlib.dumps(owned_record(OTHER_ID)))
    second = src / "BeaconNamingRecord" / OTHER_ID
    second.mkdir(parents=True)
    (second / "CCCC.plist").write_bytes(plistlib.dumps(naming_record("Keys", OTHER_ID)))

    out = tmp_path / "out"
    convert(src, out)
    assert sorted(path.name for path in out.glob("*.json")) == ["Keys-2.json", "Keys.json"]


def test_unnamed_accessory_still_exports(tmp_path):
    """A tag with no naming record is nameless, not unexportable."""
    out = tmp_path / "out"
    assert convert(write_plain(tmp_path / "src", naming=False), out) == 0
    assert only_file(out)["name"] is None


def test_empty_source_says_what_was_expected(tmp_path, capsys):
    """The commonest mistake is pointing it at the wrong folder."""
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert convert(empty, tmp_path / "out") == 2
    assert "OwnedBeacons" in capsys.readouterr().err


def test_missing_source_is_not_a_traceback(tmp_path, capsys):
    assert findmy_export.main([str(tmp_path / "gone"), "--out", str(tmp_path / "out")]) == 2
    assert "does not exist" in capsys.readouterr().err


def test_decrypt_only_writes_plists_in_the_same_layout(tmp_path):
    """The halfway house: decrypt here, convert (or not) elsewhere."""
    out = tmp_path / "plain"
    assert findmy_export.main(
        [str(write_encrypted(tmp_path / "src")), "--out", str(out), "--key", STORE_KEY.hex(), "--decrypt-only"]
    ) == 0

    decrypted = out / "OwnedBeacons" / f"{BEACON_ID}.plist"
    assert decrypted.exists()
    assert plistlib.loads(decrypted.read_bytes())["identifier"] == BEACON_ID
    # And the result is itself a valid source.
    final = tmp_path / "out"
    assert convert(out, final) == 0
    assert only_file(final)["name"] == "Keys"


@pytest.mark.parametrize(
    ("stable", "expected"),
    [
        (["2006~#HW1234~#HHXXAB0CD1EF"], "HHXXAB0CD1EF"),   # AirTag
        (["a:/" + OTHER_ID + "~#C7XYZ01ABCDE"], "C7XYZ01ABCDE"),  # third-party tag
        (["nothing-separated"], None),
        ([], None),
        (None, None),
    ],
)
def test_serial_number_forms(stable, expected):
    assert findmy_export._serial_number(stable) == expected
