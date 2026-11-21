"""Entity type, relation type, and name standardisation for JKG.

The rules are intentionally conservative. Over-normalising ("Alice Smith" vs
"Alice-Smith" vs "A. Smith") would merge distinct people; under-normalising
would create duplicate nodes for trivial spacing/case differences. v1 only
removes differences that never carry meaning:

* Unicode NFKC (full-width -> ASCII, compatibility forms)
* remove control/format characters (zero-width joiners, BOMs)
* collapse any run of whitespace to one space and trim the ends
* Unicode casefold for the dedupe key

``canonical_name`` keeps the original case; ``normalized_key`` is casefolded.
Both are bounded so a pathological client payload cannot create an unbounded
index key.
"""
from __future__ import annotations

import json
import unicodedata
from typing import Any, Dict, Optional

from ..core.errors import ValidationError
from .errors import JkgInvalidMetadataError, JkgInvalidRelationError

MAX_ENTITY_NAME_LENGTH = 200
MAX_NORMALIZED_KEY_LENGTH = 400
MAX_METADATA_BYTES = 16_384

ENTITY_TYPES = ("PERSON", "PROJECT", "EVENT")
RELATION_TYPES = ("WORKS_ON", "ATTENDS", "BELONGS_TO", "KNOWS")

# Frozen direction contract (Authority Spec §6.2).
RELATION_ENDPOINTS: Dict[str, tuple[str, str]] = {
    "WORKS_ON": ("PERSON", "PROJECT"),
    "ATTENDS": ("PERSON", "EVENT"),
    "BELONGS_TO": ("EVENT", "PROJECT"),
    "KNOWS": ("PERSON", "PERSON"),
}

# Source memories in these states still support a derived fact. ARCHIVED is a
# reversible lifecycle state (the memory is hidden from default lists but not
# forgotten); DELETED and MERGED are terminal for the original row.
VALID_SOURCE_STATUSES = frozenset({"ACTIVE", "ARCHIVED"})
INVALID_SOURCE_STATUSES = frozenset({"DELETED", "MERGED"})


def _strip_control_characters(value: str) -> str:
    return "".join(
        char
        for char in value
        if not unicodedata.category(char).startswith("C")
    )


def canonicalize_entity_name(name: str) -> str:
    """Return the stable display form of an entity name.

    Raises ``ValidationError`` for non-strings, empty values, or names longer
    than the configured bound.
    """
    if not isinstance(name, str):
        raise ValidationError("Entity name must be a string")
    value = unicodedata.normalize("NFKC", name)
    value = _strip_control_characters(value)
    value = " ".join(value.split())
    if not value:
        raise ValidationError("Entity name must not be empty")
    if len(value) > MAX_ENTITY_NAME_LENGTH:
        raise ValidationError(
            f"Entity name exceeds {MAX_ENTITY_NAME_LENGTH} characters"
        )
    return value


def normalize_entity_name(name: str) -> str:
    """Return the dedupe key for an entity name."""
    canonical = canonicalize_entity_name(name)
    normalized = canonical.casefold()
    if len(normalized) > MAX_NORMALIZED_KEY_LENGTH:
        raise ValidationError(
            f"Normalized entity name exceeds {MAX_NORMALIZED_KEY_LENGTH} characters"
        )
    return normalized


def validate_entity_type(entity_type: Any) -> str:
    """Return the canonical uppercase entity type or raise."""
    if hasattr(entity_type, "value"):
        entity_type = entity_type.value
    value = str(entity_type or "").strip().upper()
    if value not in ENTITY_TYPES:
        raise ValidationError(
            "Unsupported entity type",
            detail={"entity_type": value, "allowed": list(ENTITY_TYPES)},
        )
    return value


def validate_relation_type(predicate: Any) -> str:
    """Return the canonical uppercase predicate or raise."""
    if hasattr(predicate, "value"):
        predicate = predicate.value
    value = str(predicate or "").strip().upper()
    if value not in RELATION_TYPES:
        raise JkgInvalidRelationError(
            detail={"predicate": value, "allowed": list(RELATION_TYPES)}
        )
    return value


def validate_endpoint_types(
    predicate: str, subject_type: str, object_type: str
) -> None:
    """Enforce the frozen subject/object type contract for a predicate."""
    expected_subject, expected_object = RELATION_ENDPOINTS[predicate]
    if subject_type != expected_subject or object_type != expected_object:
        from .errors import JkgEdgeTypeMismatchError

        raise JkgEdgeTypeMismatchError(
            detail={
                "predicate": predicate,
                "expected": {
                    "subject": expected_subject,
                    "object": expected_object,
                },
                "actual": {"subject": subject_type, "object": object_type},
            }
        )


def serialize_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[str]:
    """Validate and serialise a metadata object.

    Only a JSON object is accepted. The serialised bound is enforced because
    the column is TEXT and an unbounded payload would bloat every graph read.
    """
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise JkgInvalidMetadataError("Metadata must be a JSON object")
    try:
        serialized = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise JkgInvalidMetadataError(
            "Metadata is not JSON-serialisable"
        ) from exc
    if len(serialized.encode("utf-8")) > MAX_METADATA_BYTES:
        raise JkgInvalidMetadataError(
            f"Metadata exceeds {MAX_METADATA_BYTES} bytes"
        )
    return serialized


def deserialize_metadata(value: Optional[str]) -> Optional[Dict[str, Any]]:
    """Best-effort read of stored metadata; corrupt values become None."""
    if not value:
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = [
    "ENTITY_TYPES",
    "RELATION_TYPES",
    "RELATION_ENDPOINTS",
    "VALID_SOURCE_STATUSES",
    "INVALID_SOURCE_STATUSES",
    "MAX_ENTITY_NAME_LENGTH",
    "MAX_NORMALIZED_KEY_LENGTH",
    "MAX_METADATA_BYTES",
    "canonicalize_entity_name",
    "normalize_entity_name",
    "validate_entity_type",
    "validate_relation_type",
    "validate_endpoint_types",
    "serialize_metadata",
    "deserialize_metadata",
]
