"""Every RefusalError raise site in src/ builds its message from fixed text.

A refusal's message is returned to a client and logged without a traceback, so a
raise site must not put caller-supplied text in it (a collection name, a URL, a
path).  A message that interpolates anything is listed here with the reason its
values are safe; a new interpolating site fails until someone makes that case.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "quarry"

# file (relative to src/quarry) -> how many raise sites interpolate, and why each is
# safe.  Only counts and sizes, a column name, a page number, a sniffed-format or
# media-type label that its owner sanitises, an UrlAdmission reason (a rule or a host
# name), or a FailureReason.  Never a caller-supplied name, URL, path or credential.
_ALLOWED: dict[str, int] = {
    "content_guard.py": 5,
    "db/row_budget.py": 1,
    "ingestion/opened_response.py": 2,
    "ingestion/page_report.py": 1,
    "ingestion/page_splitter.py": 1,
    "ingestion/media_type_policy.py": 1,
    "ingestion/fetched_body.py": 3,
    "daemon/refusal_aware_terminal.py": 1,
    "extractors/sheet_tables.py": 1,
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


def _is_dynamic(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Constant):
        return False
    if isinstance(expr, ast.JoinedStr):
        return any(isinstance(v, ast.FormattedValue) for v in expr.values)
    if isinstance(expr, ast.BinOp):
        return (
            isinstance(expr.op, ast.Mod)
            or _is_dynamic(expr.left)
            or _is_dynamic(expr.right)
        )
    return True


def _dynamic_sites() -> Counter[str]:
    trees = {p: ast.parse(p.read_text()) for p in _SRC.rglob("*.py")}
    classes = _refusal_classes(trees)
    found: Counter[str] = Counter()
    for path, tree in trees.items():
        rel = path.relative_to(_SRC).as_posix()
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            assigns = [
                (n.lineno, n.targets[0].id, n.value)
                for n in ast.walk(func)
                if isinstance(n, ast.Assign)
                and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)
            ]
            for call in ast.walk(func):
                if not (isinstance(call, ast.Call) and call.args):
                    continue
                fn = call.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if name not in classes:
                    continue
                arg = call.args[0]
                values = (
                    [v for ln, nm, v in assigns if nm == arg.id and ln < call.lineno]
                    if isinstance(arg, ast.Name)
                    else [arg]
                )
                found[rel] += sum(1 for v in values if _is_dynamic(v))
    return +found


def test_the_interpolating_raise_sites_are_exactly_the_reviewed_ones() -> None:
    assert dict(_dynamic_sites()) == _ALLOWED


def test_registration_refusals_interpolate_nothing() -> None:
    assert "registration_rules.py" not in _dynamic_sites()
