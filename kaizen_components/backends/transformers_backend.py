"""In-process transformers text backend (opt-in, no server).

Activated via ``KAIZEN_TEXT_BACKEND=transformers``. Runs a local instruct model for advisory text
(B2) and the advisory LLM-judge (B4/O4) without an Ollama server, satisfying the same ``TextBackend``
protocol. The heavy import is deferred to first use; when the opt-in extra is not installed the
backend denies cleanly (pointing at ``requirements-pytorch.txt``). GPU-first: ``KAIZEN_TORCH_DEVICE``
= ``auto`` prefers CUDA and falls back to CPU, where configured model dtypes may be slower or unsupported. Generation is greedy (deterministic). Advisory only --
never an acceptance authority.
"""

from __future__ import annotations

import os
from typing import Any

from ..denials import KaizenDenied

_DEFAULT_MODEL = "Qwen/Qwen3.5-9B"  # apache-2.0; the lighter path is its GGUF Q4_K_M via Ollama
_DEFAULT_MAX_NEW_TOKENS = 256


def _resolve_device(spec: str | None) -> str:
    """Accepts `cpu`/`cuda`/`auto`(default); `auto` probes torch, CPU when torch absent or no CUDA."""
    spec = (spec or "auto").strip().lower()
    if spec in ("cpu", "cuda"):
        return spec
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001 -- torch absent -> CPU is the only option anyway
        return "cpu"


class TransformersTextBackend:
    """Opt-in in-process `TextBackend`; lazy heavy import; advisory-only, never an acceptance authority."""
    name = "transformers"

    def __init__(
        self,
        *,
        model: str | None = None,
        device: str | None = None,
        max_new_tokens: int | None = None,
    ) -> None:
        """Device` falls back to `KAIZEN_TORCH_DEVICE`; `model`/`max_new_tokens` to module defaults."""
        self.model = model or _DEFAULT_MODEL
        self.device_spec = device or os.environ.get("KAIZEN_TORCH_DEVICE", "auto")
        self.max_new_tokens = max_new_tokens or _DEFAULT_MAX_NEW_TOKENS
        self._tokenizer: Any = None
        self._model_obj: Any = None
        self._torch: Any = None
        self._device: str | None = None

    def _load(self) -> None:
        """Idempotent lazy load; two denials — `DENIED_BACKEND_UNAVAILABLE` (missing extra) vs `DENIED_BACKEND_MODEL` (bad name/download/OOM)."""
        if self._model_obj is not None:
            return
        from ..quiet import quiet_stderr

        try:
            with quiet_stderr("transformers"):
                import torch  # noqa: F401
                from transformers import AutoModelForCausalLM, AutoTokenizer
        except Exception as error:  # noqa: BLE001 -- extra not installed
            raise KaizenDenied(
                "DENIED_BACKEND_UNAVAILABLE",
                {
                    "backend": self.name,
                    "reason": str(error),
                    "required_action": "install the opt-in extra: pip install -r requirements-pytorch.txt (see setup/PYTORCH.md)",
                },
                exit_code=2,
            ) from error
        self._device = _resolve_device(self.device_spec)
        self._torch = torch
        try:
            with quiet_stderr("transformers"):
                self._tokenizer = AutoTokenizer.from_pretrained(self.model)
                self._model_obj = AutoModelForCausalLM.from_pretrained(
                    self.model,
                    dtype=torch.float32 if self._device == "cpu" else "auto",
                    device_map="auto" if self._device == "cuda" else None,
                )
                if self._device == "cpu":
                    self._model_obj = self._model_obj.to("cpu")
        except Exception as error:  # noqa: BLE001 -- bad model name / download / OOM
            raise KaizenDenied(
                "DENIED_BACKEND_MODEL",
                {
                    "backend": self.name,
                    "model": self.model,
                    "device": self._device,
                    "reason": str(error),
                    "required_action": "check the model name and VRAM budget; first use downloads weights (set HF_HOME -- see setup/PYTORCH.md)",
                },
                exit_code=2,
            ) from error

    def chat(self, prompt: str, **opts: Any) -> dict[str, Any]:
        """Returns `{text, usage{prompt,completion,total}, model}`; greedy/deterministic; honors `max_tokens`/`max_new_tokens`."""
        self._load()

        messages = [{"role": "user", "content": prompt}]
        encoded = self._tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt"
        )
        if isinstance(encoded, dict):
            model_inputs = {key: value.to(self._model_obj.device) for key, value in encoded.items()}
            input_ids = model_inputs.get("input_ids")
            if input_ids is None:
                raise KaizenDenied(
                    "DENIED_BACKEND_MALFORMED",
                    {"backend": self.name, "reason": "tokenizer output omitted input_ids"},
                    exit_code=2,
                )
        else:
            input_ids = encoded.to(self._model_obj.device)
            model_inputs = {"input_ids": input_ids}
        prompt_tokens = int(input_ids.shape[-1])
        requested_max = opts.get("max_tokens")
        if requested_max is None:
            requested_max = opts.get("max_new_tokens")
        max_new = int(self.max_new_tokens if requested_max is None else requested_max)
        if max_new <= 0:
            raise KaizenDenied(
                "DENIED_BACKEND_OPTIONS",
                {"max_new_tokens": max_new, "required_action": "max_tokens must be a positive integer"},
                exit_code=2,
            )
        pad_token_id = self._tokenizer.eos_token_id
        if pad_token_id is None:
            pad_token_id = self._tokenizer.pad_token_id
        generate_opts: dict[str, Any] = {}
        if pad_token_id is not None:
            generate_opts["pad_token_id"] = pad_token_id
        with self._torch.no_grad():
            generated = self._model_obj.generate(
                **model_inputs,
                max_new_tokens=max_new,
                do_sample=False,  # greedy -> deterministic advisory output
                **generate_opts,
            )
        completion = generated[0][prompt_tokens:]
        completion_tokens = int(completion.shape[-1])
        text = self._tokenizer.decode(completion, skip_special_tokens=True)
        return {
            "text": text,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "model": self.model,
        }

    def probe(self) -> dict[str, Any]:
        """Promote inline comment: availability = load-only, no warm-up generate; returns lane descriptor."""
        self._load()  # loading the model is the availability check; avoids a slow warm-up generate
        return {"backend": self.name, "kind": "text", "model": self.model, "device": self._device, "in_process": True}
