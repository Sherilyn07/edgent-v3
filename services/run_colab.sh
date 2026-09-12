#!/usr/bin/env bash
# The one command. From a Colab terminal:
#
#     bash /content/drive/MyDrive/edgentrag_services/run_colab.sh
#
# Installs what is missing, then starts all three services with an HTTPS
# address in front of each. Ctrl-C stops everything.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

echo
echo "EdgentRAG services — setup"
echo

# Colab already has torch, and it is matched to the runtime's CUDA driver.
# Reinstalling it is the most common way to break a Colab GPU, so we leave it
# alone and install only what is missing around it.
if python -c "import torch" 2>/dev/null; then
  echo "  torch          already present (leaving it alone)"
else
  echo "  torch          installing"
  pip install -q torch
fi

echo "  dependencies   installing"
pip install -q -r requirements.txt

# ffmpeg is what faster-whisper uses to pull audio out of an mp4.
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "  ffmpeg         installing"
  apt-get -qq update && apt-get -qq install -y ffmpeg >/dev/null 2>&1 || \
    echo "  ffmpeg         could not install (video may still work)"
else
  echo "  ffmpeg         already present"
fi

# Everything that gets written goes on the Colab disk, never on Drive.
#
# Weights, because Drive is slow and reading a 2 GB checkpoint through it turns
# a 30-second load into minutes.
#
# The vector index, because Chroma is SQLite and Drive's FUSE layer cannot do
# the file locking SQLite needs -- it fails with "database is locked" on the
# first document you index.
#
# Only the code lives on Drive, and the code is only ever read.
export HF_HOME="${HF_HOME:-/content/hf_cache}"
export CHROMA_DIR="${CHROMA_DIR:-/content/_chroma}"
mkdir -p "$HF_HOME" "$CHROMA_DIR"

echo
exec python launch.py
