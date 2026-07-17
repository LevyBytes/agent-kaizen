"""Generate and recognize canonical Kaizen command-surface Markdown stubs."""

from __future__ import annotations

from pathlib import Path


STUB_TEMPLATE = """# {name}

This file is a Kaizen command surface, not the durable source of truth.

Use the Kaizen data-plane CLI to write and inspect records:

```powershell
python kaizen.py {list_code}
python kaizen.py {query_code} --query "search text"
python kaizen.py {inspect_code} --id <record-id>
```

Generated views or reports may be pasted below this stub when explicitly requested.
"""


def stub_for(filename: str) -> str:
    """Return the stub body for a markdown filename; maps known bases (GOTCHA/LEARNING/LEARNED) to their list/query/inspect CLI codes, else generic R3 fallback."""
    name = filename.rsplit(".", 1)[0] or filename
    if name == "GOTCHA":
        return STUB_TEMPLATE.format(name="GOTCHA", list_code="G2", query_code="G3", inspect_code="G4")
    if name == "LEARNING":
        return STUB_TEMPLATE.format(name="LEARNING", list_code="L4", query_code="L5", inspect_code="L6")
    if name == "LEARNED":
        return STUB_TEMPLATE.format(name="LEARNED", list_code="L7", query_code="L8", inspect_code="L9")
    return (
        f"# {name}\n\nNo canonical record family is registered for this command surface.\n\n"
        f"Use `python kaizen.py K0 --query \"{name}\" --json` to find the applicable operation.\n"
    )


def is_stub(path: Path) -> bool:
    """True iff path is an existing file whose text contains both stub marker phrases; reads utf-8-sig with errors="replace" (BOM-tolerant, non-raising boolean probe)."""
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return "This file is a Kaizen command surface" in text and "python kaizen.py" in text
