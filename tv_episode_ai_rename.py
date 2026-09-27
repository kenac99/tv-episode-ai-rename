#!/usr/bin/env python3
"""tv-episode-ai-rename — name TV episode files by their actual DIALOGUE, not by their filename.

Whisper listens to three 90-second windows of each video, the real subtitles for every episode of
the season are pulled from OpenSubtitles, and each file is matched to the episode whose subtitles
contain the most of what was actually said. Works on raw disc rips with no episode number in the
name at all (`Disc 1_t02.mkv`) — there is nothing to trust but the audio.

Assignment is global and one-to-one: two files can never claim the same episode. Anything that
doesn't clear a confidence floor is reported as UNMATCHED instead of guessed.

    DRY RUN by default.   --apply  renames everything above the floor (you reviewed the plan)
                          --auto   renames only high-confidence matches, holds the rest
                          --undo last   puts the last run back

    tv-episode-ai-rename ~/rips/enterprise_s4_d*/ --tmdb-id 314 --season 4 \\
        --show "Star Trek - Enterprise" --dest ~/tv/"Star Trek - Enterprise"/"Season 4"
"""
import argparse, glob, hashlib, importlib.util, json, os, re, shutil, subprocess, sys, tempfile, time
import urllib.parse, urllib.request

__version__ = "1.0.0"

# ctranslate2 (the engine under faster-whisper) installs into site-packages/lib64 while its CUDA
# libraries (pip's nvidia-* wheels) land under site-packages/nvidia/*/lib, so the loader can't find
# libcublas and whisper dies at the FIRST transcribe — after it has already reported "on GPU".
# LD_LIBRARY_PATH only takes effect at process start, so re-exec once with it set.
if not os.environ.get("_TV_EP_AI_CUDA_PATH"):
    import site
    _roots = list(getattr(site, "getsitepackages", lambda: [])()) + [site.getusersitepackages()]
    _spec_nv = importlib.util.find_spec("nvidia")
    if _spec_nv and _spec_nv.submodule_search_locations:
        _roots = list(_spec_nv.submodule_search_locations) + _roots
    _nv = []
    for _r in _roots:
        _base = _r if os.path.basename(_r) == "nvidia" else os.path.join(_r, "nvidia")
        _nv += [p for p in glob.glob(os.path.join(_base, "*", "lib")) if os.path.isdir(p)]
    os.environ["_TV_EP_AI_CUDA_PATH"] = "1"
    if _nv:
        os.environ["LD_LIBRARY_PATH"] = ":".join(_nv + [os.environ.get("LD_LIBRARY_PATH", "")])
        os.execv(sys.executable, [sys.executable] + sys.argv)

# ---------------------------------------------------------------- thresholds (set from evidence)
FLOOR = 0.25      # best match below this = refuse to name the file at all
MARGIN = 0.05     # winner must beat the runner-up by this much, else flag it for a human
# --auto renames ONLY files clearing both bars. Measured on 24 known-correct episodes: scores ran
# 51-85% but margins ran +51 to +83pp — dialogue from one episode barely appears in another's
# subtitles, so a right answer buries the runner-up. Margin is the real discriminator and gets the
# high bar; score is a sanity floor, kept low so a quiet, sparse-dialogue episode with a decisive
# margin still names itself. Anything genuinely ambiguous sits near +0pp and is held.
AUTO_SCORE = 0.35
AUTO_MARGIN = 0.30

WINDOWS = (0.20, 0.50, 0.75)   # sample points (fraction of runtime): robust to quiet stretches
WINDOW_SEC = 90

# ---------------------------------------------------------------- paths + config
_xdg = lambda var, default: os.path.join(os.environ.get(var) or os.path.expanduser(default), "tv-episode-ai-rename")
CONFIG_FILE = os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "config")
CACHE_DIR = _xdg("XDG_CACHE_HOME", "~/.cache")
JOURNAL = os.path.join(_xdg("XDG_STATE_HOME", "~/.local/state"), "journal.jsonl")


