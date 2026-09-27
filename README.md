# tv-episode-ai-rename

**Rips come out named `Disc 1_t02.mkv`. This tool listens to each one and names it by the episode it really is.**

It uses AI speech recognition (Whisper) to hear what's said in each file, downloads the real
subtitles for every episode of the season, and matches them up. It doesn't guess from runtimes or
disc order, which is why it gets them right.

```
  listening to disc1_t03.mkv   -> E01  79%
  listening to disc2_t00.mkv   -> E05  47%
  listening to disc3_t01.mkv   -> E12  75%

  disc1_t03.mkv  -> Star Trek - Enterprise - S03E01 - The Xindi.mkv
  disc2_t00.mkv  -> Star Trek - Enterprise - S03E05 - Impulse.mkv
  disc3_t01.mkv  -> Star Trek - Enterprise - S03E12 - Chosen Realm.mkv
```

Runs on Linux (tested on RHEL/Rocky; the installer handles Debian/Ubuntu too) and macOS. No Windows version.

---

## Install (about 10 minutes, once)

**1. Put this folder in your home folder.** Unzip it or copy it so you have `~/tv-episode-ai-rename`.

**2. Open a terminal and type these two lines:**

```
cd ~/tv-episode-ai-rename
./install.sh
```

It checks for everything it needs and installs what's missing. It may ask for your password; that's
normal. It downloads a few hundred MB, more if you have an NVIDIA graphics card. When it says
**Done.**, close the terminal and open a new one.

**3. Check it worked.** In the new terminal type:

```
tv-episode-ai-rename --help
```

