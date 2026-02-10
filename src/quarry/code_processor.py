from __future__ import annotations

import logging
import re
from pathlib import Path

from quarry.models import PageContent, PageType
from quarry.text_processor import _read_text_with_fallback

logger = logging.getLogger(__name__)

# Extension → tree-sitter language name.
# Covers the top 20+ languages by popularity plus common config formats.
_CODE_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".cs": "c_sharp",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".scala": "scala",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".r": "r",
    ".R": "r",
    ".lua": "lua",
    ".pl": "perl",
    ".pm": "perl",
    ".ex": "elixir",
    ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".sql": "sql",
    ".zig": "zig",
    ".dart": "dart",
    ".nim": "nim",
    ".vue": "vue",
    ".svelte": "svelte",
}

SUPPORTED_CODE_EXTENSIONS = frozenset(_CODE_LANGUAGES)

# Tree-sitter node types considered "compound definitions" — each gets its
# own section.  Everything else (imports, assignments, comments) is grouped
# with adjacent small nodes.  This set covers the major languages without
# requiring per-language configuration.
_DEFINITION_NODE_TYPES = frozenset(
    {
        "call",
        "class",
        "class_declaration",
        "class_definition",
        "class_specifier",
        "constructor_declaration",
        "data",
        "decorated_definition",
        "enum_declaration",
        "enum_item",
        "enum_specifier",
        "export_statement",
        "extension_declaration",
        "function",
        "function_declaration",
        "function_definition",
        "function_item",
        "impl_item",
        "interface_declaration",
        "lexical_declaration",
        "method",
        "method_declaration",
        "mod_item",
        "module",
        "namespace_definition",
        "object_declaration",
        "protocol_declaration",
        "record_declaration",
        "struct_declaration",
        "struct_item",
        "struct_specifier",
        "template_declaration",
        "trait_item",
        "type_alias",
        "type_declaration",
    }
)


def process_code_file(file_path: Path) -> list[PageContent]:
    """Parse a source code file into semantic sections.

    Uses tree-sitter for language-aware splitting when available.
    Falls back to blank-line splitting otherwise.

    Args:
        file_path: Path to source code file.

    Returns:
        List of PageContent objects, one per top-level definition or
        group of small statements.

    Raises:
        ValueError: If file extension is not a supported code format.
        FileNotFoundError: If file does not exist.
    """
    suffix = file_path.suffix.lower()
    language = _CODE_LANGUAGES.get(suffix)
    if language is None:
        msg = f"Unsupported code format: {suffix}"
        raise ValueError(msg)

    text = _read_text_with_fallback(file_path)
    if not text.strip():
        return []

    document_name = file_path.name
    document_path = str(file_path.resolve())

    sections = _split_with_treesitter(text, language, document_name)
    if sections is None:
        sections = _fallback_split(text)

    return _sections_to_pages(sections, document_name, document_path)


def _split_with_treesitter(
    text: str,
    language: str,
    document_name: str,
) -> list[str] | None:
    """Split source code using tree-sitter AST.

    Returns None if tree-sitter is not installed or the language is
    not available, signaling the caller to use fallback splitting.
    """
    try:
        from tree_sitter_language_pack import get_parser  # noqa: PLC0415
    except ImportError:
        logger.info(
            "tree-sitter-language-pack not installed; "
            "falling back to plain splitting for %s",
            document_name,
        )
        return None

    try:
        parser = get_parser(language)  # type: ignore[arg-type]
    except (KeyError, ValueError, LookupError):
        logger.warning(
            "tree-sitter language %r not available for %s; "
            "falling back to plain splitting",
            language,
            document_name,
            exc_info=True,
        )
        return None

    tree = parser.parse(text.encode())
    root = tree.root_node

    sections: list[str] = []
    pending: list[str] = []

    for child in root.children:
        node_text = text[child.start_byte : child.end_byte].strip()
        if not node_text:
            continue

        if child.type in _DEFINITION_NODE_TYPES:
            if pending:
                sections.append("\n".join(pending))
                pending = []
            sections.append(node_text)
        else:
            pending.append(node_text)

    if pending:
        sections.append("\n".join(pending))

    return sections if sections else [text]


def _fallback_split(text: str) -> list[str]:
    """Split code on blank lines when tree-sitter is unavailable."""
    parts = re.split(r"\n\s*\n", text)
    return [p for p in parts if p.strip()]


def _sections_to_pages(
    sections: list[str],
    document_name: str,
    document_path: str,
) -> list[PageContent]:
    """Convert section strings to PageContent objects."""
    total = len(sections)
    return [
        PageContent(
            document_name=document_name,
            document_path=document_path,
            page_number=i + 1,
            total_pages=total,
            text=section,
            page_type=PageType.CODE,
        )
        for i, section in enumerate(sections)
    ]
