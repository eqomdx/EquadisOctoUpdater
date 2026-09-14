#!/usr/bin/env python3
"""
Octo Updater installer - worker.

Launched by install.ps1, which guarantees a real Python and PyInstaller first.

In one pass:
  1. Patches octo_updater.py so its Tweaks defaults match the stock client, the
     aspect-ratio field of view is never auto-applied, and dlls.txt is written
     in a defined load order.  (Skipped when already patched.)
  2. Finds the OctoWoW game folder, or asks for one.
  3. Optionally copies addons and settings from another install.
  4. Backs up everything in the game folder the updater can rewrite.
  5. Normalises dlls.txt order and drops its stale absolute-path cache.
  6. Points the updater's config.json at the game folder.
  7. Builds OctoUpdater.exe.

Every step is idempotent - re-running is safe.

This is a fork installer. Octo Updater itself is by rebasedkon:
https://github.com/rebasedkon/octo-updater
"""

import argparse
import importlib.util
import io
import json
import os
import shutil
import string
import subprocess
import sys
import time

IS_WINDOWS = os.name == "nt"


# ─────────────────────────────────────────────────────────────────────────────
#  output helpers
# ─────────────────────────────────────────────────────────────────────────────

_STEP = [0]


def step(msg):
    _STEP[0] += 1
    print("")
    print("[%d] %s" % (_STEP[0], msg))
    print("-" * (len(msg) + 4))


def info(msg):
    print("    " + msg)


def warn(msg):
    print("    ! " + msg)


def die(msg):
    print("")
    print("ERROR: " + msg)
    sys.exit(1)


def fmt_size(n):
    if n < 1024 ** 2:
        return "%d KB" % (n / 1024)
    if n < 1024 ** 3:
        return "%.1f MB" % (n / 1024 ** 2)
    return "%.2f GB" % (n / 1024 ** 3)


def ask_yes_no(question, default=True):
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        try:
            raw = input(question + suffix).strip().lower()
        except EOFError:
            return default
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False


# ─────────────────────────────────────────────────────────────────────────────
#  1. source patches
# ─────────────────────────────────────────────────────────────────────────────
# Each entry is (description, exact text to find, replacement). All are applied
# to an in-memory copy and the file is only written once every one matched, so
# a partial patch can never land on disk.

PATCH_MARKER = "Patched by the fork installer"

