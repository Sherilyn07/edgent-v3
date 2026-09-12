"""Running the language model.

STUDENT WORK — two functions.

No optimization here of any kind: one request at a time, full precision, no
batching, no caching beyond what transformers does by itself. It will be slow.
Measure how slow, and write the number down -- everything you learn later about
making inference fast gets measured against this.
"""
import logging
import time

from .config import get_settings, resolve_device, resolve_dtype

# Note on imports: torch and transformers are imported *inside* the two
# functions below, not at the top of this file. That is not normal Python
# style, and it is deliberate here -- it means the service still starts, and
# /health still answers, before you have installed anything heavy. Put
# `import torch` as the first line of each function that needs it.

log = logging.getLogger(__name__)
settings = get_settings()

_tokenizer = None
_model = None


def load_model():
    """Load the tokenizer and the model once.

    `time` is already imported at the top of this file; torch is not (see the
    note above).

    What to write:

        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        _tokenizer = AutoTokenizer.from_pretrained(settings.llm_model)
        _model = AutoModelForCausalLM.from_pretrained(
            settings.llm_model,
            torch_dtype=getattr(torch, settings.dtype),
            device_map=settings.device,
        )
        _model.eval()

    (with `global _tokenizer, _model`, and return them both.)

    The first run downloads about 2.2 GB. Watch your memory while it loads --
    seeing a 1.1B model take roughly 4.4 GB in float32 makes the arithmetic
    concrete: four bytes per parameter, plus room to work.

    Log how long the load takes. On a laptop it is often thirty seconds or
    more, which is exactly why we do it once at startup and not per request.
    """
    global _tokenizer, _model
    if _model is None:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        device = resolve_device(settings.device)
        dtype = resolve_dtype(settings.dtype, device)
        log.info("loading %s on %s (%s)", settings.llm_model, device, dtype)
        started = time.time()
        _tokenizer = AutoTokenizer.from_pretrained(settings.llm_model)
        _model = AutoModelForCausalLM.from_pretrained(
            settings.llm_model,
            torch_dtype=getattr(torch, dtype),
            device_map=device,
        )
        _model.eval()
        log.info("model loaded in %.1fs", time.time() - started)
    return _tokenizer, _model


def generate(prompt: str, max_new_tokens: int, temperature: float) -> dict:
    """Answer one prompt. Returns {"content": str, "usage": {...}}.

    What to write:

      0. `import torch` — this function needs it too.

      1. tokenizer, model = load_model()

      2. TinyLlama is a *chat* model, so it expects its input wrapped in the
         special markers it was trained on. Do not hand it a bare string:

             messages = [{"role": "user", "content": prompt}]
             text = tokenizer.apply_chat_template(
                 messages, tokenize=False, add_generation_prompt=True)

         `add_generation_prompt=True` appends the marker that means "your turn
         now". Leave it off and the model often just carries on writing the
         user's message.

      3. inputs = tokenizer(text, return_tensors="pt",
                            truncation=True,
                            max_length=settings.max_input_tokens).to(model.device)

         The truncation matters. TinyLlama's whole context is 2048 tokens, and
         four retrieved chunks plus a conversation will overrun it easily. When
         that happens you will see a very confused answer rather than an error,
         which is a hard bug to spot.

      4. Time it, then generate:

             started = time.time()
             with torch.inference_mode():
                 output = model.generate(
                     **inputs,
                     max_new_tokens=max_new_tokens,
                     temperature=temperature,
                     do_sample=temperature > 0,
                     pad_token_id=tokenizer.eos_token_id,
                 )
             elapsed = time.time() - started

      5. `output` contains the prompt *and* the answer. Slice the prompt off,
         or the user sees their own question read back to them:

             prompt_tokens = inputs["input_ids"].shape[-1]
             answer_ids = output[0][prompt_tokens:]
             content = tokenizer.decode(answer_ids, skip_special_tokens=True).strip()

      6. Return the text together with the numbers:

             {"content": content,
              "usage": {"prompt_tokens": ..., "completion_tokens": ...,
                        "seconds": round(elapsed, 2),
                        "tokens_per_second": ...}}

    Those numbers are the point of this exercise as much as the answer is. Log
    them on every call. `tokens_per_second` on an unoptimized CPU run is the
    baseline the whole second half of this course is spent beating.
    """
    import torch

    tokenizer, model = load_model()

    messages = [{"role": "user", "content": prompt}]
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)

    inputs = tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=settings.max_input_tokens,
    ).to(model.device)

    do_sample = temperature > 0
    generate_kwargs = {
        "max_new_tokens": max_new_tokens,
        "do_sample": do_sample,
        "pad_token_id": tokenizer.eos_token_id,
    }
    if do_sample:
        generate_kwargs["temperature"] = temperature

    started = time.time()
    with torch.inference_mode():
        output = model.generate(**inputs, **generate_kwargs)
    elapsed = time.time() - started

    prompt_tokens = int(inputs["input_ids"].shape[-1])
    answer_ids = output[0][prompt_tokens:]
    content = tokenizer.decode(answer_ids, skip_special_tokens=True).strip()
    completion_tokens = int(answer_ids.shape[-1])

    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "seconds": round(elapsed, 2),
        "tokens_per_second": round(completion_tokens / elapsed, 2) if elapsed > 0 else 0.0,
    }
    log.info("generate: %s", usage)
    return {"content": content, "usage": usage}
