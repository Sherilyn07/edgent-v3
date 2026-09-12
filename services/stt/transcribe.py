"""Turning speech into text, and text into chunks.

STUDENT WORK — three functions.

faster-whisper is a rebuilt version of OpenAI's Whisper that runs several times
quicker and, usefully for us, runs perfectly well on a CPU.
"""
import logging

from .config import get_settings, resolve_compute_type, resolve_device

log = logging.getLogger(__name__)
settings = get_settings()

_model = None


def load_model():
    """Load Whisper once.

    What to write:

        from faster_whisper import WhisperModel

        _model = WhisperModel(settings.whisper_model,
                              device=settings.device,
                              compute_type=settings.compute_type)

    (with `global _model` at the top, and `return _model`).

    First run downloads about 75 MB for tiny.en. int8 on CPU means the weights
    are stored as 8-bit integers instead of 32-bit floats: a quarter of the
    memory, several times faster, and slightly less accurate. That trade is
    called quantization, and we come back to it properly later in the course.
    """
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        device = resolve_device(settings.device)
        compute_type = resolve_compute_type(settings.compute_type, device)
        log.info("loading whisper %s on %s (%s)",
                 settings.whisper_model, device, compute_type)
        _model = WhisperModel(
            settings.whisper_model,
            device=device,
            compute_type=compute_type,
        )
    return _model


def transcribe_file(local_path: str) -> tuple[list[dict], float]:
    """Transcribe an audio or video file.

    Returns (segments, duration_seconds).

    What to write:

      1. model = load_model()
      2. segments, info = model.transcribe(local_path, beam_size=1,
                                           vad_filter=True)

         `segments` is a *generator* -- nothing has actually run yet. The work
         happens as you loop over it, which is why the next line can take
         minutes and look like it has hung.

         `vad_filter=True` switches on voice activity detection: it skips
         silence instead of transcribing it, which on a lecture recording with
         long pauses is a large saving.

      3. Build a plain list of dictionaries:

             {"start": round(s.start, 2), "end": round(s.end, 2),
              "text": s.text.strip()}

         Drop any segment whose text is empty.

      4. Return that list and info.duration.

    You do not need ffmpeg installed separately -- faster-whisper reads the
    audio out of an mp4 by itself.

    We ignore settings.max_seconds here. Adding the cut is a good small
    exercise once the rest works: transcribe only what falls before it, and
    think about whether the user should be told.
    """
    model = load_model()
    segments, info = model.transcribe(local_path, beam_size=1, vad_filter=True)

    results = []
    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        results.append(
            {"start": round(segment.start, 2), "end": round(segment.end, 2), "text": text}
        )

    return results, info.duration


def segments_to_chunks(
    segments: list[dict], *, session_id: str, file_id: str, source: str
) -> list[dict]:
    """Pack transcript segments into chunks.

    Whisper gives you a sentence or two at a time -- far too small to embed on
    its own. Group them until you have roughly settings.chunk_words words, then
    start a new chunk.

    What to write:
      1. Walk the segments, keeping a running list and a running word count.
      2. When the count passes settings.chunk_words, emit a chunk and start
         again. Carry the last segment or two into the new chunk as overlap,
         the same idea as the text chunker uses.
      3. Each chunk is a dictionary, and it must have the same field names the
         text chunker produces -- once it is text, nothing downstream should be
         able to tell where it came from:

             {
               "chunk_id":   f"{file_id}:{index:04d}",
               "file_id":    file_id,
               "session_id": session_id,
               "source":     source,
               "kind":       "video",
               "section":    "12:30-15:05",   # from the timestamps, see below
               "text":       " ".join(segment texts),
               "order":      index,
               "start":      first segment's start,
               "end":        last segment's end,
             }

    Put the timestamp range in `section`. It shows up on screen under the
    answer, so a chunk drawn from a lecture points at the minute it came from.
    Formatting seconds as mm:ss is three lines and worth it.
    """

    def mmss(seconds: float) -> str:
        total = int(seconds)
        return f"{total // 60:02d}:{total % 60:02d}"

    def build(parts: list[dict], index: int) -> dict:
        return {
            "chunk_id": f"{file_id}:{index:04d}",
            "file_id": file_id,
            "session_id": session_id,
            "source": source,
            "kind": "video",
            "section": f"{mmss(parts[0]['start'])}-{mmss(parts[-1]['end'])}",
            "text": " ".join(part["text"] for part in parts),
            "order": index,
            "start": parts[0]["start"],
            "end": parts[-1]["end"],
        }

    chunks: list[dict] = []
    buffer: list[dict] = []
    fresh = 0        # segments in the buffer not yet part of an emitted chunk
    words = 0

    for segment in segments:
        buffer.append(segment)
        fresh += 1
        words += len(segment["text"].split())
        if words >= settings.chunk_words:
            chunks.append(build(buffer, len(chunks)))
            # Carry the last two segments into the next chunk as overlap.
            buffer = buffer[-2:]
            fresh = 0
            words = sum(len(part["text"].split()) for part in buffer)

    if fresh:
        chunks.append(build(buffer, len(chunks)))

    return chunks