PATCHES = [
    (
        "Tweaks defaults match the stock client",
        '''TWEAKS_DEFAULTS = {
    "locale":          DEFAULT_LOCALE,
    "alwaysAutoLoot":  True,
    "nameplateRange":  41,
    "fieldOfView":     110,
    "farClip":         777,
    "frillDistance":   120,
    "cameraDistance":  50,
    "soundInBackground": True,
}''',
        '''# These defaults deliberately match what the stock OctoWoW client and its
# official launcher already write into WoW.exe, so installing the updater does
# not change how the game looks until the player asks for a change. The
# upstream farClip 777 / frillDistance 120 / cameraDistance 50 silently
# replaced the launcher's 1000 / 70 / 100 on a fresh install: a shorter world
# draw distance, nearly double the ground clutter to render, and half the
# camera zoom-out.
TWEAKS_DEFAULTS = {
    "locale":          DEFAULT_LOCALE,
    "alwaysAutoLoot":  True,
    "nameplateRange":  41,
    "fieldOfView":     110,
    "farClip":         1000,
    "frillDistance":   70,
    "cameraDistance":  100,
    "soundInBackground": True,
}''',
    ),
    (
        "field of view is a suggestion, not an auto-applied default",
        '''def load_tweaks_config() -> dict:
    cfg     = load_config()
    stored  = cfg.get("tweaks", {})
    defaults = dict(TWEAKS_DEFAULTS)
    defaults["fieldOfView"] = fov_default_for_display()
    return {k: stored.get(k, v) for k, v in defaults.items()}''',
        '''def load_tweaks_config() -> dict:
    cfg    = load_config()
    stored = cfg.get("tweaks", {})
    # The aspect-ratio figure from fov_default_for_display() is a *suggestion*
    # shown beside the Field of View box - it is never applied on its own.
    # Seeding the default from it rewrote WoW.exe's field of view behind the
    # player's back: a 3440x1440 display silently became 150 degrees.
    return {k: stored.get(k, v) for k, v in TWEAKS_DEFAULTS.items()}''',
    ),
    (
        "dlls.txt load order + cache invalidation",
        '''def _dlls_txt_path(client_dir: str) -> str:
    return os.path.join(client_dir, "dlls.txt")


def add_dll(client_dir: str, name: str):
    path  = _dlls_txt_path(client_dir)
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    if any(l.strip().lower() == name.lower() for l in lines):
        return
    lines = [l for l in lines if l.strip()] + [name]
    with open(path, "w") as f:
        f.write("\\n".join(lines) + "\\n")


def remove_dll(client_dir: str, name: str):
    path = _dlls_txt_path(client_dir)
    if not os.path.exists(path):
        return
    lines = [l for l in open(path).read().splitlines()
             if l.strip().lower() != name.lower()]
    if not lines:
        os.remove(path)
    else:
        with open(path, "w") as f:
            f.write("\\n".join(lines) + "\\n")''',
        '''def _dlls_txt_path(client_dir: str) -> str:
    return os.path.join(client_dir, "dlls.txt")


# VanillaFixes injects the DLLs listed in dlls.txt from top to bottom, and
# several of them hook the same client functions. The display and memory
# patches have to land before SuperWoW installs its hooks, and everything that
# builds on SuperWoW's API (nampower, ClassicAPI, AuctionQueryThrottle,
# UnitXP_SP3) has to come after it. Appending each mod as it installed produced
# roughly the reverse of that, which is its own source of instability.
DLL_LOAD_ORDER = [
    "VanillaMultiMonitorFix.dll",
    "no1600x1200.dll",
    "transmogfix.dll",
    "VanillaHelpers.dll",
    "SuperWoWhook.dll",
    "nampower.dll",
    "ClassicAPI.dll",
    "AuctionQueryThrottle.dll",
    "UnitXP_SP3.dll",
]


def _sorted_dll_lines(lines: list) -> list:
    """dlls.txt entries in load order. Entries we do not know about (added by
    hand by the player) keep their relative order and go last - sorted is
    stable."""
    rank = {n.lower(): i for i, n in enumerate(DLL_LOAD_ORDER)}
    return sorted((l for l in lines if l.strip()),
                  key=lambda l: rank.get(l.strip().lower(), len(rank)))


def _invalidate_dll_cache(client_dir: str):
    """VanillaFixes caches dlls.txt as *absolute* paths in dlls.txt.cache.
    Those paths go stale the moment the game folder is moved or renamed, and a
    stale cache means the mods silently never load. Drop it whenever dlls.txt
    is rewritten so the loader rebuilds it against the current folder."""
    cache = _dlls_txt_path(client_dir) + ".cache"
    try:
        if os.path.exists(cache):
            os.remove(cache)
    except OSError:
        pass


def _write_dlls_txt(client_dir: str, lines: list):
    path = _dlls_txt_path(client_dir)
    with open(path, "w") as f:
        f.write("\\n".join(_sorted_dll_lines(lines)) + "\\n")
    _invalidate_dll_cache(client_dir)


def add_dll(client_dir: str, name: str):
    path  = _dlls_txt_path(client_dir)
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    if not any(l.strip().lower() == name.lower() for l in lines):
        lines = [l for l in lines if l.strip()] + [name]
    # Rewrite even when the entry was already present: a dlls.txt left behind
    # by an older build can still be in the wrong order.
    _write_dlls_txt(client_dir, lines)


def ensure_dll(client_dir: str, name: str) -> bool:
    """add_dll, but only when the entry is genuinely absent.

    Every write drops dlls.txt.cache and makes VanillaFixes rebuild it on the
    next launch, so a mod that is already registered must not be rewritten just
    to confirm it. Returns True when something was actually repaired."""
    path  = _dlls_txt_path(client_dir)
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    if any(l.strip().lower() == name.lower() for l in lines):
        return False
    add_dll(client_dir, name)
    return True


def reconcile_dlls_txt(client_dir: str, mods_cfg: dict) -> list:
    """Put every enabled, installed mod's DLL back into dlls.txt if it has
    gone missing. Returns the names that were re-registered.

    ensure_dll repairs one mod when the Apply loop visits it - and a targeted
    single-mod update visits only that one mod, so the loop skips the others
    and their entries never get checked. That is exactly how UnitXP_SP3 was
    lost a second time: something outside this updater (the official launcher
    rewrites dlls.txt with its own list) dropped it, and the next thing to run
    was a one-click ClassicAPI update, which wrote the file back without it.

    So the sweep is its own step, run on every Apply regardless of scope, and
    on PLAY - the last thing between the file and the loader. Cheap: one read
    of dlls.txt per mod, a write only when something is actually absent."""
    repaired = []
    for mod in MODS_REGISTRY:
        name = mod.get("register_dll")
        if not name:
            continue
        state = mods_cfg.get(mod["id"], {})
        if not state.get("enabled") or not state.get("installed_version"):
            continue
        files = state.get("installed_files") or mod.get("installed_files") or []
        if not all(os.path.exists(os.path.join(client_dir, f)) for f in files):
            continue   # the DLL itself is gone; registering it would only break the loader
        if ensure_dll(client_dir, name):
            repaired.append(name)
    return repaired


def remove_dll(client_dir: str, name: str):
    path = _dlls_txt_path(client_dir)
    if not os.path.exists(path):
        return
    lines = [l for l in open(path).read().splitlines()
             if l.strip() and l.strip().lower() != name.lower()]
    if not lines:
        os.remove(path)
        _invalidate_dll_cache(client_dir)
    else:
        _write_dlls_txt(client_dir, lines)''',
    ),
    (
        "dlls.txt reconciled for mods that need no other work",
        '''                if not enabled and state.get("error"):
                    mods_cfg.setdefault(mid, {})["error"] = None
                continue''',
        '''                if not enabled and state.get("error"):
                    mods_cfg.setdefault(mid, {})["error"] = None
                # An installed, enabled, up-to-date mod still has to be listed
                # in dlls.txt to load, and nothing else re-checks that. The
                # three paths that write the file are install, update and
                # uninstall, so once it drifts out of step with the config --
                # a restore from one of the installer's backups, an interrupted
                # run, a mod installed before DLL_LOAD_ORDER existed -- the mods
                # list reports the mod as enabled forever while VanillaFixes
                # never injects it. That is a silent failure with nothing to
                # find: the UI and the config both say yes, and only the file
                # says no.
                if enabled and is_installed and mod.get("register_dll"):
                    if ensure_dll(client_dir, mod["register_dll"]):
                        log("")
                        log(f"{mod['name']} was enabled but missing "
                            f"from dlls.txt - re-registered "
                            f"{mod['register_dll']}.")
                continue''',
    ),
    (
        "every enabled mod re-registered at the end of every Apply",
        '''        fresh_cfg  = update_config(_merge)
        fresh_mods = fresh_cfg.get("mods", {})
        # Keep pending checkbox toggles on a targeted single-mod update —
        # they were never applied and would be lost otherwise.
        if only_mod_id is None:
            self._mod_pending_state = {}
''',
        '''        fresh_cfg  = update_config(_merge)
        fresh_mods = fresh_cfg.get("mods", {})
        # Keep pending checkbox toggles on a targeted single-mod update —
        # they were never applied and would be lost otherwise.
        if only_mod_id is None:
            self._mod_pending_state = {}

        # Whatever the scope of this run, every enabled mod must be in
        # dlls.txt when it ends. See reconcile_dlls_txt for the case that
        # made this a separate step.
        for name in reconcile_dlls_txt(client_dir, fresh_mods):
            log(f"{name} was enabled but missing from dlls.txt - re-registered.")
''',
    ),
    (
        "dlls.txt reconciled on PLAY, before the loader reads it",
        '''        import subprocess
        client_dir = self._game_path.get().strip()
        cfg        = load_config()
''',
        '''        import subprocess
        client_dir = self._game_path.get().strip()
        cfg        = load_config()

        # The loader reads dlls.txt in a moment. Anything that edited that file
        # since the last Apply - the official launcher, a restore, a hand edit
        # - can have dropped an enabled mod, and a mod dropped here is a mod
        # that silently does not exist in the game. Last chance to put it back.
        try:
            for name in reconcile_dlls_txt(client_dir, cfg.get("mods", {})):
                self._log_line(f"{name} was missing from dlls.txt - "
                               f"re-registered before launch.\\n", "acct")
        except Exception as e:
            self._log_line(f"Could not check dlls.txt before launch: {e}\\n", "err")

''',
    ),
    (
        "field-of-view suggestion shown in the Tweaks panel",
        '''        for (tid, label, kind, recommended, _, desc, mn, mx, step) in TWEAKS_ITEMS:
            if kind == "section":''',
        '''        for (tid, label, kind, recommended, _, desc, mn, mx, step) in TWEAKS_ITEMS:
            if tid == "fieldOfView":
                # Shown, never applied on its own - see load_tweaks_config().
                try:
                    desc = ("%s Suggested for your display: %d."
                            % (desc, fov_default_for_display()))
                except Exception:
                    pass
            if kind == "section":''',
    ),
    (
        "'Install recommended addons' is unchecked by default",
        '''        self._auto_addons_var = tk.BooleanVar(
            value=bool(self._cfg.get("auto_install_addons", True)))''',
        '''        # Off by default. Recommended addons are a taste call, not a fix, and
        # silently dropping a dozen of them into a hand-curated AddOns folder
        # on first run is not something to do without being asked. The ADDONS
        # tab still lists every one of them for one-click install.
        self._auto_addons_var = tk.BooleanVar(
            value=bool(self._cfg.get("auto_install_addons", False)))''',
    ),
    (
        "recommended addons are not batch-installed unless asked for",
        '''        if not load_config().get("auto_install_addons", True):
            self._addons_verify()
            return''',
        '''        if not load_config().get("auto_install_addons", False):
            self._addons_verify()
            return''',
    ),
    (
        "PLAY launches through VanillaFixes whenever it is on disk",
        '''        vf_state   = cfg.get("mods", {}).get("VanillaFixes", {})
        vf_installed = (vf_state.get("enabled") and
                        vf_state.get("installed_version") and
                        os.path.exists(os.path.join(client_dir, "VanillaFixes.exe")))

        if vf_installed:
            exe     = os.path.join(client_dir, "VanillaFixes.exe")
            exe_lbl = "VanillaFixes.exe"
        else:
            exe     = os.path.join(client_dir, "WoW.exe")
            exe_lbl = "WoW.exe"''',
        '''        # Trust VanillaFixes.exe on disk, not the config record. That record
        # is absent after a game-folder switch, after a config reset, and on
        # any install this updater did not perform itself - and falling back
        # to WoW.exe in those cases launches the game with *none* of the
        # dlls.txt mods loaded, silently. Whether the loader is actually there
        # is the thing that matters.
        vf_exe       = os.path.join(client_dir, "VanillaFixes.exe")
        vf_installed = os.path.exists(vf_exe)

        if vf_installed:
            exe     = vf_exe
            exe_lbl = "VanillaFixes.exe"
        else:
            exe     = os.path.join(client_dir, "WoW.exe")
            exe_lbl = "WoW.exe"
            if os.path.exists(_dlls_txt_path(client_dir)):
                self._log_line(
                    "VanillaFixes.exe is missing, so the mods listed in "
                    "dlls.txt will NOT load. Install VanillaFixes from the "
                    "Mods tab.\\n", "err")''',
    ),
    (
        'the window carries its own icon',
        '''        self.title("Octo Updater")
        self.resizable(False, False)
        self.configure(bg=C_BG)
''',
        '''        self.title("Octo Updater")
        self.resizable(False, False)
        self.configure(bg=C_BG)
        self._apply_window_icon()
''',
    ),
    (
        'window icon helper',
        '''    def _build_header(self):
        HDR_H = self._px(108)
''',
        '''    def _apply_window_icon(self):
        """Put OctoUpdater.ico on the title bar and the taskbar entry.

        PyInstaller's --icon only brands the .exe file; the running window
        still shows Tk's feather until iconbitmap is told otherwise. The .ico
        is looked for next to the script, in the bundle (--add-data), and as
        a last resort inside the frozen executable itself, whose icon
        resource Tk can read on Windows."""
        candidates = []
        base = getattr(sys, "_MEIPASS", None)
        if base:
            candidates.append(os.path.join(base, "OctoUpdater.ico"))
        try:
            candidates.append(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "OctoUpdater.ico"))
        except NameError:
            pass
        if getattr(sys, "frozen", False):
            candidates.append(sys.executable)
        for path in candidates:
            try:
                if os.path.exists(path):
                    self.iconbitmap(default=path)
                    self._window_icon = path
                    return
            except Exception:
                continue

    def _build_header(self):
        HDR_H = self._px(108)
''',
    ),
    (
        'UPDATE ALL beside PLAY',
        '''        # Thin halo frame around the button gives a soft glow that follows
        # the button state (gold for UPDATE, green for PLAY).
        self._btn_mode = "update"
        self._btn_glow = tk.Frame(left, bg="#4a3812")
        self._btn_glow.pack(anchor="w", pady=(self._px(6), self._px(6)))
        self._upd_btn = tk.Label(self._btn_glow, text="UPDATE",
                                 font=("Segoe UI", 11, "bold"),
                                 fg="#ffffff", bg=C_GOLD,
                                 cursor="hand2",
                                 width=14, pady=self._px(7),
                                 anchor="center")
        self._upd_btn.pack(padx=self._px(3), pady=self._px(3))
        self._upd_btn.bind("<Button-1>", lambda e: self._btn_click())
        self._upd_btn.bind("<Enter>",    lambda e: self._btn_hover(True))
        self._upd_btn.bind("<Leave>",    lambda e: self._btn_hover(False))
''',
        '''        # Thin halo frame around the button gives a soft glow that follows
        # the button state (gold for UPDATE, green for PLAY).
        self._btn_mode = "update"
        btn_row = tk.Frame(left, bg=C_BG)
        btn_row.pack(anchor="w", pady=(self._px(6), self._px(6)))
        self._btn_glow = tk.Frame(btn_row, bg="#4a3812")
        self._btn_glow.pack(side="left")
        self._upd_btn = tk.Label(self._btn_glow, text="UPDATE",
                                 font=("Segoe UI", 11, "bold"),
                                 fg="#ffffff", bg=C_GOLD,
                                 cursor="hand2",
                                 width=14, pady=self._px(7),
                                 anchor="center")
        self._upd_btn.pack(padx=self._px(3), pady=self._px(3))
        self._upd_btn.bind("<Button-1>", lambda e: self._btn_click())
        self._upd_btn.bind("<Enter>",    lambda e: self._btn_hover(True))
        self._upd_btn.bind("<Leave>",    lambda e: self._btn_hover(False))

        # UPDATE ALL: every mod and addon with a newer version, in one click.
        # Lit only while there is something to update; faded otherwise, and
        # while any install is running. See _refresh_update_all_btn.
        self._all_glow = tk.Frame(btn_row, bg=UPDALL_GLOW_OFF)
        self._all_glow.pack(side="left", padx=(self._px(10), 0))
        self._all_btn = tk.Label(self._all_glow, text="UPDATE ALL",
                                 font=("Segoe UI", 11, "bold"),
                                 fg=UPDALL_FG_OFF, bg=UPDALL_BG_OFF,
                                 cursor="arrow",
                                 width=14, pady=self._px(7),
                                 anchor="center")
        self._all_btn.pack(padx=self._px(3), pady=self._px(3))
        self._all_btn.bind("<Button-1>", lambda e: self._update_all())
        self._all_btn.bind("<Enter>",    lambda e: self._all_hover(True))
        self._all_btn.bind("<Leave>",    lambda e: self._all_hover(False))
        self._all_ready = False
        self._update_all_chain = False
''',
    ),
    (
        'progress bar starts right of the two buttons',
        '''        pb_frame = tk.Frame(foot, bg=C_BG)
        pb_frame.place(x=self._px(250), y=0,
                       width=WIN_W - self._px(250) - self._px(40), height=FOOT_H)
''',
        '''        pb_frame = tk.Frame(foot, bg=C_BG)
        pb_frame.place(x=self._px(400), y=0,
                       width=WIN_W - self._px(400) - self._px(40), height=FOOT_H)
''',
    ),
    (
        'progress bar width follows',
        '''        self._pb_width  = WIN_W - self._px(250) - self._px(40)
''',
        '''        self._pb_width  = WIN_W - self._px(400) - self._px(40)
''',
    ),
    (
        'UPDATE ALL colours',
        '''C_MOD_HL     = "#a8b83c"   # olive-green highlight for installed mods
''',
        '''C_MOD_HL     = "#a8b83c"   # olive-green highlight for installed mods

# UPDATE ALL: lit when there is something to update, faded when there is not
UPDALL_BG_ON    = C_GOLD
UPDALL_BG_HOV   = C_GOLD_LT
UPDALL_FG_ON    = "#2b1f08"
UPDALL_GLOW_ON  = "#4a3812"
UPDALL_BG_OFF   = "#3a2c12"
UPDALL_FG_OFF   = "#7a6640"
UPDALL_GLOW_OFF = "#241c10"
''',
    ),
    (
        'UPDATE ALL state, hover and click',
        '''    def _set_btn_play(self):
        self._btn_mode = "play"
''',
        '''    def _update_all_count(self) -> int:
        return int(self._mod_updates_count) + int(self._addon_updates_count)

    def _refresh_update_all_btn(self):
        """Lit when at least one mod or addon has a newer version and nothing
        is installing; faded otherwise. Called wherever either count or the
        busy state can change."""
        if not hasattr(self, "_all_btn"):
            return   # footer not built yet
        busy = (self._btn_mode == "busy" or self._addons_busy
                or getattr(self, "_running", False))
        ready = self._update_all_count() > 0 and not busy
        self._all_ready = ready
        if ready:
            self._all_btn.configure(bg=UPDALL_BG_ON, fg=UPDALL_FG_ON,
                                    cursor="hand2")
            self._all_glow.configure(bg=UPDALL_GLOW_ON)
        else:
            self._all_btn.configure(bg=UPDALL_BG_OFF, fg=UPDALL_FG_OFF,
                                    cursor="arrow")
            self._all_glow.configure(bg=UPDALL_GLOW_OFF)

    def _all_hover(self, entering: bool):
        if not self._all_ready:
            return
        self._all_btn.configure(bg=UPDALL_BG_HOV if entering else UPDALL_BG_ON)

    def _update_all(self):
        """Update every mod with a newer version, then every addon with one.
        The mods run through the normal Apply worker (which updates exactly
        the enabled, non-ignored mods whose latest version differs); when it
        finishes, _update_all_chain hands over to the addons' update-all."""
        if not self._all_ready:
            return
        out = self._game_path.get().strip()
        if not out:
            return
        mods = self._mod_updates_count
        addons = self._addon_updates_count
        self._log_line(f"\\nUpdating everything: {mods} mod(s), "
                       f"{addons} addon(s)...\\n", "acct")
        self._all_ready = False
        self._refresh_update_all_btn()
        if mods > 0:
            self._update_all_chain = addons > 0
            self._set_btn_busy("Installing…")
            self._status_var.set("Downloading mods…")
            threading.Thread(target=self._apply_mods_worker,
                             args=(out,), daemon=True).start()
        else:
            self._addon_update_all()

    def _set_btn_play(self):
        self._btn_mode = "play"
''',
    ),
    (
        'mods badge refreshes UPDATE ALL',
        '''        if count != self._mod_updates_count:
            self._mod_updates_count = count
            self._draw_nav_tab("MODS")
''',
        '''        if count != self._mod_updates_count:
            self._mod_updates_count = count
            self._draw_nav_tab("MODS")
        self._refresh_update_all_btn()
''',
    ),
    (
        'addons badge refreshes UPDATE ALL',
        '''        if count != self._addon_updates_count:
            self._addon_updates_count = count
            self._draw_nav_tab("ADDONS")
''',
        '''        if count != self._addon_updates_count:
            self._addon_updates_count = count
            self._draw_nav_tab("ADDONS")
        self._refresh_update_all_btn()
''',
    ),
    (
        'busy and ready states refresh UPDATE ALL',
        '''    def _set_btn_busy(self, label="…"):
        self._btn_mode = "busy"
        self._upd_btn.configure(text=label, bg="#2a2434", fg=C_TEXT_DIM)
        self._btn_glow.configure(bg="#211c2c")
''',
        '''    def _set_btn_busy(self, label="…"):
        self._btn_mode = "busy"
        self._upd_btn.configure(text=label, bg="#2a2434", fg=C_TEXT_DIM)
        self._btn_glow.configure(bg="#211c2c")
        self._refresh_update_all_btn()
''',
    ),
    (
        'ready state refreshes UPDATE ALL',
        '''        if self._mods_have_errors():
            self._set_btn_busy("PLAY")
            self._status_var.set("Mod errors — check MODS tab")
        else:
            self._set_btn_play()
            self._status_var.set("Everything up to date!")
''',
        '''        if self._mods_have_errors():
            self._set_btn_busy("PLAY")
            self._status_var.set("Mod errors — check MODS tab")
        else:
            self._set_btn_play()
            self._status_var.set("Everything up to date!")
        self._refresh_update_all_btn()
''',
    ),
    (
        'mods done hands over to the addons',
        '''            self._apply_btn.configure(text="Apply", bg=C_PANEL_BDR, fg=C_TEXT)
            self._refresh_apply_btn_visibility()
            self._refresh_mods_badge()
            # Fresh setup chain: once the default mods finished installing,
            # the recommended addons follow (no-op if already initialized).
            self._maybe_install_default_addons()
''',
        '''            self._apply_btn.configure(text="Apply", bg=C_PANEL_BDR, fg=C_TEXT)
            self._refresh_apply_btn_visibility()
            self._refresh_mods_badge()
            # Fresh setup chain: once the default mods finished installing,
            # the recommended addons follow (no-op if already initialized).
            self._maybe_install_default_addons()
            # UPDATE ALL: the mods half is done; the addons half follows.
            if self._update_all_chain:
                self._update_all_chain = False
                self.after(0, self._addon_update_all)
''',
    ),
    (
        'folder change resets UPDATE ALL',
        '''        self._draw_nav_tab("MODS")
        self._draw_nav_tab("ADDONS")
        self._draw_nav_tab("MPQ")
        self._render_addons()
        self._render_mpq()
''',
        '''        self._draw_nav_tab("MODS")
        self._draw_nav_tab("ADDONS")
        self._draw_nav_tab("MPQ")
        self._update_all_chain = False
        self._refresh_update_all_btn()
        self._render_addons()
        self._render_mpq()
''',
    ),
    (
        "patched-build banner",
        'UPDATER_VERSION  = "1.3.1"',
        '''UPDATER_VERSION  = "1.3.1"
# ''' + PATCH_MARKER + '''. Changes against upstream v1.3.1:
#   * TWEAKS_DEFAULTS match the stock client (farClip 1000, frillDistance 70,
#     cameraDistance 100) instead of silently shortening the draw distance and
#     halving camera zoom-out on a fresh install.
#   * The aspect-ratio field of view is a suggestion in the UI, not an
#     auto-applied default - it no longer rewrites WoW.exe on its own.
#   * dlls.txt is written in a defined load order (DLL_LOAD_ORDER) rather than
#     mod-install order, and dlls.txt.cache is invalidated on every rewrite.
#   * A mod that is installed and enabled but missing from dlls.txt is
#     re-registered on Apply. Only install, update and uninstall wrote that
#     file, so a dlls.txt that drifted out of step could never be repaired -
#     the mod showed as enabled and simply never loaded.
#   * PLAY launches through VanillaFixes.exe whenever that file is present,
#     instead of requiring a config record that is missing after a folder
#     switch - which silently started the game with no mods loaded.
#   * Every enabled mod is re-registered in dlls.txt at the end of every
#     Apply - single-mod updates included - and again on PLAY, so a file
#     rewritten by the official launcher cannot silently drop a mod.
#   * "Install recommended addons" is unchecked by default, so a curated
#     AddOns folder is never filled in on first run without being asked.
#   * The window carries OctoUpdater.ico on its title bar and taskbar entry,
#     and an UPDATE ALL button beside PLAY updates every mod and addon with a
#     newer version in one click - lit only while there is something to do.
# UPDATER_VERSION is left at 1.3.1 so the daily upstream release check still
# works. Re-run the installer after replacing this file with a stock one.''',
    ),
]