If you see a list of options, it's installed. If it says `command not found`, see
[Troubleshooting](#troubleshooting).

---

## Get your keys (free, once)

The tool needs two free accounts: one to look up episode names and one to download subtitles. The
installer made a file for them at `~/.config/tv-episode-ai-rename/config`. Open it with any text
editor, for example:

```
nano ~/.config/tv-episode-ai-rename/config
```

It looks like this. You'll paste things after the `=` signs:

```
TMDB_API_TOKEN=
OPENSUBTITLES_API_KEY=
OPENSUBTITLES_USERNAME=
OPENSUBTITLES_PASSWORD=
```

**TMDB (episode names):**
1. Make a free account at **themoviedb.org** and log in.
2. Click your profile picture → **Settings** → **API** (left side).
3. Request an API key (choose "Personal"; any short description is fine).
4. On the same page, copy the long **API Read Access Token** (it starts with `eyJ`).
5. Paste it after `TMDB_API_TOKEN=`.

**OpenSubtitles (subtitles):**
1. Make a free account at **opensubtitles.com** and log in.
2. Go to **opensubtitles.com/consumers** and click **New consumer**. Any name is fine.
3. Copy the **API key** it shows you and paste it after `OPENSUBTITLES_API_KEY=`.
4. Put your OpenSubtitles username and password after the last two `=` signs. (Without them you
   only get a handful of downloads a day, and a season needs one per episode.)

Save the file (in nano: **Ctrl+O**, **Enter**, then **Ctrl+X** to exit).

---

## Rename a season

**1. Put one season's rips in one folder,** for example `~/rips/enterprise-s1/`.

**2. Find the show's number on TMDB.** Search for the show on themoviedb.org and open it. The number
is in the web address: `themoviedb.org/tv/`**`314`**`-star-trek-enterprise` → `314`.

**3. Do a practice run.** This changes nothing; it only shows you what it *would* do:

```
tv-episode-ai-rename ~/rips/enterprise-s1 --tmdb-id 314 --season 1 --show "Star Trek - Enterprise"
```

The first time, it downloads the speech model (about 150 MB). Then it listens to each file, which
takes a little while per episode.

**4. Read the list.** Each line shows the episode it picked, how sure it is, and the new name.
Anything it wasn't sure about is listed at the bottom under **NEEDS A HUMAN**.

**5. Do it for real.** Run the same line again with `--apply` added at the end:

```
tv-episode-ai-rename ~/rips/enterprise-s1 --tmdb-id 314 --season 1 --show "Star Trek - Enterprise" --apply
```

The files are renamed where they are. To move them into your library at the same time, add
`--dest` and the folder:

```
... --apply --dest ~/tv/"Star Trek - Enterprise"/"Season 1"
```

**Made a mistake?** This puts everything from the last run back exactly how it was:

```
tv-episode-ai-rename --undo last
```

The new names look like `Star Trek - Enterprise - S01E01 - Broken Bow.mkv`, which Plex, Emby and
Jellyfin all recognise.

---

## Troubleshooting

| you see | do this |
|---|---|
| `command not found` after installing | Open a new terminal. Still no? Run `echo 'export PATH=$HOME/.local/bin:$PATH' >> ~/.bashrc`, then open a new terminal. |
| `missing TMDB_API_TOKEN` or `missing OPENSUBTITLES_API_KEY` | A key is missing from the config file (see *Get your keys*). |
| `HTTP Error 401` | A key or your OpenSubtitles password was pasted wrong. Check for extra spaces. |
| `HTTP Error 406` or `429` | OpenSubtitles' daily download limit. Add your username and password, or wait a day. Downloaded subtitles are kept, so it continues where it stopped. |
| `GPU not usable … using the CPU instead` | Nothing's wrong; it's just slower. |
| `WARNING no subtitles for episodes [...]` | Nobody has uploaded subtitles for those episodes, so they can't be matched. |
| a file shows up under `UNMATCHED` | It couldn't match that file confidently, so it left it alone on purpose. It may be an extra, a trailer, or from a different season. |

---

## How it works

1. **Listen.** It cuts three 90-second clips from each file (at 20%, 50% and 75% of the way through),
   so a quiet stretch or a long fight scene can't throw it off. Whisper turns the speech into text.
2. **Read.** It gets the season's episode list from TMDB and the most-downloaded subtitle file for
   each episode from OpenSubtitles.
3. **Compare.** The score is how many of the file's 4-word phrases appear in an episode's subtitles.
   Lines from one episode almost never appear in another, so the right answer usually wins by
   45–80 points.
4. **Assign.** Each episode can go to only one file, strongest matches first, so two files never
   get the same name.

## Safety

- **Practice run by default.** Nothing changes without `--apply` or `--auto`.
- **It won't guess.** Below 25%, a file is listed as `UNMATCHED` and left alone. A winner that barely
  beats the runner-up is flagged `THIN MARGIN, CHECK`.
- **`--auto` is for unattended use.** It renames only matches of at least 35% that beat the runner-up
  by at least 30 points, and holds everything else. Those limits come from testing: on 24
  known-correct episodes, scores ran 51–85% and the winning margins were +51 to +83 points.
- **Everything can be undone.** Every run is logged, and `--undo last` reverses it.

## All options

```
tv-episode-ai-rename FOLDER-OR-FILES --tmdb-id ID --season N --show "Show Name" [options]

  --apply            rename every confident-enough match (after you've read the practice run)
  --auto             rename only very confident matches; hold the rest (for scripts)
  --dest DIR         move the renamed files into this folder
  --copy             copy instead of move (with --dest)
  --language en      language of the dialogue and subtitles
  --model base       Whisper size: tiny / base / small / medium / large-v3 (bigger = slower, more accurate)
  --device auto      auto / cuda / cpu
  --undo last        reverse the last run (or give a run name from the list it prints)
  -n                 practice run even if --apply or --auto is given
```

Settings can also come from environment variables with the same names as the config file.
Downloads and transcripts are cached in `~/.cache/tv-episode-ai-rename`, so running a season again
is quick.

## Limits

- It can only match episodes that have English subtitles on OpenSubtitles (or your `--language`).
- It matches within the one season you name. It won't notice a file from a different season; it
  will just list it as `UNMATCHED`.
- A two-part episode ripped as one file will match one of its two parts.

## License

MIT — see [LICENSE](LICENSE).
