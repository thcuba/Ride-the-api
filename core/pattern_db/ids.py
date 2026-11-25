"""Stable identifiers for learned patterns.

The id used to be ``{device_id}_{kind}_{md5(path)[:8]}``: method and protocol were not
part of it, so two different commands sharing a path — ``GET /api/ctl`` and
``POST /api/ctl`` — produced the *same* id, and the second write overwrote the first
(``path_pattern``, ``body_schema``, status code and template). The digest was 32 bits,
cheap to collide on purpose, and the path comes from LLM output that the device's own
traffic can influence.

Two functions keep this useful instead of disruptive:

* :func:`pattern_id` builds the identity from the canonical tuple
  ``(kind, method, protocol, path)`` with 64 bits of SHA-256.
* :func:`resolve_pattern_id` decides which id a command should be written under,
  reusing a row learned by an older version when it really is the same command, so an
  existing device does not get every pattern duplicated on the first re-flush.
"""

from __future__ import annotations

import hashlib

#: Length of the digest in ids written before method and protocol were part of the
#: identity. Only used to find (and reuse) those rows.
LEGACY_DIGEST_CHARS = 8

#: 64 bits: unlike the old 32, a deliberate collision is out of reach.
DIGEST_CHARS = 16

#: Separator inside the canonical string. A unit separator cannot appear in a path,
#: a method or a protocol, so two different tuples can never serialise the same way.
_SEP = "\x1f"


def pattern_id(
    device_id: str,
    kind: str,
    method: str | None = None,
    protocol: str | None = None,
    path: str | None = None,
) -> str:
    """Identity of a learned pattern: device, kind, method, protocol and path.

    Kept readable on purpose (``kind`` stays in the id) so a row can still be
    identified by eye in the database and in the ``.ride-pattern.json`` export.
    """
    canonical = _SEP.join(
        [
            str(kind or ""),
            str(method or "").upper(),
            str(protocol or "").lower(),
            str(path or ""),
        ]
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:DIGEST_CHARS]
    return f"{device_id}_{kind}_{digest}"


def legacy_pattern_id(device_id: str, kind: str, path: str | None = None) -> str:
    """The id this pattern would have had before method and protocol counted."""
    digest = hashlib.md5(str(path or "").encode("utf-8")).hexdigest()[:LEGACY_DIGEST_CHARS]
    return f"{device_id}_{kind}_{digest}"


def resolve_pattern_id(  # noqa: PLR0913 — keyword-only, one argument per identity field
    device_id: str,
    kind: str,
    *,
    method: str | None = None,
    protocol: str | None = None,
    path: str | None = None,
    existing: dict[str, str] | None = None,
) -> str:
    """The id to write this command under, reusing a pre-existing row when it is the same.

    ``existing`` maps an already-persisted pattern id to the method stored on that row
    (empty string when the row carries none). A row written by an older version carries
    no method in its id, so it is reused **only when its stored method matches** the one
    being learned: a pattern that is merely being re-learned keeps its row and its
    templates, while two commands that used to collapse into a single id are now kept
    apart. Without that check the reuse would reproduce the original bug, because the
    legacy id is identical for both. A row with no stored method is ambiguous — it is
    not reused, so nothing is overwritten on a guess.
    """
    new_id = pattern_id(device_id, kind, method, protocol, path)
    if not existing or new_id in existing:
        return new_id

    legacy = legacy_pattern_id(device_id, kind, path)
    stored_method = existing.get(legacy)
    if stored_method is None:
        return new_id

    if str(stored_method or "").upper() == str(method or "").upper():
        return legacy

    return new_id