# The same auto-FoV override also sits in the dirty-check and in Reset.
FOV_OVERRIDE_LINE = 'defaults["fieldOfView"] = fov_default_for_display()'


def patch_source(repo):
    src_path = os.path.join(repo, "octo_updater.py")
    if not os.path.exists(src_path):
        die("octo_updater.py not found in %s\n"
            "Run the installer from inside the downloaded Octo Updater "
            "folder." % repo)

    src = io.open(src_path, encoding="utf-8").read()

    if PATCH_MARKER in src:
        info("already patched - nothing to do")
        return False

    for name, old, new in PATCHES:
        n = src.count(old)
        if n != 1:
            die("patch '%s' did not match (found %d times).\n\n"
                "octo_updater.py is a different version than this installer "
                "expects.\nGet a matching installer, or apply the changes by "
                "hand - they are\ndescribed in install/README.md." % (name, n))
        src = src.replace(old, new)
        info("applied: " + name)

    kept, dropped = [], 0
    for line in src.splitlines(True):
        if line.strip() == FOV_OVERRIDE_LINE:
            dropped += 1
            continue
        kept.append(line)
    if dropped != 2:
        die("expected 2 leftover auto-FoV lines, found %d" % dropped)
    src = "".join(kept)
    info("applied: removed auto-FoV override from dirty-check and Reset")

    backup = src_path + ".orig"
    if not os.path.exists(backup):
        shutil.copy2(src_path, backup)
        info("unpatched original saved as octo_updater.py.orig")

    io.open(src_path, "w", encoding="utf-8", newline="").write(src)
    return True


