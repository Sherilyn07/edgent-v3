# EdgentRAG services — on Colab

Three model services, one GPU, one command.

    embedding   :8001   text -> vectors, and searching them
    stt         :8002   video -> transcript -> chunks
    llm         :8003   prompt -> answer

They are three separate FastAPI applications sharing one card. None of them
talks to a database or keeps any state; everything they need arrives in the
request. That is what makes them movable.

One nuance worth knowing: the speech-to-text service does read from storage,
but only from a signed link the monolith hands it, and only when files are in
S3. It never needs to know where the bucket is or hold a credential — the
permission is baked into the link and expires.

---

## Setup, once

**1. Put this folder on Drive.** Upload the whole `services` folder to
`MyDrive`. Rename it to something you will recognise:

    MyDrive/edgentrag_services/

**2. New Colab notebook, GPU runtime.** Runtime → Change runtime type → A100
(or any GPU). Then mount Drive, in a cell:

```python
from google.colab import drive
drive.mount('/content/drive')
```

**3. Open the terminal.** The `>_` button, bottom left of the Colab sidebar.
It needs Colab Pro.

---

## The one command

```bash
bash /content/drive/MyDrive/edgentrag_services/run_colab.sh
```

Installs what is missing, starts all three services, opens an HTTPS tunnel in
front of each, and finishes by printing:

```
Paste these three into the app's start screen

  EMBEDDING   https://polite-otter-bright.trycloudflare.com
        STT   https://quiet-hill-monday.trycloudflare.com
        LLM   https://brave-cloud-summer.trycloudflare.com
```

Copy all three. In the app on your laptop, paste them into the start screen —
paste all three lines into any one box and they will sort themselves into the
right fields. Press Connect. Now you can upload.

Leave the terminal open. Ctrl-C stops everything it started — the three
services plus a tunnel for each.

---

## What to expect

The first run downloads model weights: about 90 MB for the embedder, 75 MB for
Whisper `tiny.en`, 2.2 GB for TinyLlama. They land on the Colab disk (not Drive
— Drive is too slow to read a checkpoint through) so a runtime restart
downloads them again.

`launch.py` waits for each service to answer `/health` before it reports it as
up, so if a service says "did not come up", the reason is in its log. On Colab
those go to `/content/edgentrag_work/logs/`, not next to the code — the
launcher prints the location when it starts.

**Nothing writable goes on Drive, deliberately.** Drive is a FUSE filesystem
and it cannot do two things this depends on: the byte-range locking SQLite
needs (Chroma is SQLite underneath, so an index on Drive fails with "database
is locked" on your first document), and `chmod` (a downloaded binary stays
non-executable). The code is read from Drive; everything else lives on the
Colab disk and disappears with the runtime, which is fine — the index is
rebuilt by re-uploading.

---

## The addresses change

A tunnel address lives as long as the command that created it. Restart the
Colab runtime, or lose the connection, and all three are gone — the app's
health strip goes red and you press **reconnect**, run the command again, and
paste three new addresses.

This is annoying and it is honest: it is the same problem as any service whose
address is not stable, and it is why the app asks for the addresses at runtime
instead of reading them from a config file.

---

## Using the card you are paying for

TinyLlama on an A100 80GB is about 3% of the card. Everything below is one line
in `.env` (copy `.env.example` first):

```bash
LLM_MODEL=Qwen/Qwen2.5-7B-Instruct     # no licence gate
MAX_INPUT_TOKENS=8000                  # its context is far bigger than TinyLlama's
WHISPER_MODEL=large-v3                 # still faster than real time on a GPU
BATCH_SIZE=256                         # the embedder likes a big batch on a GPU
```

`DEVICE`, `DTYPE` and `COMPUTE_TYPE` all default to `auto`: CUDA and bfloat16 on
a GPU, CPU and float32 on a laptop. You do not have to set them, and the same
code runs in both places.

Changing `EMBED_MODEL` means **re-indexing everything**. Vectors from two
different models are not comparable, and old ones do not get thrown away — you
will get retrieval that is quietly, confusingly wrong rather than an error.

---

## Options

| variable | default | what it does |
|---|---|---|
| `TUNNEL` | `cloudflared` | `ngrok` (set `NGROK_AUTHTOKEN`) or `none` for local only |
| `CHROMA_DIR` | `/content/_chroma` | where the vectors go. **Never put this on Drive** — see below |
| `HF_HOME` | `/content/hf_cache` | where weights are cached |

---

## Running these locally instead

Nothing here is Colab-specific. From this folder:

    pip install -r requirements.txt
    TUNNEL=none python launch.py

Three services on 8001/8002/8003, no tunnels, everything on CPU. Point the app
at `http://localhost:8001` and friends.

---

## When it does not work

**`did not come up`** — read `logs/<name>.log`. Nearly always a missing package
or, for the LLM, running out of memory while loading.

**No tunnel URL** — cloudflared could not reach Cloudflare. Re-run; quick
tunnels are occasionally rate-limited. Or use `TUNNEL=ngrok` with a token.

**The app says it cannot reach a service** — the tunnel died with the runtime.
Re-run the command and paste the new addresses.

**Video fails but documents work** — `ffmpeg` is missing. `run_colab.sh`
installs it; if that step was skipped, `apt-get install -y ffmpeg`.

**Everything is slow and the GPU looks idle** — check the top of the launcher's
output. If it says "no GPU found", the notebook is on a CPU runtime.
