"""In-process GLiNER2 PII scanner (opt-in, no server).

Activated via ``KAIZEN_PII_MODEL`` / ``KAIZEN_PII_BACKEND``. An ADVISORY NER that augments -- never
replaces -- the deterministic regex redaction gate. The heavy import is deferred to first use; when
the opt-in extra is not installed the backend denies cleanly (pointing at ``requirements-pytorch.txt``).
Returns entity spans (label + char offsets); the caller hashes spans and never stores raw PII.
"""

from __future__ import annotations

import contextlib
import io
import os
import threading
from typing import Any

from ..denials import KaizenDenied
from .transformers_backend import _resolve_device as _resolve_torch_device


@contextlib.contextmanager
def _quiet():
    """Redirect the library's stdout/stderr to an in-memory sink during load/scan.

    Some gliner2 builds print a Unicode banner (e.g. an emoji) that raises
    UnicodeEncodeError on a non-UTF-8 console (Windows cp1252). Capturing it keeps
    the backend robust regardless of the terminal encoding.
    """
    from ..quiet import quiet_stderr

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), quiet_stderr("gliner2", "transformers"):
        yield

_DEFAULT_MODEL = "fastino/gliner2-privacy-filter-PII-multi"  # apache-2.0, ~0.3B, 42 PII types
# Deliberately narrow high-risk default; callers may opt into the model's broader label set.
_DEFAULT_LABELS = [
    "person",
    "email",
    "phone number",
    "street address",
    "credit card number",
    "social security number",
    "api key",
    "password",
    "date of birth",
    "ip address",
]


class Gliner2PiiBackend:
    """Advisory in-process GLiNER2 PII NER; opt-in; returns advisory char spans; augments, never replaces, the deterministic regex gate."""
    name = "gliner2"

    def __init__(self, *, model: str | None = None, labels: list[str] | None = None) -> None:
        self.model = model or _DEFAULT_MODEL
        self.labels = labels or _DEFAULT_LABELS
        self._model_obj: Any = None
        self._device: str | None = None
        self._load_lock = threading.Lock()

    def _load(self) -> Any:
        """Deferred heavy import + cached model load; raises KaizenDenied (DENIED_BACKEND_UNAVAILABLE when the extra is absent, DENIED_BACKEND_MODEL on load/download failure); moves to CUDA when resolved."""
        if self._model_obj is not None:
            return self._model_obj
        with self._load_lock:
            return self._load_locked()

    def _load_locked(self) -> Any:
        """Load exactly once while _load_lock is held."""
        if self._model_obj is not None:
            return self._model_obj
        try:
            with _quiet():
                from gliner2 import GLiNER2
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
        try:
            with _quiet():
                self._model_obj = GLiNER2.from_pretrained(self.model)
        except Exception as error:  # noqa: BLE001 -- bad model name / download failure
            raise KaizenDenied(
                "DENIED_BACKEND_MODEL",
                {
                    "backend": self.name,
                    "model": self.model,
                    "reason": str(error),
                    "required_action": "check the model name; first use downloads weights (set HF_HOME -- see setup/PYTORCH.md)",
                },
                exit_code=2,
            ) from error
        # GPU-first: GLiNER2 loads on CPU by default; move it to the resolved device.
        self._device = _resolve_torch_device(os.environ.get("KAIZEN_TORCH_DEVICE", "auto"))
        if self._device == "cuda":
            with _quiet():
                for target in (self._model_obj, getattr(self._model_obj, "model", None)):
                    if target is None or not hasattr(target, "to"):
                        continue
                    try:
                        target.to("cuda")
                        break
                    except Exception:  # noqa: BLE001 -- move unsupported -> stay on CPU
                        continue
        return self._model_obj

    def scan(self, text: str) -> list[dict[str, Any]]:
        """Return advisory PII spans; repeated dict matches advance by occurrence and missing entities yield no hits."""
        if not text:
            return []
        model = self._load()
        with _quiet():
            raw = model.extract_entities(text, self.labels)
        entities = raw.get("entities", raw) if isinstance(raw, dict) else raw
        hits: list[dict[str, Any]] = []
        if isinstance(entities, dict):
            # GLiNER2 shape: {label: [matched_text, ...]}; locate each match to a char span.
            next_start: dict[tuple[str, str], int] = {}
            for label, matches in entities.items():
                for match in matches or []:
                    if not isinstance(match, str):
                        continue
                    key = (str(label), match)
                    start = text.find(match, next_start.get(key, 0))
                    if start >= 0:
                        next_start[key] = start + len(match)
                    hits.append(
                        {
                            "label": str(label),
                            "start": start if start >= 0 else None,
                            "end": (start + len(match)) if start >= 0 else None,
                        }
                    )
        elif isinstance(entities, list):
            # Fallback for a list-of-spans shape: [{label|type, start, end}].
            for item in entities:
                if isinstance(item, dict):
                    hits.append(
                        {
                            "label": str(item.get("label") or item.get("type") or "pii"),
                            "start": item.get("start"),
                            "end": item.get("end"),
                        }
                    )
        return hits

    def probe(self) -> dict[str, Any]:
        """Warm-loads via a trivial `scan("probe")` (real inference) and returns backend/kind/model/device metadata; may raise the same KaizenDenied as `_load`."""
        self.scan("probe")
        return {"backend": self.name, "kind": "pii", "model": self.model, "device": self._device, "in_process": True}
