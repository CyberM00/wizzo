# BMS Kneeboard

A second-monitor kneeboard for Falcon BMS. It reads the briefing BMS exports,
pulls the matching approach plates and theatre maps out of your BMS install, and
turns your loadout into an employment reference — automatically, every time you
commit to a new mission.

Runs as a small local web server, so the same board also opens on a tablet or
phone over your LAN.

## Running it

```bash
pip install -r requirements.txt
```

```bash
python kneeboard.py
```

Then drag the browser window to your second monitor and press **F11**.

On Windows you can also just double-click **`start-kneeboard.bat`**.

The BMS install is found automatically from the registry. If that fails, point at
it explicitly:

```bash
python kneeboard.py --bms-path "D:\Falcon BMS 4.38"
```

Other options: `--port` (default 5000), `--host`, `--no-browser`, `--no-update`.

## Keeping it up to date

The board updates itself. On every start it checks GitHub, fast-forwards the
working copy if there is something new, and relaunches on the updated version
before serving anything. Push a change from anywhere — including Claude Code on
web from a phone — and the next launch on the desktop picks it up.

The current version and commit are shown at the bottom of the sidebar.

This only works if the folder is a **git clone**. If you copied the files rather
than cloning, there is no remote to update from; the sidebar will say
`not auto-updating`.

Updating is deliberately cautious and never destructive. It is skipped, with the
reason printed, when:

- there are uncommitted changes in the working copy — your edits are never touched
- there are local commits that were not pushed, so a fast-forward is impossible
- GitHub is unreachable — the board runs on what it has

Only fast-forward pulls are performed, so an update cannot rewrite history,
discard a change, or leave a merge conflict behind. Skip the check entirely with
`--no-update`.

If an update changes `requirements.txt`, the console says so and you should run
`pip install -r requirements.txt` again.

## Pages

| Page | Contents |
|---|---|
| **Brief** | Flight, package, time on target, target, situation, package elements, roster, ROE, emergency procedures |
| **Loadout** | Your stores with weights, missile ranges and expandable employment detail; laser-code panel |
| **Steer** | Full steerpoint table — time, distance, heading, CAS, altitude, action, formation |
| **Comms** | Comm ladder alongside the actual UHF/VHF preset table, plus IFF and Link 16 |
| **Threats** | Air and surface threat analysis, support assets with TACAN channels |
| **Weather** | Conditions at takeoff, target and landing |
| **Charts** | Approach plates auto-selected for your departure, recovery, alternate and target fields; all 89 KTO airfields browsable |
| **Maps** | Theatre maps with drag-to-pan and scroll-to-zoom |

Keys `1`–`8` switch pages. `R` forces a reload.

## How it updates

The board polls the modification time of `briefing.txt` every two seconds and
rebuilds when BMS rewrites it. Commit to a new mission and the board follows —
nothing to click.

## Where the data comes from

Everything is read live from your own install; nothing is bundled or cached.

| Data | Source |
|---|---|
| Mission brief, steerpoints, comms, IFF, Link 16, weather, ordnance | `User\Briefings\briefing.txt` |
| UHF/VHF radio presets | `User\Briefings\dtc_comm.txt` |
| Weapon weights and missile ranges | `Data\TerrData\Objects\Falcon4_WCD.xml` |
| Approach plates and airfield diagrams | `Docs\03 KTO Charts\` |
| Theatre maps | `Docs\05 Maps\` |
| Employment guidance, fuzing, laser applicability | `bmskb/data/f16_stores.json` (curated) |

Charts are matched to your mission by ICAO code and airfield name — the
departure, recovery and alternate fields come from the comm ladder and emergency
procedures, and the target field from the package mission line.

## Things worth knowing

These are deliberate choices, not gaps waiting to be filled.

**Laser codes are entered by hand.** BMS sets them in the in-game DTC and does
not write them to any exported file, so there is nothing to read. The laser panel
is a reminder of what you set, validated against BMS's rules (`1xyz`, where each
of `x`, `y`, `z` is 1–8). It does not read the jet. The panel highlights itself
when your loadout actually contains a laser-guided weapon.

**Missile ranges are game data; bomb ranges are not shown.** `Falcon4_WCD.xml`
holds a usable maximum range for missiles, which is displayed. For bombs it holds
a campaign-engine placeholder of 0–2 that has nothing to do with release range,
so it is suppressed rather than shown as a misleading figure. Bomb delivery
guidance comes from the curated library instead.

**Guidance types are not derived from the game files.** WCD stores guidance as an
integer that conflates ballistic bombs and GPS JDAMs under the same value, so any
label built from it would be wrong. Guidance descriptions come from the curated
library.

**IFF rotation rows are shown as BMS writes them.** BMS does not always emit the
same number of values per row, so the rows are not zipped into aligned columns —
pairing a code against the wrong time block is worse than reading across a row.

**Radio presets are checked for staleness.** `dtc_comm.txt` is written
independently of `briefing.txt`. If the two timestamps disagree by more than five
minutes the board says so, because presets from a previous mission are worse than
none.

**Tank weights are dry weights.** The stores total is airframe-external weight
from the game files, not a fuel load.

The curated employment notes are planning guidance drawn from the BMS manuals and
standard Viper procedures. They are a reference, not certified release tables.

## Layout

```
kneeboard.py              entry point and Flask routes
start-kneeboard.bat       double-click launcher
bmskb/
  install.py              BMS discovery (registry, env var, drive scan) and encoding handling
  briefing.py             briefing.txt parser
  dtc.py                  dtc_comm.txt radio preset parser
  weapons.py              WCD weapon data joined to the curated library
  charts.py               chart and map indexing, ICAO matching
  state.py                payload assembly, caching, change detection, validation
  selfupdate.py           safe fast-forward self-update on startup
  data/f16_stores.json    curated F-16 store reference
templates/index.html      page shell
static/css, static/js     styling and renderer
```

Only dependency is Flask.

## Theatres other than Korea

The briefing, loadout, comms and weather pages are theatre-independent. The chart
and map pages read whatever is in your `Docs` tree, matching on ICAO and airfield
name, so a theatre that follows the same `Docs` layout works without changes.
