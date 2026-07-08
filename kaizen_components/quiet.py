"""Keep noisy optional-library output off stderr.

The CLI reserves stderr for the structured ``--json`` envelope, so any line a library leaks there
corrupts machine-readable output: HuggingFace/transformers download progress bars and load warnings,
pypdf ("EOF marker not found") and openpyxl ("Data Validation extension is not supported") parser
warnings, etc. Wrap the library call in ``quiet_stderr(...)`` to contain it.
"""

from __future__ import annotations

import contextlib
import io
import logging
import warnings
from collections.abc import Iterator


@contextlib.contextmanager
def quiet_stderr(*logger_names: str) -> Iterator[None]:
    """Silence the named loggers (to ERROR) and capture stray stderr + Python warnings for the block.

    Exceptions raised inside (e.g. ``KaizenDenied``) still propagate -- stderr is restored on exit, so
    the caller's structured error envelope prints cleanly.
    """
    for name in logger_names:
        logging.getLogger(name).setLevel(logging.ERROR)
    with contextlib.redirect_stderr(io.StringIO()), warnings.catch_warnings():
        warnings.simplefilter("ignore")
        yield
