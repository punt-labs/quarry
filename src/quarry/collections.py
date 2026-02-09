from __future__ import annotations

from pathlib import Path


def derive_collection(
    file_path: Path,
    explicit: str | None = None,
) -> str:
    """Derive a collection name from a file path or explicit override.

    Args:
        file_path: Path to the document being ingested.
        explicit: If provided, use this as the collection name.

    Returns:
        Validated collection name.

    Raises:
        ValueError: If the derived or explicit name is invalid.
    """
    if explicit is not None:
        return validate_collection_name(explicit)
    return validate_collection_name(file_path.resolve().parent.name)


def validate_collection_name(name: str) -> str:
    """Validate and normalize a collection name.

    Strips whitespace. Rejects empty strings and names containing
    single quotes (which would break SQL predicates).

    Args:
        name: Raw collection name.

    Returns:
        Validated collection name.

    Raises:
        ValueError: If name is empty or contains single quotes.
    """
    name = name.strip()
    if not name:
        msg = "Collection name must not be empty"
        raise ValueError(msg)
    if "'" in name:
        msg = f"Collection name must not contain single quotes: {name!r}"
        raise ValueError(msg)
    return name
