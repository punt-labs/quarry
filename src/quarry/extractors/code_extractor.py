"""Source code extraction: tree-sitter section splitting for 30+ languages."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Self, cast

from tree_sitter_language_pack import SupportedLanguage, get_parser

from quarry.ingestion.text_splitter import read_text_with_fallback, sections_to_pages
from quarry.models import PageContent, PageType

logger = logging.getLogger(__name__)

# Extension -> tree-sitter language name.
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

# Tree-sitter node types considered "compound definitions" -- each gets its
# own section.  Everything else (imports, assignments, comments) is grouped
# with adjacent small nodes.
_DEFINITION_NODE_TYPES = frozenset(
    {
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


class CodeExtractor:
    """Extract pages from source code files using tree-sitter.

    Implements ``FormatExtractor`` protocol.  Uses tree-sitter for
    language-aware splitting into semantic sections.  Falls back to
    blank-line splitting when the grammar is unavailable.
    """

    def __new__(cls) -> Self:
        return super().__new__(cls)

    def extract_pages(
        self,
        path: Path,
        *,
        document_name: str | None = None,
    ) -> list[PageContent]:
        """Parse a source code file into semantic sections."""
        suffix = path.suffix.lower()
        language = _CODE_LANGUAGES.get(suffix)
        if language is None:
            msg = f"Unsupported code format: {suffix}"
            raise ValueError(msg)

        text = read_text_with_fallback(path)
        if not text.strip():
            return []

        resolved_name = document_name or path.name
        document_path = str(path.resolve())

        sections = self._split_treesitter(text, language, resolved_name)
        if sections is None:
            sections = self._split_fallback(text)

        return sections_to_pages(sections, resolved_name, document_path, PageType.CODE)

    @staticmethod
    def _split_treesitter(
        text: str,
        language: str,
        document_name: str,
    ) -> list[str] | None:
        """Split source code using tree-sitter AST.

        Returns None if the language grammar is not available,
        signaling the caller to use fallback splitting.
        """
        try:
            parser = get_parser(cast("SupportedLanguage", language))
        except (KeyError, ValueError, LookupError):
            logger.info(
                "tree-sitter language %r not available for %s; "
                "falling back to plain splitting",
                language,
                document_name,
            )
            return None

        source_bytes = text.encode("utf-8")
        tree = parser.parse(source_bytes)
        root = tree.root_node

        sections: list[str] = []
        pending: list[str] = []

        for child in root.children:
            raw = source_bytes[child.start_byte : child.end_byte]
            node_text = raw.decode("utf-8").strip()
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

    @staticmethod
    def _split_fallback(text: str) -> list[str]:
        """Split code on blank lines when tree-sitter is unavailable."""
        parts = re.split(r"\n\s*\n", text)
        return [p for p in parts if p.strip()]
