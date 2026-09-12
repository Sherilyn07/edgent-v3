"""Turning text into numbers.

STUDENT WORK — two functions.

An embedding is a list of numbers that stands for a piece of text's meaning.
Two texts about the same thing end up with similar lists, even when they share
no words at all. That is the trick that makes search-by-meaning possible.

We use the plain `transformers` library rather than a wrapper, because doing
the pooling yourself is the only way to understand what a sentence embedding
actually is.
"""
import logging

from .config import get_settings, resolve_device

# torch and transformers are imported *inside* the functions below rather than
# at the top of this file, so the service starts and answers /health before you
# have installed anything heavy. Put `import torch` as the first line of each
# function that needs it.

log = logging.getLogger(__name__)
settings = get_settings()

_tokenizer = None
_model = None
_device = None


def load_model() -> None:
    """Load the tokenizer and the model once, when the service starts.

    What to write:

        from transformers import AutoModel, AutoTokenizer

        _tokenizer = AutoTokenizer.from_pretrained(settings.embed_model)
        _model = AutoModel.from_pretrained(settings.embed_model)
        _model.to(settings.device)
        _model.eval()

    Remember `global _tokenizer, _model` at the top, or you will assign to
    local variables and wonder why nothing works.

    The first run downloads about 90 MB. After that it is cached in
    ~/.cache/huggingface and startup is instant.

    `.eval()` matters: it switches off dropout. Without it you get slightly
    different numbers every time you embed the same text, which makes for a
    memorable afternoon of debugging.
    """
    global _tokenizer, _model, _device
    if _model is not None:
        return

    from transformers import AutoModel, AutoTokenizer

    _device = resolve_device(settings.device)
    log.info("loading embedding model %s on %s", settings.embed_model, _device)
    _tokenizer = AutoTokenizer.from_pretrained(settings.embed_model)
    _model = AutoModel.from_pretrained(settings.embed_model)
    _model.to(_device)
    _model.eval()


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Turn a list of strings into a list of vectors.

    Four steps. Take them one at a time:

      1. TOKENIZE
             batch = _tokenizer(texts, padding=True, truncation=True,
                                max_length=settings.max_length,
                                return_tensors="pt")
         Padding makes every sequence in the batch the same length. Truncation
         cuts anything too long. Both matter, and step 3 is where padding comes
         back to bite you.

      2. RUN THE MODEL
             import torch
             with torch.no_grad():
                 output = _model(**batch)
         `no_grad` tells torch we are not training, which halves the memory.

         output.last_hidden_state has shape [batch, tokens, 384] -- one vector
         per *token*, not per text.

      3. POOL
         We need one vector per text, so average the token vectors. But the
         padding tokens are not real, and averaging them in drags every vector
         toward the same meaningless middle. So use the attention mask:

             mask = batch["attention_mask"].unsqueeze(-1).float()
             summed = (output.last_hidden_state * mask).sum(dim=1)
             counts = mask.sum(dim=1).clamp(min=1e-9)
             pooled = summed / counts

         This is "mean pooling with attention masking", and it is the single
         most common thing people get wrong when they first do this by hand.

      4. NORMALISE
             pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
         Scale every vector to length 1. Once they are all the same length,
         cosine similarity is just a dot product, which is why every vector
         database wants normalised input.

    Then return `pooled.cpu().tolist()`.

    Do this in batches of settings.batch_size. A thousand chunks in one call
    will run your laptop out of memory.
    """
    import torch

    if _model is None:
        load_model()

    vectors: list[list[float]] = []
    for start in range(0, len(texts), settings.batch_size):
        batch_texts = texts[start : start + settings.batch_size]

        batch = _tokenizer(
            batch_texts,
            padding=True,
            truncation=True,
            max_length=settings.max_length,
            return_tensors="pt",
        ).to(_device)

        with torch.no_grad():
            output = _model(**batch)

        mask = batch["attention_mask"].unsqueeze(-1).float()
        summed = (output.last_hidden_state * mask).sum(dim=1)
        counts = mask.sum(dim=1).clamp(min=1e-9)
        pooled = summed / counts

        pooled = torch.nn.functional.normalize(pooled, p=2, dim=1)
        vectors.extend(pooled.cpu().tolist())

    return vectors