def load_patched_module(repo):
    """Import the patched octo_updater so we reuse its own load-order rules
    rather than keeping a second copy of them here."""
    path = os.path.join(repo, "octo_updater.py")
    spec = importlib.util.spec_from_file_location("octo_updater_patched", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["octo_updater_patched"] = mod
    spec.loader.exec_module(mod)
    return mod


# ─────────────────────────────────────────────────────────────────────────────
#  2. finding the game folder
# ─────────────────────────────────────────────────────────────────────────────

def updater_config_path():
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return os.path.join(base, "OctoUpdater", "config.json")


def read_updater_config():
    path = updater_config_path()
    if not path or not os.path.exists(path):
        return {}
    try:
        return json.load(io.open(path, encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def looks_like_client(path):
    return bool(path) and os.path.exists(os.path.join(path, "WoW.exe"))


def _fixed_drives():
    if not IS_WINDOWS:
        return ["/"]
    drives = []
    for letter in string.ascii_uppercase:
        root = "%s:\\" % letter
        if os.path.isdir(root):
            drives.append(root)
    return drives


def detect_game_folders():
    """Likely OctoWoW installs, best guess first. Cheap: a fixed list of common
    locations plus a one-level listing of each drive root and common game
    roots - never a full disk scan."""
    seen, found = set(), []

    def consider(path):
        if not path:
            return
        path = os.path.abspath(path)
        key = os.path.normcase(path)
        if key in seen:
            return
        seen.add(key)
        if looks_like_client(path):
            found.append(path)

    # 1. whatever the updater is already configured with
    consider(read_updater_config().get("out_dir"))

    # 2. common explicit locations
    home = os.path.expanduser("~")
    roots = list(_fixed_drives())
    roots += [os.path.join(home, "Desktop"), os.path.join(home, "Downloads"),
              os.environ.get("ProgramFiles", ""),
              os.environ.get("ProgramFiles(x86)", "")]
    names = ["OctoWoW", "Octowow", "OctoWow", "octowow"]
    for r in roots:
        if not r or not os.path.isdir(r):
            continue
        for n in names:
            consider(os.path.join(r, n))
        for sub in ("Games", "Game"):
            for n in names:
                consider(os.path.join(r, sub, n))

    # 3. one-level listing of drive roots and common game roots, matching
    #    anything with "octo" in the name
    scan_roots = list(_fixed_drives())
    for r in list(_fixed_drives()):
        for sub in ("Games", "Game"):
            p = os.path.join(r, sub)
            if os.path.isdir(p):
                scan_roots.append(p)
    for r in scan_roots:
        try:
            entries = os.listdir(r)
        except OSError:
            continue
        for name in entries:
            if "octo" in name.lower() or "wow" in name.lower():
                consider(os.path.join(r, name))

    return found


def pick_folder_dialog(title, initial):
    """tkinter folder picker. tkinter ships with Python and Octo Updater itself
    needs it, so it is always available - but never let a headless or broken
    display kill the install."""
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(title=title, initialdir=initial or "/")
        root.destroy()
        return chosen or None
    except Exception:
        return None


def resolve_target(explicit, assume_yes, repo):
    if explicit:
        return os.path.abspath(explicit)

    candidates = detect_game_folders()

    if len(candidates) == 1:
        info("found an OctoWoW install: %s" % candidates[0])
        if assume_yes or ask_yes_no("    Use this folder?"):
            return candidates[0]
    elif len(candidates) > 1:
        info("found %d possible OctoWoW installs:" % len(candidates))
        for i, c in enumerate(candidates, 1):
            info("  %d) %s" % (i, c))
        if assume_yes:
            info("using the first one")
            return candidates[0]
        while True:
            try:
                raw = input("    Pick a number, or press Enter to browse: ").strip()
            except EOFError:
                return candidates[0]
            if not raw:
                break
            if raw.isdigit() and 1 <= int(raw) <= len(candidates):
                return candidates[int(raw) - 1]
    else:
        info("no existing OctoWoW install found automatically")

    default_new = os.path.join(repo, "OctoWoW")
    if assume_yes:
        info("using the default game folder: %s" % default_new)
        return default_new

    print("")
    print("    Pick your OctoWoW folder (the one with WoW.exe in it).")
    print("    If you have not installed the game yet, pick or create an empty")
    print("    folder - Octo Updater will download the client into it.")
    chosen = pick_folder_dialog("Select your OctoWoW game folder", repo)
    if chosen:
        return os.path.abspath(chosen)

    try:
        raw = input("    Folder path [%s]: " % default_new).strip().strip('"')
    except EOFError:
        raw = ""
    return os.path.abspath(raw) if raw else default_new


# ─────────────────────────────────────────────────────────────────────────────
#  3. backup
# ─────────────────────────────────────────────────────────────────────────────

BACKUP_FILES = [
    "WoW.exe",
    "dlls.txt",
    "dlls.txt.cache",
    "realmlist.wtf",
    os.path.join("WTF", "Config.wtf"),
]


def backup_target(target, with_user_data):
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    bk = os.path.join(target, "_backup_before_octoupdater_" + stamp)
    os.makedirs(bk, exist_ok=True)

    saved = 0
    for rel in BACKUP_FILES:
        src = os.path.join(target, rel)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(bk, rel.replace(os.sep, "_")))
            info("saved %s" % rel)
            saved += 1

    if with_user_data:
        for rel in (os.path.join("Interface", "AddOns"), "WTF"):
            src = os.path.join(target, rel)
            if os.path.isdir(src):
                info("saving %s (this can take a minute)..." % rel)
                shutil.copytree(src, os.path.join(bk, rel.replace(os.sep, "_")),
                                dirs_exist_ok=True)
                info("saved %s" % rel)
                saved += 1

    if saved:
        info("backup folder: %s" % bk)
    else:
        os.rmdir(bk)
        info("nothing to back up yet (fresh folder)")
    return bk


# ─────────────────────────────────────────────────────────────────────────────
#  4. migration
# ─────────────────────────────────────────────────────────────────────────────
# Addons and settings only. WDB / Logs / Errors are caches and crash dumps -
# regenerated, never worth carrying to a new install.

MIGRATE_TREES = [os.path.join("Interface", "AddOns"), "WTF"]
MIGRATE_FILES = ["realmlist.wtf"]


def _copy_tree(src, dst, label):
    total = count = 0
    last = time.time()
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        out = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(out, exist_ok=True)
        for name in files:
            s = os.path.join(root, name)
            try:
                shutil.copy2(s, os.path.join(out, name))
                total += os.path.getsize(s)
                count += 1
            except OSError as e:
                warn("skipped %s (%s)" % (s, e))
            if time.time() - last > 3:
                print("        %s: %d files, %s..."
                      % (label, count, fmt_size(total)))
                last = time.time()
    return count, total


def migrate(source, target):
    files = nbytes = 0
    for rel in MIGRATE_TREES:
        s = os.path.join(source, rel)
        if not os.path.isdir(s):
            warn("%s not present in the source install - skipped" % rel)
            continue
        info("copying %s ..." % rel)
        c, b = _copy_tree(s, os.path.join(target, rel), rel)
        info("copied %s: %d files, %s" % (rel, c, fmt_size(b)))
        files += c
        nbytes += b

    for rel in MIGRATE_FILES:
        s = os.path.join(source, rel)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(target, rel))
            info("copied %s" % rel)
            files += 1
            nbytes += os.path.getsize(s)

    info("migrated %d files, %s total" % (files, fmt_size(nbytes)))


# ─────────────────────────────────────────────────────────────────────────────
#  5. dlls.txt in the target
# ─────────────────────────────────────────────────────────────────────────────

def normalise_dlls(mod, target):
    path = os.path.join(target, "dlls.txt")
    if not os.path.exists(path):
        info("no dlls.txt yet - the updater writes one when it installs mods")
        return
    lines = [l for l in io.open(path, encoding="utf-8").read().splitlines()
             if l.strip()]
    ordered = mod._sorted_dll_lines(lines)
    if ordered == lines:
        info("already in load order: %s" % ", ".join(ordered))
    else:
        io.open(path, "w", encoding="utf-8",
                newline="").write("\n".join(ordered) + "\n")
        info("before: %s" % ", ".join(lines))
        info("after : %s" % ", ".join(ordered))

    cache = path + ".cache"
    if os.path.exists(cache):
        entries = [l.strip() for l in
                   io.open(cache, encoding="utf-8",
                           errors="replace").read().splitlines() if l.strip()]
        bad = [l for l in entries if not os.path.exists(l)]
        os.remove(cache)
        info("dropped dlls.txt.cache (%d of %d cached paths were dead)"
             % (len(bad), len(entries)))


# ─────────────────────────────────────────────────────────────────────────────
#  6. updater config
# ─────────────────────────────────────────────────────────────────────────────

def write_updater_config(target, keep_addon_records):
    path = updater_config_path()
    if not path:
        warn("LOCALAPPDATA is not set - skipping updater config")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)

    cfg = {}
    if os.path.exists(path):
        try:
            cfg = json.load(io.open(path, encoding="utf-8"))
        except (ValueError, OSError) as e:
            warn("existing config.json unreadable (%s) - writing a fresh one" % e)
        else:
            shutil.copy2(path, path + ".bak")
            info("existing config backed up to config.json.bak")

    cfg["out_dir"] = target
    # Mod records describe whichever folder was configured before. Dropping the
    # key is exactly what the app's own folder switch does: it re-detects what
    # is on disk, and re-arms the essential-mod install (which brings in DXVK).
    cfg.pop("mods", None)
    if not keep_addon_records:
        # Re-detect addons against this folder. The recommended-addon batch is
        # unchecked by default in the patched app, so nothing arrives uninvited
        # and an explicit choice already in the config is left alone.
        cfg.pop("addons", None)
    # 'keep-config' = integrity check + WoW.exe re-patch, Config.wtf left alone.
    # 'full' would overwrite Config.wtf and lose the player's tuned settings.
    cfg["pending_reconcile"] = "keep-config"
    # active_torrent_hash / active_client_dir are deliberately left as-is: the
    # sync sees them as stale and clears aria2's resume state itself.

    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)

    info("game folder       : %s" % target)
    info("mod records       : wiped (re-detect against this folder)")
    info("addon records     : %s" % ("kept" if keep_addon_records else
                                     "wiped (re-detect)"))
    info("recommended addons: unchecked by default - install from the "
         "Addons tab")
    info("pending_reconcile : keep-config (Config.wtf preserved)")


