"""Every RefusalError construction in src/ builds its message from fixed text.

A refusal's message is returned to a client and logged without a traceback, so a
construction must not put caller-supplied text in it (a collection name, a URL, a
path).  The sweep finds every construction of a ``RefusalError`` subclass: by name,
by attribute, and as ``cls(...)``, ``self.__class__(...)`` or ``type(self)(...)``
inside a subclass; with the message positional or as ``msg=``, ``message=`` or
``reason=``.  A message that is a constant (a literal, a conditional of literals,
an f-string with no placeholders, literals concatenated) passes.  Anything else is
counted per file and must match the reviewed table below, so a new construction
that interpolates fails until someone justifies it here.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

from quarry.content_guard import BinaryContent

_SRC = Path(__file__).resolve().parent.parent / "src" / "quarry"
_MESSAGE_KEYWORDS = frozenset({"msg", "message", "reason"})

# Helpers that build a message from numbers alone; each is pinned by a test below.
_PINNED_HELPERS = frozenset({"_ratio_message"})

# file (relative to src/quarry) -> how many constructions interpolate, and why each is
# safe.  Only counts and sizes, a column name, a page number, a sniffed-format or
# media-type label that its owner sanitises, an UrlAdmission reason (a rule or a host
# name), a rule text from the SSRF policy, a status code, or a FailureReason.
# Never a caller-supplied name, URL, path or credential.
_ALLOWED: dict[str, int] = {
    "content_guard.py": 2,
    "db/row_budget.py": 1,
    "ingestion/opened_response.py": 2,
    "ingestion/page_report.py": 1,
    "ingestion/page_splitter.py": 1,
    "ingestion/media_type_policy.py": 1,
    "ingestion/fetched_body.py": 3,
    "ingestion/fetch_refused.py": 4,
    "daemon/refusal_aware_terminal.py": 1,
    "extractors/sheet_tables.py": 0,
}


def _refusal_classes(trees: dict[Path, ast.Module]) -> set[str]:
    names = {"RefusalError"}
    grew = True
    while grew:
        grew = False
        for tree in trees.values():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ClassDef)
                    and node.name not in names
                    and any(
                        isinstance(b, ast.Name) and b.id in names for b in node.bases
                    )
                ):
                    names.add(node.name)
                    grew = True
    return names


def _is_constant(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Constant):
        return True
    if isinstance(expr, ast.JoinedStr):
        return not any(isinstance(v, ast.FormattedValue) for v in expr.values)
    if isinstance(expr, ast.IfExp):
        return _is_constant(expr.body) and _is_constant(expr.orelse)
    if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
        return _is_constant(expr.left) and _is_constant(expr.right)
    return False


def _is_pinned_helper(expr: ast.expr) -> bool:
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr in _PINNED_HELPERS
    )


def _constructs_refusal(call: ast.Call, classes: set[str], *, inside: bool) -> bool:
    fn = call.func
    if isinstance(fn, ast.Name):
        return fn.id in classes or (inside and fn.id == "cls")
    if isinstance(fn, ast.Attribute):
        is_self_class = fn.attr == "__class__" and isinstance(fn.value, ast.Name)
        return fn.attr in classes or (inside and is_self_class)
    if isinstance(fn, ast.Call) and isinstance(fn.func, ast.Name):
        return inside and fn.func.id == "type"
    return False


def _message(call: ast.Call) -> ast.expr | None:
    if call.args:
        return call.args[0]
    for keyword in call.keywords:
        if keyword.arg in _MESSAGE_KEYWORDS:
            return keyword.value
    return None


def _resolved(expr: ast.expr, assigns: list[tuple[int, str, ast.expr]], line: int):
    if isinstance(expr, ast.Name):
        prior = [v for ln, nm, v in assigns if nm == expr.id and ln < line]
        return prior or [expr]
    return [expr]


def _scan(tree: ast.Module, classes: set[str]) -> int:
    """Count the constructions in *tree* whose message is not a safe constant."""
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }

    def nearest(node: ast.AST, kinds: tuple[type, ...]) -> ast.AST | None:
        while node in parents:
            node = parents[node]
            if isinstance(node, kinds):
                return node
        return None

    count = 0
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        owner = nearest(call, (ast.ClassDef,))
        inside = isinstance(owner, ast.ClassDef) and owner.name in classes
        if not _constructs_refusal(call, classes, inside=inside):
            continue
        message = _message(call)
        if message is None:
            continue
        func = nearest(call, (ast.FunctionDef, ast.AsyncFunctionDef))
        assigns = [
            (0 if node in tree.body else node.lineno, node.targets[0].id, node.value)
            for node in ast.walk(func or tree)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ] + [
            (0, node.targets[0].id, node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ]
        for value in _resolved(message, assigns, call.lineno):
            if not (_is_constant(value) or _is_pinned_helper(value)):
                count += 1
    return count


def _dynamic_sites() -> Counter[str]:
    trees = {p: ast.parse(p.read_text()) for p in _SRC.rglob("*.py")}
    classes = _refusal_classes(trees)
    found: Counter[str] = Counter()
    for path, tree in trees.items():
        found[path.relative_to(_SRC).as_posix()] += _scan(tree, classes)
    return +found


def test_the_interpolating_constructions_are_exactly_the_reviewed_ones() -> None:
    assert dict(_dynamic_sites()) == {k: v for k, v in _ALLOWED.items() if v}


def test_registration_refusals_interpolate_nothing() -> None:
    assert "registration_rules.py" not in _dynamic_sites()


class TestTheSweepSeesEveryShape:
    """The detector itself: each way of building a message is classified."""

    @staticmethod
    def _count(source: str) -> int:
        tree = ast.parse(source)
        return _scan(tree, {"RefusalError", "SomeRefusal"})

    def test_a_constant_passes(self) -> None:
        assert self._count("def f():\n    raise RefusalError('fixed')\n") == 0

    def test_a_positional_fstring_is_counted(self) -> None:
        assert self._count("def f(x):\n    raise RefusalError(f'bad {x}')\n") == 1

    def test_a_keyword_message_is_counted(self) -> None:
        assert self._count("def f(x):\n    raise RefusalError(msg=f'bad {x}')\n") == 1
        assert self._count("def f(x):\n    raise RefusalError(reason='a' + x)\n") == 1

    def test_an_assigned_message_is_traced(self) -> None:
        assert (
            self._count("def f(x):\n    m = f'{x}'\n    raise RefusalError(m)\n") == 1
        )
        assert self._count("def f():\n    m = 'a'\n    raise RefusalError(m)\n") == 0

    def test_percent_and_format_are_counted(self) -> None:
        assert self._count("def f(x):\n    raise RefusalError('a %s' % x)\n") == 1
        assert self._count("def f(x):\n    raise RefusalError('a {}'.format(x))\n") == 1

    def test_cls_and_self_class_inside_a_subclass_are_counted(self) -> None:
        src = (
            "class SomeRefusal(RefusalError):\n"
            "    def a(self, x):\n        return self.__class__(f'{x}')\n"
            "    @classmethod\n    def b(cls, x):\n        return cls(f'{x}')\n"
            "    def c(self, x):\n        return type(self)(f'{x}')\n"
        )
        assert self._count(src) == 3

    def test_cls_outside_a_refusal_class_is_ignored(self) -> None:
        src = (
            "class Other:\n    @classmethod\n    def b(cls, x):\n"
            "        return cls(f'{x}')\n"
        )
        assert self._count(src) == 0

    def test_a_helper_that_is_not_pinned_is_counted(self) -> None:
        assert self._count("def f(g):\n    raise RefusalError(g._other(1))\n") == 1
        assert (
            self._count("def f(g):\n    raise RefusalError(g._ratio_message(1, 2))\n")
            == 0
        )

    def test_a_conditional_of_constants_passes(self) -> None:
        assert (
            self._count("def f(x):\n    raise RefusalError('a' if x else 'b')\n") == 0
        )


class TestPinnedHelpers:
    def test_the_ratio_message_is_built_from_numbers_and_fixed_words(self) -> None:
        guard = BinaryContent()
        shown = guard._ratio_message(3, 10)
        assert re.fullmatch(
            r"binary content rejected: non-text ratio \d\.\d\d exceeds \d\.\d\d", shown
        )

    def test_the_page_subject_is_a_fixed_template_with_a_number(self) -> None:
        guard = BinaryContent()
        shown = guard._ratio_message(3, 10, "page 4 non-text")
        assert re.fullmatch(
            r"binary content rejected: page \d+ non-text ratio "
            r"\d\.\d\d exceeds \d\.\d\d",
            shown,
        )
