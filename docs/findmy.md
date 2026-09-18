# Tracking AirTags and other Find My accessories

Bermuda can follow an AirTag around the house, but not by hearing it: the tag
changes its Bluetooth address every 15 minutes, and the new address is not
guessable from the old one. It is generated from a key schedule seeded when you
paired the tag, so the only way to recognise a tag is to know that seed and run
the schedule forward yourself. That is what Bermuda does, and it is why this is
more work than adding an iBeacon.

So the whole job is: get the pairing secrets out of Apple's Find My, and paste
them into **Bermuda → Configure → FindMy Accessories → Add**.

The secrets arrive as one JSON file per accessory, like this (shortened):

```json
{
  "type": "accessory",
  "master_key": "…56 hex characters…",
  "skn": "…64 hex characters…",
  "sks": "…64 hex characters…",
  "paired_at": "2024-03-01T12:00:00+00:00",
  "name": "Keys",
  "model": "AirTag",
  "alignment_index": 62841,
  "alignment_date": "2026-09-01T06:30:00+00:00"
}
```

> **Each of these files is that tag's location secret.** Anyone who copies one
> can follow the tag for as long as it exists, from anywhere, using Apple's
> network — and there is no revoking it short of unpairing and re-pairing the
> tag. Keep them the way you keep passwords, and delete them once they are in
> Home Assistant. Note that they then live in your config entry, so they are in
> your backups and in Bermuda's diagnostics download too.

## Which route applies to you

Apple keeps the records in two places: encrypted files under
`~/Library/com.apple.icloud.searchpartyd` on a Mac, and in iCloud. Getting at
the local ones has been progressively closed off, so the version of macOS you
are on decides the route.