# ─────────────────────────────────────────────────────────────────────────────
#  7. build
# ─────────────────────────────────────────────────────────────────────────────

def build_exe(repo):
    icon = os.path.join(repo, "OctoUpdater.ico")
    cmd = [sys.executable, "-m", "PyInstaller",
           "--onefile", "--windowed",
           "--name", "OctoUpdater",
           "--noconfirm",
           "--distpath", os.path.join(repo, "dist"),
           "--workpath", os.path.join(repo, "build"),
           "--specpath", repo]
    if os.path.exists(icon):
        cmd += ["--icon", icon, "--add-data", icon + os.pathsep + "."]
    cmd.append(os.path.join(repo, "octo_updater.py"))

    info("running PyInstaller (20-60 seconds)...")
    proc = subprocess.run(cmd, cwd=repo, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, errors="replace")
    if proc.returncode != 0:
        print(proc.stdout[-4000:])
        die("PyInstaller failed (exit %d)" % proc.returncode)

    built = os.path.join(repo, "dist", "OctoUpdater.exe")
    if not os.path.exists(built):
        die("PyInstaller reported success but %s is missing" % built)

    final = os.path.join(repo, "OctoUpdater.exe")
    shutil.copy2(built, final)
    info("built %s (%s)" % (final, fmt_size(os.path.getsize(final))))
    return final


