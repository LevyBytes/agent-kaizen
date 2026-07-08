"""In-process transformers text backend (opt-in, no server).

Activated via ``KAIZEN_TEXT_BACKEND=transformers``. Runs a local instruct model for advisory text
(B2) and the advisory LLM-judge (B4/O4) without an Ollama server, satisfying the same ``TextBackend``
protocol. The heavy import is deferred to first use; when the opt-in extra is not installed the
backend denies cleanly (pointing at ``requirements-pytorch.txt``). GPU-first: ``KAIZEN_TORCH_DEVICE``
= ``auto`` prefers CUDA and falls back to CPU. Generation is greedy (deterministic). Advisory only --
never an acceptance authority.
"""

from __future__ import annotations

import os
from typing import Any

from ..denials import KaizenDenied

_DEFAULT_MODEL = "Qwen/Qwen3.5-9B"  # apache-2.0; the lighter path is its GGUF Q4_K_M via Ollama
_DEFAULT_MAX_NEW_TOKENS = 256


def _resolve_device(spec: str | None) -> str:
    spec = (spec or "auto").strip().lower()
    if spec in ("cpu", "cuda"):
        return spec
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001 -- torch absent -> CPU is the only option anyway
        return "cpu"


class TransformersTextBackend:
    name = "transformers"

    def __init__(
        self,
        *,
        model: str | None = None,
        device: str | None = None,
        max_new_tokens: int | None = None,
    ) -> None:
        self.model = model or _DEFAULT_MODEL
        self.device_spec = device or os.environ.get("KAIZEN_TORCH_DEVICE", "auto")
        self.max_new_tokens = max_new_tokens or _DEFAULT_MAX_NEW_TOKENS
        self._tokenizer: Any = None
        self._model_obj: Any = None
        self._device: str | None = None

    def _load(self) -> None:
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
        try:
            with quiet_stderr("transformers"):
                self._tokenizer = AutoTokenizer.from_pretrained(self.model)
                self._model_obj = AutoModelForCausalLM.from_pretrained(
                    self.model,
                    torch_dtype="auto",
                    device_map=self._device if self._device == "cuda" else None,
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
        self._load()
        import torch

        messages = [{"role": "user", "content": prompt}]
        inputs = self._tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt"
        ).to(self._model_obj.device)
        prompt_tokens = int(inputs.shape[-1])
        max_new = int(opts.get("max_tokens") or opts.get("max_new_tokens") or self.max_new_tokens)
        with torch.no_grad():
            generated = self._model_obj.generate(
                inputs,
                max_new_tokens=max_new,
                do_sample=False,  # greedy -> deterministic advisory output
                pad_token_id=self._tokenizer.eos_token_id,
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
        self._load()  # loading the model is the availability check; avoids a slow warm-up generate
        return {"backend": self.name, "kind": "text", "model": self.model, "device": self._device, "in_process": True}
