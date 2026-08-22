# Installer

**Close World of Warcraft first.** The installer refuses to run while `WoW.exe`
is open, because the updater cannot rewrite a locked executable.

No administrator rights needed. Everything installs per-user. Safe to run more
than once; every step is idempotent.

## Install

Pick whichever is easiest.

**1. Download the project** — green **Code** button on
<https://github.com/eqomdx/EquadisOctoUpdater> → **Download ZIP** → extract →
double-click **`install/INSTALL.cmd`**.

**2. One line, nothing to download first** — open PowerShell and paste:

```powershell
irm https://raw.githubusercontent.com/eqomdx/EquadisOctoUpdater/main/install/install.ps1 | iex
```

It fetches the project into a temp folder and runs from there.

**3. Just the installer files** — if you only have `install/`, run it anyway.
When `octo_updater.py` isn't found it downloads the project from the repository
above and carries on.

Any of these gets you the same thing: Python installed if you need it, Octo
Updater patched and compiled, your game folder found, backed up and configured.

Then run `OctoUpdater.exe` and press **UPDATE**.

---

## What it does

| Step | Action |
| --- | --- |
| 1 | Finds a real Python 3.10+. Installs Python 3.12 per-user via winget if there isn't one. The Microsoft Store `python` stub is ignored — it is not an interpreter. |
| 2 | Installs PyInstaller and certifi. |
| 3 | Patches `octo_updater.py` (see below). Skips if already patched. |
| 4 | Finds your OctoWoW folder, or asks — with a folder picker. |
| 5 | Backs up everything in that folder the updater can rewrite. |
| 6 | Copies addons and settings from another install, if you asked for that. |
| 7 | Rewrites `dlls.txt` in load order and drops its stale path cache. |
| 8 | Points the updater's `config.json` at your game folder. |
| 9 | Builds `OctoUpdater.exe` in the folder above this one. |

Then run `OctoUpdater.exe` and press **UPDATE**.

## Haven't installed the game yet?

Fine. Pick or create an empty folder when asked — Octo Updater downloads the
client into it.

## Moving to a new folder, keeping your setup

```powershell
.\install.ps1 -GameFolder "E:\Games\OctoWoW" -MigrateFrom "D:\Octowow"
```

Copies `Interface\AddOns\`, the whole `WTF\` tree (Config.wtf, per-account and
per-character SavedVariables, macros, key bindings) and `realmlist.wtf`.

Not copied: `WDB`, `Logs`, `Errors` — server cache and crash dumps, all
regenerated.

The copy is a **merge**. Anything already in the target that the source doesn't
have stays, and the target's previous `Interface\AddOns` and `WTF` are saved
into the backup folder first.

---

## The patches

Applied to `octo_updater.py`. The unpatched original is kept as
`octo_updater.py.orig`.

**Tweaks defaults now match the stock client.** Upstream ships `farClip 777`,
`frillDistance 120` and `cameraDistance 50`, which silently replace the
official launcher's `1000` / `70` / `100` on a fresh install — a shorter world
draw distance, nearly double the ground clutter to render, and half the camera
zoom-out. The symptom is "the world doesn't load properly".

**The aspect-ratio field of view is a suggestion, not a default.** Three code
paths seeded the default from the monitor's aspect ratio, so a 21:9 display
silently rewrote `WoW.exe` from 110° to 150°. The figure is still shown beside
the Field of View box; nothing applies it unless you type it and press Apply.

**`dlls.txt` gets a defined load order.** `add_dll` appended in mod-install
order, which put `nampower` and `ClassicAPI` *before* `SuperWoWhook` — the
reverse of what those mods need. Entries are sorted by `DLL_LOAD_ORDER`;
anything added by hand keeps its relative order and goes last.

**`dlls.txt.cache` is invalidated on every rewrite.** VanillaFixes caches that
file as absolute paths, so moving or renaming the game folder leaves every
entry dead and the mods silently never load.

**"Install recommended addons" is unchecked by default.** Upstream ticks it, so
a fresh run drops a dozen curated addons into your `AddOns` folder without
asking. Recommended addons are a taste call, not a fix. The ADDONS tab still
lists every one of them for one-click install whenever you want them.

"Install essential mods" stays **ticked** — that is what installs DXVK, which
is the actual fix for effect-heavy frame drops.

**PLAY launches through VanillaFixes whenever it is on disk.** Upstream also
required a matching entry in the updater's own config. That entry is absent
after a game-folder switch, after a config reset, and on any install the
updater didn't perform itself — and the fallback is to start `WoW.exe`
directly, which loads **none** of your `dlls.txt` mods, with no warning. Now
the file being present is what counts, and if the loader is missing while
`dlls.txt` exists, the log says so.

---

## Backups

Written to `<game folder>\_backup_before_octoupdater_<timestamp>\`:
`WoW.exe`, `dlls.txt`, `dlls.txt.cache`, `realmlist.wtf`, `WTF\Config.wtf` —
and, when migrating, the target's whole previous `Interface\AddOns` and `WTF`.

The updater's `config.json` is copied to `config.json.bak` before it's
rewritten.

## Config choices worth knowing

`pending_reconcile` is set to **`keep-config`**, not `full`. `full` overwrites
`Config.wtf` and loses tuned UI scale, volumes and nameplate settings.
`keep-config` still does the integrity check and the `WoW.exe` re-patch.

Mod install records are **wiped** so the updater re-detects what is actually on
disk rather than trusting records from another folder, and so the essential-mod
install re-arms — that is what brings in DXVK.

Addon records are wiped too, but `auto_install_addons` is set to **false**, so
a hand-curated `AddOns` folder doesn't get the recommended set dumped into it.
Pass `-KeepAddonRecords` to leave the records alone.

## Options

| Flag | Effect |
| --- | --- |
| `-GameFolder <dir>` | Your OctoWoW folder. Skips detection. |
| `-MigrateFrom <dir>` | Copy addons and settings from this install first. |
| `-NoBuild` | Skip building the exe. |
| `-NoConfig` | Leave the updater's `config.json` alone. |
| `-KeepAddonRecords` | Don't wipe addon install records. |
| `-Yes` | Accept every prompt (unattended). |
| `-Force` | Proceed even if `WoW.exe` is running. |

## Where do addons go?

Into `<game folder>\Interface\AddOns\`. `OctoUpdater.exe` has no addon folder of
its own — it reads and writes whichever game folder it is pointed at, the same
one the game loads from. Add addons by hand there, or through the Addons tab;
both land in the same place.

## Troubleshooting

**"Python was not found; run without arguments to install from the Microsoft
Store"** — you ran `python` yourself in a shell that resolves to the Store stub.
The installer handles this; just run `INSTALL.cmd`.

**Windows Defender flags `OctoUpdater.exe`** — single-file PyInstaller builds
get false positives fairly often. Add an exclusion, or use the Settings tab's
Defender-exclusion button.

**"patch ... did not match"** — `octo_updater.py` is a different version than
this installer expects. Get a matching installer, or apply the five changes
above by hand.

**Multi-monitor setup** — install `VanillaMultiMonitorFix` from the Mods tab.
It is not in the essential set, and without it the client cannot read your
monitor's refresh rate and forces 60 Hz.

---

## Credit

Octo Updater is by **rebasedkon** — <https://github.com/rebasedkon/octo-updater>

This is a fork that adds the installer and the patches above. Support the
original author:

- Ko-fi: <https://ko-fi.com/rebased>
- Buy Me a Coffee: <https://buymeacoffee.com/rebased>
