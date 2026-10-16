#!/usr/bin/env python3
"""aofi — anime launcher for ani-cli, in the style of hofi.

Opens on your ani-cli history as a cover grid; picking a show continues it.
Typing + Enter swaps the grid for ani-cli's search results; picking one plays it.
ani-cli's episode picker shows up as a rofi popup; closing mpv ends playback.
"""

import io
import os
import re
import html
import json
import time
import shutil
import hashlib
import functools
import subprocess
import tomllib
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dataclasses import dataclass

import fcntl
import signal
import sys
import threading

_show_pending = threading.Event()

# Must register before GTK imports (which are slow) so SIGUSR1
# doesn't kill the process while libraries are loading
if "--daemon" in sys.argv:
    signal.signal(signal.SIGUSR1, lambda *_: _show_pending.set())

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, GtkLayerShell

_LOCK_FILE    = Path("/tmp/aofi.lock")
_VISIBLE_FILE = Path("/tmp/aofi.visible")
_lock_fh      = None  # held for the lifetime of the process

def _acquire_lock() -> bool:
    global _lock_fh
    _lock_fh = open(_LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False

CACHE       = Path.home() / ".cache" / "aofi"
COVER_CACHE = CACHE / "covers"
RECENT_PATH = CACHE / "recent.json"   # anime_id → last time its episode changed
SEEN_PATH   = CACHE / "seen.json"     # anime_id → episode at last look
BIN_DIR     = CACHE / "bin"           # holds the rofi shim put first on ani-cli's PATH
HIST_PATH   = Path(
    os.environ.get("ANI_CLI_HIST_DIR")
    or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "ani-cli"
) / "ani-hsts"

CONFIG_PATH = Path(__file__).parent / "config.toml"
ANILIST     = "https://graphql.anilist.co"

DEFAULT_CONFIG = """\
[display]
columns       = 3
window_width  = 600
window_height = 400
card_ratio    = 1.45
spacing       = 10
padding       = 12
font_size     = 13
# font = "JetBrains Mono 12"   # default: the font from ~/.config/rofi/config.rasi
# window_x / window_y pin the window; leave them out to center it like rofi

[ani_cli]
# extra flags for every ani-cli run, e.g. ["--dub"], ["--skip"], ["-q", "1080p"]
args = []
"""


@dataclass
class Show:
    title:    str
    anime_id: str
    episode:  str          # last watched episode; "" for search results
    cover:    str = ""
    pick:     str = ""     # search results: the ani-cli title to auto-pick
    poster:   str = ""     # search results: poster url from ani-cli's site


# ---------------------------------------------------------------------------
# ani-cli history
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}

def load_history() -> list[Show]:
    """ani-cli history, most recently watched first."""
    shows: list[Show] = []
    try:
        lines = HIST_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        lines = []
    for line in lines:
        parts = line.split("\t")
        if len(parts) == 3 and parts[1]:
            ep, anime_id, title = parts
            shows.append(Show(title=title, anime_id=anime_id, episode=ep, cover=_cover_path(title)))

    # ani-cli rewrites entries in place, so file order isn't watch order:
    # stamp a show as recent whenever its episode changes (works for terminal ani-cli too)
    seen, recent, now = _read_json(SEEN_PATH), _read_json(RECENT_PATH), time.time()
    for s in shows:
        if seen.get(s.anime_id) != s.episode:
            recent[s.anime_id] = now
    CACHE.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps({s.anime_id: s.episode for s in shows}))
    RECENT_PATH.write_text(json.dumps(recent))

    order = {s.anime_id: i for i, s in enumerate(shows)}
    return sorted(shows, key=lambda s: (-recent.get(s.anime_id, 0), -order[s.anime_id]))

def _show_keys(shows: list[Show]) -> list[str]:
    return [f"{s.anime_id}:{s.episode}:{bool(s.cover)}" for s in shows]


# ---------------------------------------------------------------------------
# Cover art (AniList)
# ---------------------------------------------------------------------------

def _cover_file(title: str) -> Path:
    return COVER_CACHE / (hashlib.md5(title.encode()).hexdigest()[:16] + ".jpg")

def _cover_path(title: str) -> str:
    p = _cover_file(title)
    return str(p) if p.exists() else ""