# ─────────────────────────────────────────────────────────────────────────────

def find_repo(explicit):
    """The Octo Updater project folder - the one holding octo_updater.py.

    Works whether this script sits in install/ inside the project, or directly
    alongside octo_updater.py. The installer does not contain Octo Updater; it
    builds it, so the project has to be there."""
    if explicit:
        repo = os.path.abspath(explicit)
        if not os.path.exists(os.path.join(repo, "octo_updater.py")):
            die("no octo_updater.py in the folder you passed: %s" % repo)
        return repo

    searched = []
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(3):
        searched.append(d)
        if os.path.exists(os.path.join(d, "octo_updater.py")):
            return d
        up = os.path.dirname(d)
        if not up or up == d:
            break
        d = up

    die("octo_updater.py was not found.\n\n"
        "Looked in:\n" + "".join("    %s\n" % s for s in searched) +
        "\nThis installer builds Octo Updater, but it does not contain it.\n"
        "You need the whole project, not just the installer files.\n\n"
        "Fix it one of two ways:\n"
        "  1. Download the full project from\n"
        "     https://github.com/rebasedkon/octo-updater\n"
        "     then put this install folder inside it and run it again.\n"
        "  2. Point the installer at the project yourself:\n"
        "     octo_setup.py --repo \"C:\\path\\to\\octo-updater\"\n\n"
        "The project folder is the one containing octo_updater.py.")


