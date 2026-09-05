"""A copy of a judge's response that survives the stage's own repairs.

Each stage repairs a short list of cosmetic drift in its judge's JSON before
validating it (the _normalize_*_response functions), and on a validation
failure the ledger records what the judge actually wrote, not the repaired
copy. That record used to be a deep copy, which recursed into every field
and raised RecursionError on an unknown field nested a few hundred levels
deep, turning an otherwise valid response into a pipeline error even
though the validators tolerate unknown fields and the ledger can serialize
far deeper than a deep copy can walk.

The normalizers only ever write to the top level of the response and to
the dicts inside one list (findings, rebuttals). So the snapshot copies
exactly that much, one dict at the top and one per list item, and shares
everything else by reference. Nothing the normalizers leave alone is
copied, and nothing they touch is shared, which is the whole invariant.
"""

from typing import Any


def snapshot_response(response: dict[str, Any], *list_fields: str) -> dict[str, Any]:
    """Return a copy of ``response`` isolated from the normalizer's edits.

    ``list_fields`` names the lists whose dict items a normalizer edits in
    place. Each such list is rebuilt with a shallow copy of every dict in it;
    non-dict items pass through. Every other value is shared by reference,
    so a field of any shape or depth costs nothing to keep and cannot fail.
    """
    snapshot: dict[str, Any] = dict(response)
    for field in list_fields:
        items: Any = response.get(field)
        if isinstance(items, list):
            snapshot[field] = [
                dict(item) if isinstance(item, dict) else item for item in items
            ]
    return snapshot