def _title_variants(title: str) -> list[str]:
    # ani-cli titles are long ("X 3rd Season: The Culling Game Part 1"), retry shorter
    short = re.sub(r"\s*\([^)]*\)", "", title)
    short = re.sub(r"\s(\d+(st|nd|rd|th)\s)?(Season|Part)\b.*$", "", short, flags=re.I)
    out: list[str] = []
    for t in (title, title.split(":")[0], short):
        t = t.strip()
        if t and t not in out:
            out.append(t)
    return out

@functools.lru_cache(maxsize=1)
def _ani_cli_site() -> tuple[str, str]:
    """(base url, user agent) of the site ani-cli scrapes, read from ani-cli itself
    so posters keep matching when ani-cli switches sites."""
    try:
        src = Path(shutil.which("ani-cli") or "").read_text(errors="ignore")
        base  = re.search(r'^base_api="([^"]+)"', src, re.M).group(1)
        agent = re.search(r'^agent="([^"]+)"', src, re.M).group(1)
        return base, agent
    except Exception:
        return "", "Mozilla/5.0"

def _get(url: str, agent: str = "Mozilla/5.0", timeout: int = 8) -> bytes:
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": agent}), timeout=timeout
    ) as resp:
        return resp.read()

def site_posters(query: str) -> dict[str, str]:
    """title → poster url, from the same search page ani-cli reads."""
    base, agent = _ani_cli_site()
    if not base:
        return {}
    try:
        page = _get(f"{base}/search?keyword={urllib.parse.quote_plus(query)}", agent).decode("utf-8", "ignore")
    except Exception:
        return {}
    page = page.split('id="main-sidebar"')[0]   # the top-10 sidebar repeats the markup
    posters: dict[str, str] = {}
    for item in page.split('class="flw-item')[1:]:
        src   = re.search(r'src="(https?://[^"]+)"', item)
        title = re.search(r'title="([^"]+)"', item)
        if src and title:
            posters.setdefault(html.unescape(title.group(1)), src.group(1))
    return posters

def _show_page_poster(anime_id: str) -> str:
    """Poster url from the show's own page on ani-cli's site (exact match for history)."""
    base, agent = _ani_cli_site()
    if not base or not anime_id or anime_id.startswith("search:"):
        return ""
    try:
        page = _get(f"{base}/{anime_id}", agent).decode("utf-8", "ignore")
        m = re.search(r'<img src="([^"]+)" class="film-poster-img"', page)
        return m.group(1) if m else ""
    except Exception:
        return ""

def _save_image(data: bytes, dest: Path) -> bool:
    if len(data) < 5_000:
        return False
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        try:  # webp etc. — GdkPixbuf has no loader for it here, so convert
            from PIL import Image
            out = io.BytesIO()
            Image.open(io.BytesIO(data)).convert("RGB").save(out, format="JPEG", quality=92)
            data = out.getvalue()
        except Exception:
            return False
    COVER_CACHE.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return True