| You have | Route |
| --- | --- |
| macOS 14 (Sonoma) or earlier, signed into iCloud | [The local files](#route-1-a-macs-own-files), read by the tool in this repo |
| macOS 15 (Sequoia) or later | [iCloud](#route-2-icloud), because the local route no longer works |
| Windows or Linux, no Mac at all | [iCloud](#route-2-icloud) |
| A FindMy.py JSON file already | Nothing to do — paste it straight into Bermuda |

On macOS 15 and later, two things changed and both matter. The key that
decrypts the local files moved behind a keychain access group that only Apple's
own signed binaries hold, so `security find-generic-password -l BeaconStore`
now fails no matter what password you type; reading it again means disabling
System Integrity Protection (documented at
[pajowu/beaconstorekey-extractor](https://github.com/pajowu/beaconstorekey-extractor)),
which is not worth doing to add a tag to Home Assistant. And on a current macOS
the files may not be on disk at all — on macOS 26 the directory does not exist,
so there is nothing local to decrypt even with the key.

## Route 1: a Mac's own files

On macOS 14 or earlier, everything needed is already on the machine.

```bash
python3 tools/findmy_export.py --out ~/findmy-keys
```

macOS will ask for your login password twice — once to find the `BeaconStore`
key and once to read it. The tool prints the tags it found and writes one file
per tag:

```
Wrote 3 accessory file(s) to /Users/you/findmy-keys:

  Keys        AirTag   aligned:yes  Keys.json
  Backpack    AirTag   aligned:yes  Backpack.json
  Meg's Cafe  AirTag   aligned:no   Meg-s-Cafe.json
```

It prints names and models only; the key material goes to the files, which are
written mode `0600`.

Only `cryptography` is needed, which you already have if you have Home
Assistant. Nothing else is installed and nothing talks to the network.

## Route 2: iCloud

The records are in iCloud as well, and Apple will hand them to an account
holder who signs in. No released library does this yet, but
[OpenTagViewer](https://github.com/parawanderer/OpenTagViewer)'s exporter does,
using an unreleased branch of FindMy.py. Run it, then convert what it writes.

It is a third-party program and you will be typing your Apple ID password into
it. It states that the sign-in is read-only and that nothing is kept; that is
its claim rather than something this project has verified, so read it yourself
if that matters to you. Below is the command sequence; the full instructions
are in that repository.

```bash
git clone https://github.com/parawanderer/OpenTagViewer.git
cd OpenTagViewer/python
uv sync
uv run python -m exporter.cli --no-password
```

It asks for your Apple ID and password, a two-factor code, and then the
**screen-lock passcode of one device on your account** — that last one is what
unlocks the beacon records, and is the step people get stuck on. Tick the tags
you want and choose where to save. The result is a zip.

Then convert it, from this repository:

```bash
python3 tools/findmy_export.py ~/Downloads/AirTags.zip --out ~/findmy-keys
```

Same output as above: one JSON file per tag, ready to paste.

`uv` is [Astral's Python runner](https://docs.astral.sh/uv/); it fetches its
own Python, so you do not need one installed. On macOS: `brew install uv`.

## Putting them into Bermuda

For each file: open it, copy the whole thing, and paste it into **Settings →
Devices & Services → Bermuda → Configure → FindMy Accessories → Add**. Name it
if you like, though the export usually carries the name Find My knows it by.

Bermuda creates a device tracker named `findmy_<identifier>` and starts
deriving addresses for it. Expect nothing for a minute or two — the tag has to
advertise, and a proxy has to hear it.

**Include the alignment fields if you can** (`alignment_index` and
`alignment_date`, which this tool fills in whenever a `KeyAlignmentRecords`
entry was present). They say where the tag had got to in its key schedule when
Apple last saw it. Without them Bermuda starts from the pairing date and has to
search a wide window of candidate addresses — it still locks on, but it takes
longer and costs more work each cycle until it does.

Then delete the JSON files.

## What can actually be tracked this way

Anything that appears in Find My as an item, which is more than just AirTags:

- **AirTags** — the straightforward case. Broadcasts constantly, day and night.
- **Third-party Find My tags** (Chipolo, Eufy, and so on) — same mechanism,
  same export, same result.
- **AirPods** with Find My support — these appear in the export, and Bermuda
  can follow them, but the caveat is physical: AirPods in a closed case with a
  flat battery are not advertising anything. One record covers both buds and
  the case, and the serial in the export is whichever section the record
  names.
- **Not the Siri Remote.** It is not a Find My accessory, whatever the name of
  the feature suggests: tvOS 17's "find my remote" is an iPhone ranging the
  remote directly over Bluetooth. There are no pairing keys, no record in the
  export, and nothing for Bermuda to follow. A remote bonded to its Apple TV
  also does not advertise while connected, so it cannot be tracked passively
  either. To track one, put a tag on it - an AirTag in one of the remote
  sleeves made for the purpose, or a small iBeacon - and track the tag.

**iPhones, iPads, Macs and Apple Watches are the exception.** They are in these
records too, but use Identity Resolving Keys for local Bluetooth, which is a
much better fit: an IRK is testable against an address directly, with no key
schedule to run forward and no window to search. Add those through Home
Assistant's **Private BLE Device** integration instead, which Bermuda picks up
automatically. Sextant's *Add a thing* wizard does this for you.

## When it does not work

**"No BeaconStore key in your keychain"** on macOS 15 or later — expected, and
not a password problem. Use route 2.

**"This Mac has no Find My records"** — the local cache is empty or absent.
Recent macOS does not keep it. Use route 2.

**The accessory is added but never seen.** Check a proxy is in range and
hearing anything at all (the Bermuda device list will show plenty of unnamed
devices if it is). An AirTag near its owner advertises differently from one
that has been left behind, and Bermuda handles both — but a tag in a drawer
with a dead battery advertises nothing. Also check the pairing date came
through: a wrong `paired_at` puts the whole search window in the wrong place.

**It was working and now it is not.** If the tag was unpaired and re-paired,
every secret in the export is stale. Export it again.
