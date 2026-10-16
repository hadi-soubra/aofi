# aofi

A Wayland-native GTK overlay anime launcher for [ani-cli](https://github.com/pystardust/ani-cli), in the style of [hofi](https://github.com/hadi-soubra/hofi) and rofi.

Opens on your ani-cli watch history as a cover grid. Pick a show to continue at the next episode, or type a search and the grid turns into the results.

## Features

- **Continue watching** — your ani-cli history as a cover grid, most recently watched first, with the last watched episode under each show
- **Search in place** — type + `Enter` swaps the grid for ani-cli's search results; pick one to play it
- **Exact covers** — posters come from the same site ani-cli streams from (read from ani-cli itself, so they follow when ani-cli switches sites), falling back to [AniList](https://anilist.co)
- **Out of the way** — aofi closes as soon as an anime starts; ani-cli's episode picker shows up as a rofi popup, and closing mpv ends playback (the next episode is waiting in the grid)
- Errors like "No results found" arrive as a notification
- Looks like rofi: same size, position and font (read from `~/.config/rofi/config.rasi`), keyboard focus stays in the search bar while the arrows move the highlight
- Single-instance lock via `flock`, daemon mode for instant show/hide

## Requirements

- Python 3.11+
- GTK 3
- `gtk-layer-shell` (Wayland compositor with layer-shell support required)
- `pygobject`
- [ani-cli](https://github.com/pystardust/ani-cli), `mpv`, `rofi`
- `notify-send` (optional — error notifications)
- `Pillow` (optional — converts WebP posters)

On Arch Linux:

```bash
sudo pacman -S python-gobject gtk3 gtk-layer-shell python-pillow ani-cli mpv rofi libnotify
```

## Installation

```bash
git clone https://github.com/hadi-soubra/aofi.git
cd aofi
python aofi.py
```

A `config.toml` is created automatically on first run with sensible defaults.

## Configuration

`config.toml` lives next to `aofi.py`.

```toml
[display]
columns       = 3
window_width  = 600
window_height = 400
card_ratio    = 1.45   # height/width ratio of each cover card
spacing       = 10
padding       = 12
font_size     = 13     # card titles
# font = "JetBrains Mono 12"   # default: the font from ~/.config/rofi/config.rasi
# window_x / window_y pin the window; leave them out to center it like rofi

[ani_cli]
# extra flags for every ani-cli run, e.g. ["--dub"], ["--skip"], ["-q", "1080p"]
args = []
```

## Keybindings

| Key | Action |
|-----|--------|
| Typing | Search bar |
| Arrow keys | Move the highlight |
| `Enter` | Play the highlighted show, or search when nothing is highlighted |
| `Escape` | Back to history from results, otherwise close |
| Click | Select / click again to play |

## Daemon mode

```bash
python3 aofi.py --daemon   # start hidden, e.g. from your compositor's autostart
kill -USR1 <pid>           # toggle show/hide
```

hofi's `launcher.sh` cycles rofi → hofi → aofi → rofi on each call, handy as a single hotkey or gesture binding.

## How it works

aofi never streams anything itself. It hands off to ani-cli, which switches to rofi menus when it has no terminal. A small rofi shim on ani-cli's `PATH` answers the "Select anime" menu with the show picked in aofi (and, for searches, quietly captures the result list instead of showing it). Every other menu goes to the real rofi.

## License

MIT