def game_is_running():
    if not IS_WINDOWS:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq WoW.exe"],
                             stdout=subprocess.PIPE, text=True,
                             errors="replace").stdout
    except OSError:
        return False
    return "WoW.exe" in out


def main():
    ap = argparse.ArgumentParser(description="Install and build Octo Updater.")
    ap.add_argument("--repo", default=None,
        help="folder holding octo_updater.py (default: found automatically)")
    ap.add_argument("--game-folder", default=None,
                    help="OctoWoW folder to set up (default: auto-detect/ask)")
    ap.add_argument("--migrate-from", default=None,
                    help="another install to copy addons and settings FROM")
    ap.add_argument("--no-build", action="store_true", help="skip the exe build")
    ap.add_argument("--no-config", action="store_true",
                    help="leave the updater's config.json alone")
    ap.add_argument("--keep-addon-records", action="store_true",
                    help="do not wipe the updater's addon install records")
    ap.add_argument("--yes", action="store_true",
                    help="accept every prompt (unattended)")
    ap.add_argument("--force", action="store_true",
                    help="proceed even if WoW.exe is running")
    args = ap.parse_args()

    repo = find_repo(args.repo)

    print("=" * 70)
    print(" Octo Updater - fork installer")
    print(" Original project by rebasedkon:")
    print("   https://github.com/rebasedkon/octo-updater")
    print("=" * 70)
    print(" Octo Updater folder : %s" % repo)

    if game_is_running() and not args.force:
        die("World of Warcraft is running. Close the game and run this again "
            "-\nthe updater cannot rewrite a locked WoW.exe. "
            "(--force overrides.)")

    step("Patching octo_updater.py")
    patch_source(repo)
    mod = load_patched_module(repo)
    tw = mod.load_tweaks_config()
    info("Tweaks defaults: FoV %s, world distance %s, clutter %s, camera %s"
         % (tw["fieldOfView"], tw["farClip"], tw["frillDistance"],
            tw["cameraDistance"]))

    step("Finding your OctoWoW folder")
    target = resolve_target(args.game_folder, args.yes, repo)
    if not os.path.isdir(target):
        if args.yes or ask_yes_no("    %s does not exist. Create it?" % target):
            os.makedirs(target, exist_ok=True)
            info("created %s" % target)
        else:
            die("no game folder chosen")
    info("game folder: %s" % target)
    if looks_like_client(target):
        info("WoW.exe found - existing install")
    else:
        info("no WoW.exe here yet - Octo Updater will download the client")

    source = os.path.abspath(args.migrate_from) if args.migrate_from else None
    do_migrate = bool(source) and (os.path.normcase(source)
                                   != os.path.normcase(target))
    if source and not do_migrate:
        warn("--migrate-from is the same folder as the target - nothing to copy")
    if do_migrate and not looks_like_client(source):
        die("no WoW.exe in the folder to migrate from: %s" % source)

    step("Backing up")
    backup_target(target, do_migrate)

    if do_migrate:
        step("Copying addons and settings")
        migrate(source, target)

    step("Checking dlls.txt load order")
    normalise_dlls(mod, target)

    if not args.no_config:
        step("Pointing Octo Updater at your game folder")
        write_updater_config(target, args.keep_addon_records)

    if not args.no_build:
        step("Building OctoUpdater.exe")
        build_exe(repo)

    print("")
    print("=" * 70)
    print(" Done.")
    print("=" * 70)
    print("")
    print("  Run:  %s" % os.path.join(repo, "OctoUpdater.exe"))
    print("")
    print("  Press UPDATE. It verifies the client, patches WoW.exe with")
    print("  stock-matching values, installs the essential mods including")
    print("  DXVK, and writes dlls.txt in the correct load order.")
    print("  Your Config.wtf is left alone.")
    print("")
    print("  On a multi-monitor setup, also install VanillaMultiMonitorFix")
    print("  from the Mods tab - without it the client cannot read your")
    print("  monitor refresh rate and forces 60 Hz.")
    print("")


if __name__ == "__main__":
    main()