def _fetch_cover(title: str, poster_url: str = "", anime_id: str = "") -> tuple[str, str]:
    """Download a cover; return (title, local_path|'').

    Order: the poster ani-cli's site shows (exact match) → AniList by title.
    """
    dest = _cover_file(title)
    if dest.exists():
        return title, str(dest)
    _base, agent = _ani_cli_site()
    for url in (poster_url, _show_page_poster(anime_id) if not poster_url else ""):
        if url:
            try:
                if _save_image(_get(url, agent), dest):
                    return title, str(dest)
            except Exception:
                pass
    query = "query($s:String){Media(search:$s,type:ANIME){coverImage{extraLarge large}}}"
    for variant in _title_variants(title):
        try:
            req = urllib.request.Request(
                ANILIST,
                data=json.dumps({"query": query, "variables": {"s": variant}}).encode(),
                headers={"Content-Type": "application/json", "Accept": "application/json",
                         "User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                media = (json.loads(resp.read()).get("data") or {}).get("Media") or {}
            url = (media.get("coverImage") or {}).get("extraLarge") or (media.get("coverImage") or {}).get("large")
            if not url:
                continue
            if _save_image(_get(url), dest):
                return title, str(dest)
        except Exception:
            continue
    return title, ""


# ---------------------------------------------------------------------------
# Handing off to ani-cli
# ---------------------------------------------------------------------------

# ani-cli switches to rofi when it has no terminal. This shim sits first on its PATH:
# - AOFI_DUMP set: aofi is only searching — save the result list to that file and
#   cancel (a lone result skips that menu, so the episode menu marks it __single__)
# - otherwise it answers the "Select anime" menu once with the show picked in aofi,
#   and passes every other menu (episodes, next/replay/quit) through to the real rofi
_ROFI_SHIM = """#!/bin/sh
prompt=""; prev=""
for a in "$@"; do [ "$prev" = "-p" ] && prompt=$a; prev=$a; done
if [ -n "$AOFI_DUMP" ]; then
    if [ "$prompt" = "Select anime: " ]; then
        cat >"$AOFI_DUMP"
    else
        cat >/dev/null
        [ -f "$AOFI_DUMP" ] || echo __single__ >"$AOFI_DUMP"
    fi
    exit 1
fi
if [ "$prompt" = "Select anime: " ] && [ -n "$AOFI_PICK" ] && [ -f "$AOFI_PICK_FLAG" ]; then
    rm -f "$AOFI_PICK_FLAG"
    input=$(cat)
    line=$(printf '%s\\n' "$input" | awk '{ t = $0; sub(/^[0-9]+ /, "", t); w = ENVIRON["AOFI_PICK"]
        if (t == w || index(t, w " - episode ") == 1) { print; exit } }')
    [ -n "$line" ] && { printf '%s\\n' "$line"; exit 0; }
    printf '%s\\n' "$input" | exec {rofi} "$@"
fi
exec {rofi} "$@"
"""

# --exit-after-play: ani-cli quits when mpv closes instead of popping up its
# next/replay menu (history is already saved, so aofi's grid has the next episode).
# ani-cli's own errors (printed red, e.g. "No results found!") become a notification;
# cancelling a menu stays quiet.
_RUN_ANI = r"""
err=$(ani-cli --exit-after-play "$@" 2>&1 >/dev/null); code=$?
rm -f "$AOFI_PICK_FLAG"
msg=$(printf '%s\n' "$err" | grep -aF "$(printf '\033[1;31m')" | sed 's/\x1b\[[0-9;]*[A-Za-z]//g; s/\r//g' | tail -n 1)
if [ "$code" -ne 0 ] && [ -n "$msg" ]; then
    case "$msg" in
        *Invalid*|*"No quality"*) ;;
        *) notify-send -a aofi "aofi" "$msg" ;;
    esac
fi
"""

def _write_rofi_shim() -> None:
    real = shutil.which("rofi")
    if not real:
        return
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    shim = BIN_DIR / "rofi"
    shim.write_text(_ROFI_SHIM.replace("{rofi}", real))
    shim.chmod(0o755)

def run_ani(args: list[str], extra_args: list[str], pick: str = "") -> None:
    flag = CACHE / f"pick-{time.time_ns()}"
    if pick:
        flag.touch()
    env = dict(
        os.environ,
        PATH=f"{BIN_DIR}:{os.environ.get('PATH', '')}",
        AOFI_PICK=pick,
        AOFI_PICK_FLAG=str(flag),
    )
    subprocess.Popen(
        ["sh", "-c", _RUN_ANI, "sh", *extra_args, *args],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )

def continue_show(show: Show, extra_args: list[str]) -> None:
    run_ani(["-c"], extra_args, pick=show.title)

def search_titles(query: str, extra_args: list[str]) -> list[str]:
    """Run ani-cli's own search headless and return its result titles.

    Using ani-cli (not AniList) keeps titles identical to what ani-cli shows,
    so play_result can pick the exact entry. [SINGLE] means one result whose
    title ani-cli never printed.
    """
    dump = CACHE / f"dump-{time.time_ns()}"
    env = dict(os.environ, PATH=f"{BIN_DIR}:{os.environ.get('PATH', '')}", AOFI_DUMP=str(dump))
    try:
        subprocess.run(
            ["ani-cli", *extra_args, query],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, timeout=40,
        )
        lines = dump.read_text(encoding="utf-8", errors="ignore").splitlines() if dump.exists() else []
    except Exception:
        lines = []
    finally:
        dump.unlink(missing_ok=True)
    if lines == ["__single__"]:
        return [SINGLE]
    return [re.sub(r"^\d+ ", "", l) for l in lines if l.strip()]

SINGLE = "\0single"

def play_result(query: str, title: str, extra_args: list[str]) -> None:
    run_ani([query], extra_args, pick="" if title == SINGLE else title)


# ---------------------------------------------------------------------------
# GTK launcher UI
# ---------------------------------------------------------------------------

_CSS = b"""
window {
    background-color: rgba(0, 0, 0, 0.7);
    border: 2px solid #ffffff;
}
scrolledwindow, viewport {
    background-color: transparent;
    border: none;
}
scrollbar {
    opacity: 0;
    min-width: 0;
    min-height: 0;
}
overshoot {
    background: none;
    box-shadow: none;
}
entry {
    background-color: transparent;
    background-image: none;
    color: #ffffff;
    caret-color: #ffffff;
    font-weight: bold;
    border: none;
    border-bottom: 2px solid #ffffff;
    border-radius: 0;
    box-shadow: none;
    padding: 9px 8px;   /* 1px more than rofi: GTK draws the text line 2px shorter */
}
entry selection {
    background-color: #ffffff;
    color: #000000;
}
flowbox {
    background-color: transparent;
    padding: 0;
}
flowboxchild {
    background-color: rgba(0, 0, 0, 0);
    border: none;
    border-radius: 0;
    padding: 8px;
    margin: 0;
    transition: background-color 80ms;
}
flowboxchild:hover {
    background-color: #ffffff;
}
flowboxchild:hover .game-title {
    color: #000000;
}
flowboxchild:hover .ep-label {
    color: rgba(0, 0, 0, 0.6);
}
flowboxchild:selected {
    background-color: #ffffff;
    border: none;
    border-radius: 0;
    outline: none;
}
flowboxchild:selected .game-title {
    color: #000000;
}
flowboxchild:selected .ep-label {
    color: rgba(0, 0, 0, 0.6);
}
.game-title {
    color: #ffffff;
    font-weight: bold;
    padding: 6px 4px 2px 4px;
}
.ep-label {
    color: rgba(255, 255, 255, 0.6);
    padding: 0 4px 2px 4px;
}
.empty-label {
    color: rgba(255, 255, 255, 0.6);
    font-weight: bold;
}
"""

def _rofi_font() -> tuple[str, str]:
    """(family, size) of rofi's font, e.g. ("JetBrains Mono", "12"), so the
    launchers match rofi; display.font in config.toml overrides it."""
    try:
        text = (Path.home() / ".config/rofi/config.rasi").read_text()
        spec = re.search(r'^\s*font:\s*"([^"]+)"', text, re.M).group(1)
    except Exception:
        return "", ""
    family, _, size = spec.rpartition(" ")
    return (family, size) if size.replace(".", "", 1).isdigit() else (spec, "")

def _font_spec(display_cfg: dict) -> tuple[str, str]:
    spec = display_cfg.get("font")
    if not spec:
        return _rofi_font()
    family, _, size = spec.rpartition(" ")
    return (family, size) if size.replace(".", "", 1).isdigit() else (spec, "")

def _fetch_covers_bg(pending: list[tuple], icon_size: int, card_ratio: float) -> None:
    """Fetch missing covers in a background thread and push updates to the UI."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_cover, show.title, show.poster, show.anime_id): img
                   for show, img in pending}
        for fut in as_completed(futures):
            img_widget = futures[fut]
            _title, path = fut.result()
            if path:
                def _update(iw=img_widget, p=path):
                    pb = _load_pixbuf(p, icon_size, card_ratio)
                    if pb:
                        iw.set_from_pixbuf(pb)
                    return False
                GLib.idle_add(_update)


def _load_pixbuf(path: str, size: int, ratio: float = 1.5) -> GdkPixbuf.Pixbuf | None:
    try:
        pb   = GdkPixbuf.Pixbuf.new_from_file(path)
        sw, sh = pb.get_width(), pb.get_height()
        th   = int(size * ratio)
        # scale to fill, then crop to center
        scale  = max(size / sw, th / sh)
        bw, bh = int(sw * scale), int(sh * scale)
        pb     = pb.scale_simple(bw, bh, GdkPixbuf.InterpType.BILINEAR)
        pb     = pb.new_subpixbuf((bw - size) // 2, (bh - th) // 2, size, th)
        return pb
    except Exception:
        return None


def _fallback_pixbuf(icon_name: str, size: int) -> GdkPixbuf.Pixbuf | None:
    theme = Gtk.IconTheme.get_default()
    try:
        return theme.load_icon(icon_name, size, Gtk.IconLookupFlags.FORCE_SIZE)
    except Exception:
        return None


def run_gui(display_cfg: dict, extra_args: list[str], daemon: bool = False) -> None:
    columns       = display_cfg.get("columns", 3)
    window_width  = display_cfg.get("window_width", 900)
    window_height = display_cfg.get("window_height", 700)
    card_ratio    = display_cfg.get("card_ratio", 1.5)
    spacing       = display_cfg.get("spacing", 4)
    padding       = display_cfg.get("padding", 12)
    icon_size     = (window_width - 2 * padding - (columns - 1) * spacing) // columns - 16
    font_size     = display_cfg.get("font_size", 13)

    current_shows: list[Show] = []

    css = Gtk.CssProvider()
    font_family, entry_size = _font_spec(display_cfg)
    extra_css = (
        f".game-title {{ font-size: {font_size}px; }}"
        f".ep-label {{ font-size: {max(font_size - 2, 8)}px; }}"
        f".empty-label {{ font-size: {font_size + 1}px; }}"
        # the search bar mirrors rofi's input bar, so it uses rofi's size too
        f"entry {{ font-size: {entry_size + 'pt' if entry_size else f'{font_size + 1}px'}; }}"
    )
    if font_family:
        extra_css += f' * {{ font-family: "{font_family}"; }}'
    css.load_from_data(_CSS + extra_css.encode())
    Gtk.StyleContext.add_provider_for_screen(
        Gdk.Screen.get_default(), css,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )

    GLib.set_prgname("aofi")

    win = Gtk.Window(title="aofi")
    win.set_decorated(False)
    screen = win.get_screen()
    win.set_visual(screen.get_rgba_visual() or screen.get_system_visual())
    win.set_default_size(window_width, window_height)
    win.set_size_request(window_width, window_height)

    GtkLayerShell.init_for_window(win)
    GtkLayerShell.set_layer(win, GtkLayerShell.Layer.OVERLAY)
    GtkLayerShell.set_keyboard_mode(win, GtkLayerShell.KeyboardMode.EXCLUSIVE)

    # Without window_x/window_y nothing is anchored, so the compositor centers the
    # window in the space bars (waybar) leave free — exactly where rofi puts itself
    window_x = display_cfg.get("window_x")
    window_y = display_cfg.get("window_y")
    if window_x is not None or window_y is not None:
        display  = Gdk.Display.get_default()
        monitor  = display.get_primary_monitor() or display.get_monitor(0)
        geo      = monitor.get_geometry()
        margin_x = window_x if window_x is not None else (geo.width  - window_width)  // 2
        margin_y = window_y if window_y is not None else (geo.height - window_height) // 2
        GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.LEFT, True)
        GtkLayerShell.set_anchor(win, GtkLayerShell.Edge.TOP,  True)
        GtkLayerShell.set_margin(win, GtkLayerShell.Edge.LEFT, margin_x)
        GtkLayerShell.set_margin(win, GtkLayerShell.Edge.TOP,  margin_y)

    # like rofi: the search bar runs edge to edge at the top, the grid is padded below it
    vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    win.add(vbox)

    entry = Gtk.Entry()
    entry.set_has_frame(False)
    vbox.pack_start(entry, False, False, 0)

    stack = Gtk.Stack()
    stack.set_margin_start(padding)
    stack.set_margin_end(padding)
    stack.set_margin_top(padding)
    stack.set_margin_bottom(padding)
    vbox.pack_start(stack, True, True, 0)

    empty = Gtk.Label(label="No history yet — type to search")
    empty.get_style_context().add_class("empty-label")
    stack.add_named(empty, "empty")

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_kinetic_scrolling(False)
    stack.add_named(scroll, "grid")

    flow = Gtk.FlowBox()
    flow.set_max_children_per_line(columns)
    flow.set_min_children_per_line(columns)
    flow.set_selection_mode(Gtk.SelectionMode.SINGLE)
    flow.set_homogeneous(True)
    flow.set_column_spacing(spacing)
    flow.set_row_spacing(spacing)
    flow.set_valign(Gtk.Align.START)
    scroll.add(flow)

    card_w = icon_size
    card_h = int(icon_size * card_ratio) + 56

    def _populate(new_shows: list[Show]) -> bool:
        stack.set_visible_child_name("grid" if new_shows else "empty")
        if _show_keys(new_shows) == _show_keys(current_shows):
            return False
        current_shows[:] = new_shows
        for child in flow.get_children():
            child.destroy()
        pending: list[tuple] = []
        for show in new_shows:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            box.set_size_request(card_w, card_h)
            box.set_halign(Gtk.Align.CENTER)

            pb = _load_pixbuf(show.cover, icon_size, card_ratio) if show.cover else None
            if pb is None:
                pb = _fallback_pixbuf("video-x-generic", icon_size)
            if pb:
                img = Gtk.Image.new_from_pixbuf(pb)
            else:
                img = Gtk.Image.new_from_icon_name("video-x-generic", Gtk.IconSize.DIALOG)
            img.set_halign(Gtk.Align.CENTER)
            box.pack_start(img, False, False, 0)
            if not show.cover:
                pending.append((show, img))

            lbl = Gtk.Label(label=show.title)
            lbl.set_line_wrap(True)
            lbl.set_max_width_chars(18)
            lbl.set_lines(2)
            lbl.set_ellipsize(3)  # Pango.EllipsizeMode.END
            lbl.set_halign(Gtk.Align.CENTER)
            lbl.set_valign(Gtk.Align.CENTER)
            lbl.set_justify(Gtk.Justification.CENTER)
            lbl.get_style_context().add_class("game-title")
            box.pack_start(lbl, True, True, 0)

            if show.episode:
                ep = Gtk.Label(label=f"Episode {show.episode} watched")
                ep.set_halign(Gtk.Align.CENTER)
                ep.get_style_context().add_class("ep-label")
                box.pack_start(ep, False, False, 0)

            flow.add(box)

        flow.show_all()
        if pending:
            threading.Thread(
                target=_fetch_covers_bg,
                args=(pending, icon_size, card_ratio),
                daemon=True,
            ).start()
        return False

    def _close():
        if daemon:
            _VISIBLE_FILE.unlink(missing_ok=True)
            win.hide()
            flow.unselect_all()
        else:
            win.destroy()
            Gtk.main_quit()

    # what the grid shows: history, or the results for state["query"]
    state = {"query": "", "token": 0}

    def _continue_child(child):
        show = current_shows[child.get_index()]
        _close()
        if state["query"]:
            play_result(state["query"], show.pick, extra_args)
        else:
            continue_show(show, extra_args)

    def _show_message(text: str) -> None:
        empty.set_text(text)
        stack.set_visible_child_name("empty")

    def _show_history():
        state["query"] = ""
        state["token"] += 1   # drop any search still running
        empty.set_text("No history yet — type to search")
        _populate(load_history())

    # Like rofi, keyboard focus never leaves the search bar: arrows only move the
    # highlight in the grid, so the bar keeps its cursor and typing just works.
    def _select(index: int) -> None:
        child = flow.get_child_at_index(index)
        if not child:
            return
        flow.select_child(child)
        # the grid never has focus, so scroll the highlight into view by hand
        alloc = child.get_allocation()
        adj   = scroll.get_vadjustment()
        if alloc.y < adj.get_value():
            adj.set_value(alloc.y)
        elif alloc.y + alloc.height > adj.get_value() + adj.get_page_size():
            adj.set_value(alloc.y + alloc.height - adj.get_page_size())

    def _reset_view():
        entry.set_text("")
        _show_history()
        entry.grab_focus()
        # like rofi: first card highlighted while typing still goes to the search bar
        _select(0)

    def _on_results(token: int, query: str, titles: list[str], posters: dict) -> bool:
        if token != state["token"]:
            return False  # a newer search or a reset happened meanwhile
        if not titles:
            _show_message(f"No results for “{query}”")
            return False
        state["query"] = query
        shows = []
        for t in titles:
            display = query if t == SINGLE else t
            poster = posters.get(display) or (next(iter(posters.values()), "") if t == SINGLE else "")
            shows.append(Show(title=display, anime_id=f"search:{display}", episode="",
                              cover=_cover_path(display), pick=t, poster=poster))
        _populate(shows)
        _select(0)
        return False

    def _search(query: str) -> None:
        state["token"] += 1
        token = state["token"]
        _show_message("Searching…")

        def work():
            with ThreadPoolExecutor(max_workers=2) as pool:
                posters = pool.submit(site_posters, query)
                titles  = search_titles(query, extra_args)
                GLib.idle_add(_on_results, token, query, titles, posters.result())
        threading.Thread(target=work, daemon=True).start()

    def on_entry_activate(_entry):
        query = entry.get_text().strip()
        sel = flow.get_selected_children()
        # Enter plays the highlighted card; with none highlighted it searches
        # (typing a new search clears the highlight, arrows bring one back)
        if sel:
            _continue_child(sel[0])
        elif query:
            _search(query)

    def on_entry_changed(_entry):
        text = entry.get_text().strip()
        if not text:
            if state["query"] or stack.get_visible_child_name() == "empty":
                _show_history()
            _select(0)
        elif text != state["query"]:
            flow.unselect_all()   # Enter will search, so nothing should look picked

    def on_flow_button_press(_widget, event):
        child = flow.get_child_at_pos(int(event.x), int(event.y))
        if child is None:
            return False
        selected = flow.get_selected_children()
        if selected and selected[0] == child:
            _continue_child(child)
        else:
            _select(child.get_index())
        return True

    def on_key_press(widget, event):
        key = event.keyval
        if key == Gdk.KEY_Escape:
            # from results (or a pending search) go back to history first
            if state["query"] or entry.get_text():
                _reset_view()
            else:
                _close()
            return True
        if key in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            return True   # focus stays in the search bar
        moves = {Gdk.KEY_Left: -1, Gdk.KEY_Right: 1, Gdk.KEY_Up: -columns, Gdk.KEY_Down: columns}
        if key in moves and current_shows and stack.get_visible_child_name() == "grid":
            text = entry.get_text().strip()
            if key in (Gdk.KEY_Left, Gdk.KEY_Right) and text and text != state["query"]:
                return False  # typing a search: left/right move the text cursor
            sel = flow.get_selected_children()
            if not sel:
                _select(0)
            else:
                target = sel[0].get_index() + moves[key]
                if 0 <= target < len(current_shows):
                    _select(target)
                elif key == Gdk.KEY_Down:
                    _select(len(current_shows) - 1)   # partial last row
            return True
        if not entry.has_focus():
            entry.grab_focus_without_selecting()
            entry.set_position(-1)
        return False  # default handler delivers the key to the search bar

    entry.connect("activate", on_entry_activate)
    entry.connect("changed", on_entry_changed)
    flow.set_activate_on_single_click(False)
    flow.connect("button-press-event", on_flow_button_press)
    win.connect("key-press-event", on_key_press)
    win.connect("destroy", Gtk.main_quit)

    _populate(load_history())

    if daemon:
        def _toggle():
            if win.get_visible():
                _close()
            else:
                _VISIBLE_FILE.touch()
                win.show_all()
                win.present()
                _reset_view()   # always open on history

        def _poll_signal():
            if _show_pending.is_set():
                _show_pending.clear()
                _toggle()
            return True  # keep polling

        signal.signal(signal.SIGUSR1, lambda *_: _show_pending.set())
        GLib.timeout_add(100, _poll_signal)
        win.realize()
    else:
        win.show_all()
        stack.set_visible_child_name("grid" if current_shows else "empty")
        _reset_view()

    Gtk.main()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    daemon_mode = "--daemon" in sys.argv

    if daemon_mode:
        # Register immediately so SIGUSR1 doesn't kill us during the slow startup
        signal.signal(signal.SIGUSR1, lambda *_: _show_pending.set())

    if not _acquire_lock():
        return  # another instance is already running

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(DEFAULT_CONFIG)
        print(f"Created config: {CONFIG_PATH}")

    with open(CONFIG_PATH, "rb") as f:
        cfg = tomllib.load(f)

    display    = cfg.get("display", {})
    extra_args = [str(a) for a in cfg.get("ani_cli", {}).get("args", [])]

    CACHE.mkdir(parents=True, exist_ok=True)
    for stale in CACHE.glob("pick-*"):
        stale.unlink(missing_ok=True)
    _write_rofi_shim()

    run_gui(display, extra_args, daemon=daemon_mode)


if __name__ == "__main__":
    main()
