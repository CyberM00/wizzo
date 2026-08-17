# Contributing

Bug reports are more useful than pull requests here, because most of what
breaks Wizzo is a game patch changing a file it reads. If DCS moves a Lua
table or IL-2 renames a weapon, no amount of careful code survives it, and I
cannot see your install from here.

## Reporting something broken

Open an issue and say which sim, which version, and what the board showed
instead of what you expected. The log at `%LOCALAPPDATA%\Wizzo\wizzo.log`
usually names the file it choked on.

If it is a loadout or chart problem, the mission file matters. `.miz` files are
zips and often small enough to attach. Do check what is in yours first if it
came from somewhere you would rather not share.

## Running from source

```bash
pip install -r requirements.txt
python wizzo.py
```

Add `-r requirements-build.txt` if you want to build the packaged app or run
the tests.

```bash
python -m pytest
```

Tests that need a game installed skip cleanly when it is absent, so a bare
checkout still runs a useful subset. The suite deliberately covers the parts a
game patch could break quietly: the terrain projection solver, the `.sup5`
tile-index record layout, the install-folder verifier, version comparison.

## House style

**No em-dashes.** Use `--` instead. A test fails the build if one appears, and
it will name the file and line. This is not an aesthetic preference; they cause
real problems in the places this text ends up.

Comments explain *why*, not *what*. The code says what it does.

**Say what is actually known.** This matters more than it sounds. The board
distinguishes a name read from the game's own files from one in the curated
library, and tags the former `NAME ONLY`. When chart placement cannot be
trusted it draws a graticule and says so rather than drawing a route on a
guess. If you add something that infers, make it admit what it inferred. A
kneeboard that confidently shows the wrong thing is worse than one that shows
nothing.

## Pull requests

Keep them small enough to read in one sitting. Run the tests. If you are
changing something a sim writes, say which version of that sim you checked
against, because that is the fact I cannot verify myself.
