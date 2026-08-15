# BMS Kneeboard

A second-monitor kneeboard for **Falcon BMS**, **DCS World** and **IL-2 Sturmovik:
Great Battles**. It reads what the sim already writes, pulls in the matching
charts, and turns your loadout into an employment reference — automatically, every
time you take a new mission.

Runs as a small local web server, so the same board also opens on a tablet or
phone over your LAN.

## Which sim it shows

The board opens on a **sim chooser**: one card per sim showing whether it was
found, which mission it would read, when that was written, and which is newest.
Click one and it pins the board to that sim and goes straight to the brief. `H`
returns to the chooser at any time; the board pages keep their `1`–`8` shortcuts.

The chooser reads file stats only, never parsing a mission, so opening it is
instant regardless of how many sims you have installed.

If you would rather the board follow whichever sim wrote a mission most recently,
press the button below the nav until it reads `(auto)`. It cycles
auto → BMS → DCS → IL-2.

The three sims expose very different amounts of data, so what each page shows
differs. See [DCS support](#dcs-support) and [IL-2 support](#il-2-support) for
exactly what carries over.

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

DCS and IL-2 are found the same way, and can be pointed at with `--dcs-path` and
`--il2-path`.

Other options: `--port` (default 5000), `--host`, `--no-browser`, `--no-update`,
`--check-update` (report whether an update is waiting, then exit).

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
| **Home** | The sim chooser — status, current mission and timestamp for each sim |
| **Brief** | Flight, package, time on target, target, situation, package elements, roster, ROE, emergency procedures |
| **Aircraft** | IL-2: performance limits, engine modes, temperature limits and recommended control settings, from the sim's own data. BMS and DCS: their aircraft manuals, matched to your airframe |
| **Loadout** | Your stores with weights, missile ranges and expandable employment detail; laser-code panel |
| **Steer** | Full steerpoint table — time, distance, heading, CAS, altitude, action, formation |
| **Comms** | Comm ladder alongside the actual UHF/VHF preset table, plus IFF and Link 16 |
| **Threats** | Air and surface threat analysis, support assets with TACAN channels |
| **Weather** | Conditions at takeoff, target and landing |
| **Charts** | Approach plates auto-selected for your departure, recovery, alternate and target fields; all 89 KTO airfields browsable |
| **Maps** | BMS: theatre maps. DCS: the sim's own raster aeronautical chart for your terrain, cropped to the mission, with your route, waypoints, airfields and bullseye on it. IL-2: the sim's own planner map with your route, waypoints and airfields drawn on it |

Keys `1`–`9` switch pages, `H` opens the sim chooser. `R` forces a reload. `T`
switches theme.

On a portrait screen the nav moves to the top so the full width goes to content,
the stat grids drop to two columns, and the chart viewer takes most of the
remaining height — approach plates are portrait, so a tall screen suits them.

## Themes

Two palettes, toggled with the button under the nav or the `T` key:

- **Amber night** — the default dark cockpit scheme.
- **Paper day** — a light scheme for a lit room. Worth switching to when you are
  reading charts: the approach plates are white PDFs, so on the dark theme every
  chart is a bright rectangle in a dark frame.

The choice is stored per device in the browser, so a second monitor in a dark
room and a tablet in a lit one can each keep their own. It is applied before the
stylesheet paints, so a night-theme board never flashes white on load.

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

## DCS support

DCS exports nothing for external tools — there is no equivalent of BMS's
`briefing.txt`. Everything comes out of the mission `.miz`, which is a zip
containing the mission as a Lua table.

| Page | What DCS gives you |
|---|---|
| **Brief** | Mission briefing text, airframe, task, theatre, date, start time, time on target, plus fuel, flares, chaff and gun state |
| **Loadout** | Every station with the store on it and full employment detail for stores in the curated library |
| **Steer** | The full route: waypoint names, altitudes, speeds, ETAs, and computed leg distance and bearing |
| **Comms** | The aircraft's programmed preset channels, one block per radio, plus tanker and AWACS frequencies |
| **Weather** | Cloud base and thickness, wind at three altitudes, visibility, temperature, QNH |
| **Charts** | Kneeboard pages the mission generator embedded in the `.miz` (Retribution adds these; stock missions usually do not) |
| **Threats** | Nothing — see below |
| **Maps** | The terrain's own raster aeronautical chart, cropped to your mission, with the route, numbered waypoints, airfields, your start and bullseye drawn on it |

### The theatre chart

DCS ships real 1:500,000-style aeronautical charts for each terrain — relief, spot
elevations, airspace boundaries, labelled coastlines — as DXT-compressed tiles in
`Mods\terrains\<terrain>\RasterCharts`. Two things are needed to put a flight plan
on them, and the terrain files state both.

**Where each tile sits.** `rasterCharts.sup5` is the scene index for the tiles:
fixed 344-byte records, each carrying a world bounding box as six floats followed
by the tile's name. Every record in all three installed terrains parses, and the
boxes agree with a regular grid to the metre. This is read rather than inferred
for a reason — Syria's sheets happen to sit on a tidy 262,144 m grid from the
origin and Caucasus's do not, so anything derived from the tile names would have
been right on one terrain and wrong on another.

**How mission coordinates map to the world.** `beacons.lua` lists every beacon
twice over: `position` in world metres and `positionGeo` in latitude and
longitude. That is enough to solve the projection outright. DCS uses Transverse
Mercator on WGS 84 at the UTM scale factor 0.9996, with the central meridian of
the terrain's own UTM zone and a per-terrain false origin:

| Terrain | Central meridian | False northing | False easting | Beacons | Worst residual |
|---|---|---|---|---|---|
| Syria | 39°E | −3,879,866 | +282,801 | 151 | 0.07 m |
| Caucasus | 33°E | −4,998,115 | −99,517 | 164 | 0.79 m |
| Persian Gulf | 57°E | −2,894,933 | +75,756 | 101 | 0.11 m |

Sub-metre against the sim's own figures, with round-integer offsets and meridians
exactly on the UTM zone — an exact match rather than a fit. Nothing is hardcoded:
each terrain is solved from its own beacon table on load, so a terrain never seen
here works the same way, and the residual is reported on the page.

The whole theatre is never built. Syria at 32 m per pixel would be 390 megapixels,
so only the tiles covering the route's bounding box plus a 30 km margin are
stitched, at the finest resolution that covers the box without going over 44
megapixels. A typical sortie comes out around 5,000 px square, built in about a
second and cached in a gitignored `dcs_map_cache/` keyed on the game files' own
modification times. Stitching never happens while the board is being assembled —
only when the image is actually requested — so `/api/state` stays at 5–20 ms warm.

**Placement is checked, not assumed.** Every terrain also ships
`MissionGenerator\nodesMap.png`, a land-and-water image exactly georeferenced by
the bounds beside it. After a chart is stitched, its coastline is compared against
that image where it is placed and at eight positions 20–35 km away, and the result
is reported on the page. If a displaced position ever wins, the chart is not shown
again — the terrain outline is drawn instead and the page says why.

What is *not* done is score the overlap against a fixed threshold. The terrains
draw their seas quite differently — Syria's is a solid blue, Caucasus's is nearly
white — so the same colour test finds 52% of one and 12% of another, and a fixed
threshold rejected a Caucasus chart that visibly lands on its own printed airfield
symbols. Which position wins is a question that survives a mediocre colour test,
because both sides of the comparison share it. Measured over twelve coastal areas
across the three terrains, the stated position won every time.

**When there is no chart.** A terrain with no `RasterCharts` folder, no tile index,
or no tiles covering the route falls back to that same land-and-water image,
enlarged, with a latitude and longitude graticule, named places from the terrain's
own `towns.lua`, a scale bar, and bearing and range from bullseye tabulated for
every steerpoint. The page says which of the two it is showing and why.

### What DCS deliberately does not show, and why

**Weapon names come from a curated library, not the game files.** DCS builds its
own CLSID-to-name mapping by *executing* Lua at load time, so there is no table
to read. Three extraction approaches were tried and all rejected: proximity
matching reached 44% and mislabelled an ALQ-184 as a Soviet recon pod;
enclosing-block matching reached 35% with conflicting answers; literal
declaration arguments were accurate but covered only a handful. So names are
hand-curated for the **F/A-18C, A-10C and AV-8B N/A**. Anything outside that is
shown as its raw CLSID and flagged as having no reference data — a wrong weapon
name on a kneeboard is worse than an unresolved code.

**No weights or ranges.** `Falcon4_WCD.xml` gives BMS trustworthy per-store
weights and missile ranges. DCS publishes no equivalent, so those fields stay
empty rather than being invented.

**No threat picture.** A DCS mission lists every unit on the map, but nothing
marks which are a threat to your route. Building a threat brief from that would
be invention, not reading.

**Only the airfields the terrain names are labelled on the map.** Airfield
positions and names come from the `AIRPORT_HOMER` entries in `beacons.lua`, which
covers 23 of Syria's 76 airfields. The rest exist only in `AirfieldsTaxiways`, as
per-airfield binary road-network files whose format is not established here, so
they are left off rather than drawn as unnamed squares in guessed positions. The
chart itself prints every airfield regardless — the overlay adds names to the ones
the sim states, and does not invent the others.

**The chart is shown without a coordinate grid.** In chart mode no graticule or
place names are drawn, because the chart carries its own and a second set on top
of them would be two grids to read. The graticule appears only in the fallback,
where there is no printed one.

**Wind is reported as the direction it comes from.** DCS stores the direction the
wind blows *toward*, rotated 180° from the convention the Mission Editor
displays. The board converts it and says "from" in the value so the convention is
explicit rather than assumed.

**Only tanker and AWACS groups are listed as support.** DCS group tasks do not
reliably describe what a generated group does — a BARCAP in the test mission was
tagged `Transport`. Labelling the rest would be guesswork.

## IL-2 support

IL-2 exports nothing and has no telemetry of any kind — no shared memory, no UDP
export. Everything comes from files it leaves on disk.

Career mode is fully supported. Scripted DLC campaigns are partially supported;
see below.

| Page | What IL-2 gives you |
|---|---|
| **Brief** | Mission title, flight callsign ("Finch 4"), airframe, date, takeoff time, theatre, the complete mission briefing, your flight's pilot roster, and spawn fuel and ammunition |
| **Loadout** | Every store with the game's own name, and a planned-versus-as-flown comparison |
| **Steer** | The route: waypoint names, altitudes, commanded speeds, formations, plus computed leg distance and bearing |
| **Comms** | Callsigns only — see below |
| **Weather** | Five wind layers, cloud base and thickness, cloud preset, temperature, pressure, haze, turbulence, sea state. The richest weather of the three sims |
| **Charts** | Taxi diagrams for your departure and recovery fields, drawn from the mission's own coordinates |
| **Threats** | Nothing — see below |
| **Maps** | Nothing — IL-2 keeps terrain as packed data, not images |

### Where the loadout comes from

Unusually, IL-2's own weapon tables are readable, so store names come from the
game itself rather than a curated file. The mission's `PayloadId` is an index into
the aircraft's ammunition list inside `Scripts.gtp`, and the weapon codes in that
list resolve against the name table in `Swf.gtp`. Both are read directly and cached
in a gitignored `il2_name_cache.json`, keyed on the archives' size and modification
time so a game patch rebuilds it automatically. The build takes about 0.15 seconds
and reads roughly 8 MB of the 1.6 GB involved.

Coverage is 99.3% of every loadout string in the game; three drop-tank codes have
no entry in IL-2's own table and are shown as raw codes.

### Planned versus as flown

**IL-2 never writes your chosen loadout back to the mission file.** One career
mission here was reused across five sorties with different loadouts each time. The
mission file therefore holds only what the generator planned.

What you actually took off with is recorded in `data\missionReport(...)[0].txt`,
which IL-2 writes when a mission starts. The board reads it, checks it really does
belong to the mission on disk (four tests: the mission it names, its date, its
time, and that it is not older than the mission file), and says which source it is
showing. When they disagree, it says so and shows what you flew.

This depends on `mission_text_log = 1` in `data\startup.cfg`, which is on by
default. When it is off the board says so rather than presenting the planned
loadout as fact.

### What IL-2 deliberately does not show, and why

**No weapon modifications or gun round counts.** The mission file writes the
modification bitmask as base-2 digits and the log writes the same value in
decimal, but which end the digit string starts from is not confirmed — and bit 0
is set either way, so a wrong reading would look entirely plausible while
mislabelling every modification. Likewise, a payload label like `SHKAS-AP-1500`
carries a number, but whether it is per-gun or a total for the pair is not
established. Both are extracted and cached, so surfacing them later is a display
change; neither is displayed on a guess.

**Wind direction is stated without a convention.** The mission file gives a
direction per altitude layer, but whether it is the direction the wind comes from
or blows toward is not confirmed, so it is shown as the file states it.

**No frequencies.** IL-2 aircraft of this era have no tunable radio and the mission
files carry none. Callsigns are resolved against the game's own callsign table;
the preset, IFF and Link 16 panels are not rendered at all.

**No threat picture.** The mission file places every unit on the map but marks none
as a threat to your route.

**No estimated times.** IL-2 records no ETA at any waypoint, so the time column is
blank rather than derived from distance and speed.

**Scripted campaigns are partial.** DLC campaign missions are compiled into
`.cmpbin` inside `Campaigns.gtp` — a binary this cannot read — so there is no
route, weather or planned loadout for them. What *is* readable beside each mission
is its briefing text and its briefing map image, and the sortie log still gives the
as-flown loadout, so the board shows those and says what is missing. This path is
built from the archive's structure but has not been exercised against a real
campaign sortie.

**PWCG is out of scope.** Missions under `data\Missions\PWCG` are ignored so they
cannot be picked up as the current mission.

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
  state.py                source selection, payload assembly, caching, validation
  selfupdate.py           safe fast-forward self-update on startup
  data/f16_stores.json    curated F-16 store reference
  dcs/
    install.py            DCS discovery and mission listing
    luaparse.py           parser for the Lua table dialect .miz files use
    mission.py            .miz reader, unit conversion, route geometry
    maps.py               terrain projection, raster chart index, stitching, cache
    weapons.py            CLSID lookup against the curated library
    source.py             DCS payload assembly
    data/dcs_stores.json  curated F/A-18C, A-10C and AV-8B store reference
  il2/
    install.py            IL-2 discovery, mission and sortie-log listing
    gtp.py                reader for IL-2's packed .gtp archives
    missionfile.py        targeted scanner for the mission text format
    localization.py       UTF-16 text files and the briefing-to-prose converter
    logs.py               sortie log reader and mission correlation
    reference.py          callsign and country tables from data\GUI
    extract.py            weapon-name extraction from the game's archives, cached
    weapons.py            payload lookup with honest unknowns
    mission.py            mission reader, route geometry, unit conversion
    source.py             IL-2 payload assembly, career and campaign
templates/index.html      page shell
static/css, static/js     styling and renderer
```

Only dependency is Flask.

## Theatres other than Korea

The briefing, loadout, comms and weather pages are theatre-independent. The chart
and map pages read whatever is in your `Docs` tree, matching on ICAO and airfield
name, so a theatre that follows the same `Docs` layout works without changes.

## Support

If this saved you time, you can [buy me a coffee](https://ko-fi.com/cyberm00).