def load_config():
    """Environment variables win; otherwise KEY=VALUE lines from the config file."""
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        for line in open(CONFIG_FILE):
            m = re.match(r'\s*([A-Z_]+)\s*=\s*"?([^"\n]*)"?\s*$', line)
            if m:
                cfg[m.group(1)] = m.group(2)
    for k in ("OPENSUBTITLES_API_KEY", "OPENSUBTITLES_USER_AGENT", "OPENSUBTITLES_USERNAME",
              "OPENSUBTITLES_PASSWORD", "TMDB_API_TOKEN"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    missing = [k for k in ("OPENSUBTITLES_API_KEY", "TMDB_API_TOKEN") if not cfg.get(k)]
    if missing:
        sys.exit(f"missing {', '.join(missing)} — set them in the environment or in {CONFIG_FILE} (see README)")
    cfg.setdefault("OPENSUBTITLES_USER_AGENT", f"tv-episode-ai-rename v{__version__}")
    return cfg


def norm(s):
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return " ".join(s.split())


# ---------------------------------------------------------------- TMDB + OpenSubtitles
class Sources:
    OS = "https://api.opensubtitles.com/api/v1/"

    def __init__(self, cfg, language):
        self.cfg, self.lang, self._token = cfg, language, None

    def tmdb(self, path):
        req = urllib.request.Request("https://api.themoviedb.org/3/" + path,
                                     headers={"Authorization": f"Bearer {self.cfg['TMDB_API_TOKEN']}"})
        return json.load(urllib.request.urlopen(req, timeout=30))

    def season_episodes(self, tmdb_id, season):
        """[(number, name), ...] for the season."""
        return [(e["episode_number"], e["name"]) for e in self.tmdb(f"tv/{tmdb_id}/season/{season}")["episodes"]]

    def _os(self, path, params=None, post=None):
        url = self.OS + path + ("?" + urllib.parse.urlencode(sorted(params.items())) if params else "")
        headers = {"Api-Key": self.cfg["OPENSUBTITLES_API_KEY"], "User-Agent": self.cfg["OPENSUBTITLES_USER_AGENT"],
                   "Accept": "application/json", "Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        req = urllib.request.Request(url, data=json.dumps(post).encode() if post else None,
                                     method="POST" if post else "GET", headers=headers)
        return json.load(urllib.request.urlopen(req, timeout=30))

    def _login(self):
        """Anonymous API keys get very few downloads a day; logging in raises that to the account's quota."""
        if self._token is None and self.cfg.get("OPENSUBTITLES_USERNAME"):
            r = self._os("login", post={"username": self.cfg["OPENSUBTITLES_USERNAME"],
                                        "password": self.cfg.get("OPENSUBTITLES_PASSWORD", "")})
            self._token = r.get("token") or ""
        return self._token

    def episode_subtitles(self, tmdb_id, season, ep):
        """Normalised subtitle text for one episode ("" if none exist). Cached forever."""
        os.makedirs(os.path.join(CACHE_DIR, "subtitles"), exist_ok=True)
        cf = os.path.join(CACHE_DIR, "subtitles", f"{tmdb_id}_s{season}e{ep}_{self.lang}.txt")
        if os.path.exists(cf):
            return open(cf).read()
        r = self._os("subtitles", {"parent_tmdb_id": tmdb_id, "season_number": season,
                                   "episode_number": ep, "languages": self.lang})
        if not r["data"]:
            open(cf, "w").write("")
            return ""
        best = max(r["data"], key=lambda s: s["attributes"].get("download_count", 0))
        self._login()
        dl = self._os("download", post={"file_id": best["attributes"]["files"][0]["file_id"]})
        raw = urllib.request.urlopen(urllib.request.Request(
            dl["link"], headers={"User-Agent": self.cfg["OPENSUBTITLES_USER_AGENT"]}), timeout=60).read()
        lines = [l for l in raw.decode("utf-8", "ignore").replace("\r", "").split("\n")
                 if l.strip() and "-->" not in l and not l.strip().isdigit()]
        text = norm(" ".join(lines))
        open(cf, "w").write(text)
        return text


# ---------------------------------------------------------------- listening
def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=noprint_wrappers=1:nokey=1", path], capture_output=True, text=True).stdout
    try:
        return float(out.strip())
    except ValueError:
        return 2700.0        # unknown: assume a 45-minute episode


def transcript(model, path, language):
    """Whisper text of three windows of the file. Cached by size+mtime, so re-runs are free."""
    tc = os.path.join(CACHE_DIR, "transcripts")
    os.makedirs(tc, exist_ok=True)
    st = os.stat(path)
    cf = os.path.join(tc, hashlib.md5(f"{st.st_size}_{int(st.st_mtime)}_{language}".encode()).hexdigest() + ".txt")
    if os.path.exists(cf):
        return open(cf).read()
    d, parts = duration(path), []
    for frac in WINDOWS:
        fd, wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            subprocess.run(["ffmpeg", "-y", "-ss", str(int(d * frac)), "-i", path, "-t", str(WINDOW_SEC),
                            "-vn", "-ar", "16000", "-ac", "1", wav], capture_output=True)
            segs, _ = model.transcribe(wav, language=language)
            parts.append(" ".join(s.text for s in segs))
        finally:
            if os.path.exists(wav):
                os.remove(wav)
    text = norm(" ".join(parts))
    open(cf, "w").write(text)
    return text


def overlap(text, subs):
    """Share of the file's spoken 4-word phrases that appear in the episode's subtitles."""
    w = text.split()
    grams = set(" ".join(w[i:i + 4]) for i in range(len(w) - 3))
    return sum(1 for g in grams if g in subs) / max(len(grams), 1)


BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
def safe(t):
    return BAD.sub("", t.replace(":", " -").replace("/", "-")).strip().rstrip(".")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog="tv-episode-ai-rename",
                                 description="Name TV episode files by their dialogue (Whisper + OpenSubtitles).")
    ap.add_argument("sources", nargs="*", help="video files and/or directories of them")
    ap.add_argument("--tmdb-id", type=int, help="the show's TMDB id (themoviedb.org/tv/<id>)")
    ap.add_argument("--season", type=int)
    ap.add_argument("--show", help='show name used in the new filenames, e.g. "Star Trek - Enterprise"')
    ap.add_argument("--dest", help="move the files into this directory (default: rename in place)")
    ap.add_argument("--copy", action="store_true", help="copy instead of move when --dest is used")
    ap.add_argument("--language", default="en", help="dialogue + subtitle language (default: en)")
    ap.add_argument("--model", default="base", help="Whisper model: tiny/base/small/medium/large-v3 (default: base)")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--ext", default=".mkv,.mp4,.m4v,.avi")
    ap.add_argument("--apply", action="store_true", help="rename every match above the floor (you reviewed the plan)")
    ap.add_argument("--auto", action="store_true",
                    help=(f"unattended: rename ONLY matches >={AUTO_SCORE:.0%} with a >=+{AUTO_MARGIN:.0%} margin; "
                          "leave the rest alone and list them").replace("%", "%%"))
    ap.add_argument("--min-score", type=float, default=AUTO_SCORE, help="--auto score bar")
    ap.add_argument("--min-margin", type=float, default=AUTO_MARGIN, help="--auto margin bar")
    ap.add_argument("-n", "--dry-run", action="store_true", help="preview even with --apply/--auto")
    ap.add_argument("--undo", metavar="RUN_ID", help="reverse a previous run ('last' or an id from the journal)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = ap.parse_args()

    if a.undo:
        return undo(a.undo)
    missing = [n for n, v in (("sources", a.sources), ("--tmdb-id", a.tmdb_id),
                              ("--season", a.season), ("--show", a.show)) if not v]
    if missing:
        ap.error("required unless --undo: " + ", ".join(missing))

    exts = tuple(e.strip().lower() for e in a.ext.split(","))
    files = []
    for s in a.sources:
        if os.path.isdir(s):
            files += [f for f in sorted(glob.glob(os.path.join(s, "*"))) if f.lower().endswith(exts)]
        elif s.lower().endswith(exts):
            files.append(s)
    files = sorted(set(files))
    if not files:
        sys.exit("no video files found")

    src = Sources(load_config(), a.language)
    eps = src.season_episodes(a.tmdb_id, a.season)
    print(f"{len(files)} files  vs  S{a.season:02d}: {len(eps)} episodes  (TMDB {a.tmdb_id})")
    print("pulling subtitles...")
    subs = {n: src.episode_subtitles(a.tmdb_id, a.season, n) for n, _ in eps}
    nosubs = [n for n in subs if not subs[n]]
    if nosubs:
        print(f"  WARNING no subtitles for episodes {nosubs} — they can never be matched")

    from faster_whisper import WhisperModel
    model = None
    if a.device in ("auto", "cuda"):
        try:
            model, dev = WhisperModel(a.model, device="cuda", compute_type="float16"), "GPU"
            # Loading can succeed on a machine whose CUDA libraries are missing; the crash only comes at the
            # first transcribe. Prove the GPU works on one second of silence before trusting it.
            import numpy as np
            list(model.transcribe(np.zeros(16000, dtype=np.float32))[0])
        except Exception as e:
            model = None
            if a.device == "cuda":
                sys.exit(f"CUDA requested but it doesn't work here: {e}")
            print(f"(GPU not usable — {str(e)[:90]} — using the CPU instead; slower but fine)")
    if model is None:
        model, dev = WhisperModel(a.model, device="cpu", compute_type="int8"), "CPU"
    print(f"whisper '{a.model}' on {dev}\n")

    # score matrix: how much of each file's dialogue appears in each episode's subtitles
    score = {}
    for f in files:
        print(f"  listening to {os.path.basename(f)[:58]:58s}", end="", flush=True)
        text = transcript(model, f, a.language)
        score[f] = {n: overlap(text, subs[n]) for n, _ in eps if subs[n]}
        b = max(score[f], key=score[f].get) if score[f] else 0
        print(f"  -> E{b:02d} {score[f].get(b, 0) * 100:3.0f}%")

    # greedy one-to-one assignment, strongest match first
    title = dict(eps)
    pairs = sorted(((sc, f, n) for f, d in score.items() for n, sc in d.items()), reverse=True)
    taken_f, taken_e, assign = set(), set(), {}
    for sc, f, n in pairs:
        if f in taken_f or n in taken_e or sc < FLOOR:
            continue
        runner = max((v for k, v in score[f].items() if k != n and k not in taken_e), default=0.0)
        assign[f] = (n, sc, sc - runner)
        taken_f.add(f); taken_e.add(n)

    mode = "AUTO" if a.auto else ("APPLYING" if a.apply else "DRY RUN — nothing changed")
    if a.auto:
        mode += f" (bar: >={a.min_score:.0%} score, >=+{a.min_margin * 100:.0f}pp margin)"
    print(f"\n{mode}")
    plan, held, problems = [], [], []
    for f in files:
        base = os.path.basename(f)
        if f not in assign:
            problems.append(f"  UNMATCHED   {base}   (nothing above {FLOOR:.0%})")
            continue
        n, sc, marg = assign[f]
        new = f"{a.show} - S{a.season:02d}E{n:02d} - {safe(title[n])}{os.path.splitext(f)[1]}"
        dst = os.path.join(a.dest or os.path.dirname(f), new)
        confident = sc >= a.min_score and marg >= a.min_margin
        if a.auto and not confident:
            note = "  HELD (below auto bar)"
            held.append(f"  HELD        {base} -> E{n:02d} ({sc * 100:.0f}%, +{marg * 100:.0f}pp)")
        elif marg < MARGIN:
            note = "  <-- THIN MARGIN, CHECK"
            problems.append(f"  THIN        {base} -> E{n:02d} (+{marg * 100:.0f}pp)")
            plan.append((f, dst))
        else:
            note = ""
            plan.append((f, dst))
        print(f"  E{n:02d} {sc * 100:3.0f}% +{marg * 100:2.0f}pp  {base[:40]:40s} -> {new}{note}")
    unused = [n for n, _ in eps if n not in taken_e]
    if unused:
        problems.append(f"  NO FILE for episodes {unused}")

    if held:
        print(f"\nHELD BACK — {len(held)} file(s) left untouched for you to decide:")
        print("\n".join(held))
    if problems:
        print("\nNEEDS A HUMAN:")
        print("\n".join(problems))
    if a.dry_run or not (a.apply or a.auto):
        print("\nnothing changed — rerun with --apply (reviewed) or --auto (confident ones only)")
        return
    if a.dest:
        os.makedirs(a.dest, exist_ok=True)

    run_id = f"{a.show} S{a.season:02d} {time.strftime('%Y%m%d-%H%M%S')}"
    done = []
    for s, dst in plan:
        if os.path.exists(dst):
            print(f"  SKIP exists: {dst}")
            continue
        copying = bool(a.dest and a.copy)
        (shutil.copy2 if copying else shutil.move)(s, dst)
        if not copying:
            done.append({"from": os.path.abspath(s), "to": os.path.abspath(dst)})
        print(f"  ok {os.path.basename(dst)}")
    if done:
        os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
        with open(JOURNAL, "a") as j:
            j.write(json.dumps({"run": run_id, "moves": done}) + "\n")
        print(f"\njournalled {len(done)} move(s) as: {run_id}")
        print(f'undo with:  tv-episode-ai-rename --undo last   (or --undo "{run_id}")')


def undo(run_id):
    """Put a previous run's files back. The safety net that makes --auto reasonable."""
    if not os.path.exists(JOURNAL):
        sys.exit(f"no journal at {JOURNAL}")
    runs = [json.loads(l) for l in open(JOURNAL) if l.strip()]
    if not runs:
        sys.exit("journal is empty")
    r = runs[-1] if run_id == "last" else next((x for x in runs if x["run"] == run_id), None)
    if not r:
        sys.exit("no such run. known runs:\n  " + "\n  ".join(x["run"] for x in runs[-10:]))
    print(f"undoing: {r['run']}  ({len(r['moves'])} files)")
    back = 0
    for m in reversed(r["moves"]):
        if not os.path.exists(m["to"]):
            print(f"  gone, skipping: {m['to']}")
            continue
        if os.path.exists(m["from"]):
            print(f"  original path occupied, skipping: {m['from']}")
            continue
        os.makedirs(os.path.dirname(m["from"]), exist_ok=True)
        shutil.move(m["to"], m["from"])
        back += 1
    print(f"restored {back} file(s)")
    with open(JOURNAL, "w") as j:
        for x in runs:
            if x is not r:
                j.write(json.dumps(x) + "\n")


if __name__ == "__main__":
    main()
