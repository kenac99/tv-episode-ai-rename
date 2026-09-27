#!/usr/bin/env bash
# One-step installer for tv-episode-ai-rename (Linux and macOS).
# Run it from inside this folder:   ./install.sh
# It installs into THIS folder (a private Python environment in ./venv) and adds the command
# `tv-episode-ai-rename` to ~/.local/bin. Nothing system-wide except what you approve below.
set -e
cd "$(dirname "$0")"
HERE="$PWD"

say()  { printf '\n==> %s\n' "$*"; }
fail() { printf '\n!!  %s\n\n' "$*"; exit 1; }

say "Checking for ffmpeg and Python 3"
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
    if command -v apt-get >/dev/null; then
        echo "ffmpeg is missing. Installing it (you may be asked for your password)..."
        sudo apt-get update -q && sudo apt-get install -y ffmpeg
    elif command -v brew >/dev/null; then
        brew install ffmpeg
    else
        fail "ffmpeg is missing. Install it with your system's package manager, then run ./install.sh again."
    fi
fi
command -v python3 >/dev/null || fail "Python 3 is missing. Install python3, then run ./install.sh again."
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || fail "Python 3.9 or newer is needed (you have $(python3 -V))."
if ! python3 -m venv --help >/dev/null 2>&1; then
    if command -v apt-get >/dev/null; then
        echo "Python's venv module is missing. Installing it..."
        sudo apt-get install -y python3-venv
    else
        fail "Python's venv module is missing. Install it (often called python3-venv), then run ./install.sh again."
    fi
fi

say "Setting up a private Python environment in $HERE/venv (this downloads ~300 MB, takes a few minutes)"
[ -x venv/bin/python ] || python3 -m venv venv || { rm -rf venv; fail "Could not create venv. On Ubuntu/Debian: sudo apt install python3-venv"; }
venv/bin/python -m pip install -q --upgrade pip
venv/bin/python -m pip install -q -r requirements.txt
if command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1; then
    say "NVIDIA graphics card found: adding the CUDA libraries so Whisper can use it (~1 GB more)"
    venv/bin/python -m pip install -q nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*" \
        || echo "(Could not add them; the tool will just use the CPU instead. That's fine, only slower.)"
fi

say "Adding the command tv-episode-ai-rename"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/tv-episode-ai-rename" <<EOF
#!/usr/bin/env bash
exec "$HERE/venv/bin/python" "$HERE/tv_episode_ai_rename.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/tv-episode-ai-rename"

CFG="${XDG_CONFIG_HOME:-$HOME/.config}/tv-episode-ai-rename/config"
if [ ! -f "$CFG" ]; then
    say "Creating your key file: $CFG"
    mkdir -p "$(dirname "$CFG")"
    cat > "$CFG" <<'EOF'
# Paste your keys after the = signs. No spaces, no quotes needed. (README: "Get your keys")
TMDB_API_TOKEN=
OPENSUBTITLES_API_KEY=
OPENSUBTITLES_USERNAME=
OPENSUBTITLES_PASSWORD=
EOF
    chmod 600 "$CFG"
fi

say "Done."
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "One more thing: close this terminal and open a new one, so the new command is found."
       echo "(If it still says 'command not found', run:  echo 'export PATH=\$HOME/.local/bin:\$PATH' >> ~/.bashrc  and open a new terminal.)" ;;
esac
echo
echo "Next: put your keys in   $CFG"
echo "Then try:                 tv-episode-ai-rename --help"
