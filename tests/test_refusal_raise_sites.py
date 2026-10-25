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
# media-type label that its owner sanitises, a status message our fetch layer wrote,
# an UrlAdmission reason (a rule or a host name), a rule text from the SSRF policy, a
# status code, or a FailureReason.
# Never a caller-supplied name, URL, path or credential.
_ALLOWED: dict[str, int] = {
    "content_guard.py": 2,
    "db/row_budget.py": 1,
    "ingestion/opened_response.py": 2,
    "ingestion/page_report.py": 1,
    "ingestion/page_splitter.py": 1,
    "ingestion/media_type_policy.py": 1,
    "ingestion/fetched_body.py": 3,
    "ingestion/fetch_refused.py": 5,
    "ingestion/sitemap_reader.py": 1,
    "daemon/refusal_aware_terminal.py": 1,
    # A job's state.error (a FailureReason), and the daemon's own fixed refusals
    # re-raised across the thread boundary as backfill's.
    "daemon/queued_job.py": 3,
    "transcript_loader.py": 1,  # the read limit, a size
    "backfill_outcomes.py": 1,  # the capture failure's cause: an errno name or class
    "daemon/late_outcome.py": 1,  # the stop reason, a fixed phrase of the run
    "daemon/ingest_refusal.py": 1,  # the factory: its callers pass fixed reasons
    "daemon/thread_call.py": 1,  # the caller's stop reason: a fixed phrase of the run
    "extractors/sheet_tables.py": 0,
}


def _refusal_classes(trees: dict[Path, ast.Module]) -> set[str]:
    """Return every ``RefusalError`` subclass name, and every module-level alias."""
    names = {"RefusalError"}
    grew = True
    while grew:
        grew = False
        for tree in trees.values():
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ClassDef)
                    and node.name not in names
                    and any(_base_name(b) in names for b in node.bases)
                ):
                    names.add(node.name)
                    grew = True
            for alias in _aliases(tree):
                if alias[1] in names and alias[0] not in names:
                    names.add(alias[0])
                    grew = True
    return names


def _base_name(expr: ast.expr) -> str | None:
    """Return the last name of ``Name`` or ``module.Name``, else ``None``."""
    return getattr(expr, "id", getattr(expr, "attr", None))


def _aliases(tree: ast.Module) -> list[tuple[str, str]]:
    """Return ``(alias, original)`` for every way a class gets a second name.

    ``E = Sub``, ``E: type[Sub] = Sub`` and ``from m import Sub as E``, at module
    level or inside a function.
    """
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            found.extend(
                (a.asname, a.name.rsplit(".", 1)[-1]) for a in node.names if a.asname
            )
        target = original = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, original = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, original = node.target, node.value
        if isinstance(target, ast.Name) and original is not None:
            source = _base_name(original)
            if source is not None:
                found.append((target.id, source))
    return found


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


def _is_super_or_cls(receiver: ast.expr) -> bool:
    is_super = (
        isinstance(receiver, ast.Call)
        and isinstance(receiver.func, ast.Name)
        and receiver.func.id == "super"
    )
    return is_super or (isinstance(receiver, ast.Name) and receiver.id == "cls")


def _forwarded_message(call: ast.Call) -> ast.expr | None:
    """Return the message handed to ``super().__init__`` / ``cls.__new__``, if any."""
    args = [a for a in call.args if not (isinstance(a, ast.Name) and a.id == "cls")]
    fn = call.func
    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
        # ``Sub.__new__(Sub, msg)`` / ``Sub.__init__(e, msg)``: the first argument is
        # the class or the instance, not the message.
        args = call.args[1:]
    if args:
        return args[0]
    return next((k.value for k in call.keywords if k.arg in _MESSAGE_KEYWORDS), None)


def _splats(call: ast.Call) -> bool:
    """Return whether *call* forwards ``*args`` / ``**kwargs`` the sweep can't read."""
    return any(isinstance(a, ast.Starred) for a in call.args) or any(
        k.arg is None for k in call.keywords
    )


def _builds_text(expr: ast.expr) -> bool:
    """Return whether *expr* assembles a string: an f-string, ``%``, ``+``, format."""
    if isinstance(expr, ast.JoinedStr | ast.BinOp):
        return not _is_constant(expr)
    return (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Attribute)
        and expr.func.attr == "format"
    )


def _to_check(call: ast.Call, classes: set[str], *, inside: bool) -> list[ast.expr]:
    """Return the expressions a refusal message may come from, for *call*.

    Empty when *call* is not a construction.  A splat or a callee the sweep cannot
    name (a call result, a subscript) that is handed built text is returned as the
    call itself, which is never constant, so it is counted.
    """
    fn = call.func
    on_class = (
        isinstance(fn, ast.Attribute)
        and fn.attr in {"__init__", "__new__"}
        and _base_name(fn.value) in classes
    )
    forwards = on_class or (
        inside
        and isinstance(fn, ast.Attribute)
        and fn.attr in {"__init__", "__new__"}
        and _is_super_or_cls(fn.value)
    )
    builds = _constructs_refusal(call, classes, inside=inside)
    if (builds or forwards) and _splats(call):
        return [call]
    message = (
        _forwarded_message(call) if forwards else _message(call) if builds else None
    )
    if message is not None:
        return [message]
    unnamed = not isinstance(fn, ast.Name | ast.Attribute)
    if unnamed and any(_builds_text(a) for a in call.args):
        return [call]
    return []


def _is_args(target: ast.expr) -> bool:
    return isinstance(target, ast.Attribute) and target.attr == "args"


def _rewrites_args(node: ast.AST) -> bool:
    """Return whether *node* rewrites an exception's ``.args`` after construction.

    ``e.args = ...``, ``e.args += ...`` and ``setattr(e, "args", ...)``.
    """
    if isinstance(node, ast.Assign):
        return any(_is_args(t) for t in node.targets)
    if isinstance(node, ast.AugAssign):
        return _is_args(node.target)
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "setattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "args"
    )


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

    def assignments(scope: ast.AST) -> list[tuple[int, str, ast.expr]]:
        return [
            (0 if node in tree.body else node.lineno, node.targets[0].id, node.value)
            for node in [*ast.walk(scope), *tree.body]
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ]

    count = sum(_rewrites_args(n) for n in ast.walk(tree))
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        owner = nearest(call, (ast.ClassDef,))
        inside = isinstance(owner, ast.ClassDef) and owner.name in classes
        func = nearest(call, (ast.FunctionDef, ast.AsyncFunctionDef))
        for message in _to_check(call, classes, inside=inside):
            for value in _resolved(message, assignments(func or tree), call.lineno):
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
        classes = _refusal_classes({Path("snippet.py"): tree}) | {"SomeRefusal"}
        return _scan(tree, classes)

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


class TestTheSweepSeesTheHardShapes:
    """Splats, aliases, unnamed callees, forwarding and .args are all seen."""

    @staticmethod
    def _count(source: str) -> int:
        return TestTheSweepSeesEveryShape._count(source)

    def test_a_double_star_message_is_counted(self) -> None:
        assert self._count("def f(kw):\n    raise RefusalError(**kw)\n") == 1

    def test_a_star_args_message_is_counted(self) -> None:
        assert self._count("def f(a):\n    raise RefusalError(*a)\n") == 1

    def test_a_module_level_alias_is_followed(self) -> None:
        src = (
            "class Sub(RefusalError):\n    pass\n"
            "E = Sub\n"
            "def f(x):\n    raise E(f'bad {x}')\n"
        )
        assert self._count(src) == 1
        assert self._count(src.replace("f'bad {x}'", "'fixed'")) == 0

    def test_an_attribute_alias_is_followed(self) -> None:
        src = (
            "class Sub(RefusalError):\n    pass\n"
            "E = mod.Sub\n"
            "def f(x):\n    raise E(f'bad {x}')\n"
        )
        assert self._count(src) == 1
        assert self._count(src.replace("mod.Sub", "Sub")) == 1

    def test_a_callee_that_cannot_be_named_is_suspect_with_built_text(self) -> None:
        assert self._count("def f(g, x):\n    g()(f'{x}')\n") == 1
        assert self._count("def f(g, x):\n    g[0]('a' + x)\n") == 1
        assert self._count("def f(g, x):\n    g()('fixed')\n") == 0
        assert self._count("def f(g, x):\n    g()(x)\n") == 0

    def test_an_ordinary_method_call_is_not_suspect(self) -> None:
        assert self._count("def f(g, x):\n    g.run(f'{x}')\n") == 0

    def test_super_init_and_new_forwarding_a_built_message_are_counted(self) -> None:
        src = (
            "class SomeRefusal(RefusalError):\n"
            "    def __init__(self, x):\n        super().__init__(f'{x}')\n"
        )
        assert self._count(src) == 1
        assert self._count(src.replace("f'{x}'", "'fixed'")) == 0
        new = (
            "class SomeRefusal(RefusalError):\n"
            "    def __new__(cls, x):\n        return cls.__new__(cls, f'{x}')\n"
        )
        assert self._count(new) == 1

    def test_a_forward_with_a_keyword_message_is_counted(self) -> None:
        src = (
            "class SomeRefusal(RefusalError):\n"
            "    def __init__(self, x):\n        super().__init__(msg=f'{x}')\n"
        )
        assert self._count(src) == 1

    def test_rewriting_args_is_counted(self) -> None:
        assert self._count("def f(e, x):\n    e.args = (x,)\n") == 1
        assert self._count("def f(e):\n    e.other = 1\n") == 0


class TestTheSweepSeesTheRemainingShapes:
    """Import aliases, attribute bases, annotated and local aliases, .args writes."""

    @staticmethod
    def _count(source: str) -> int:
        return TestTheSweepSeesEveryShape._count(source)

    def test_an_import_alias_is_followed(self) -> None:
        src = (
            "from quarry.refusal import RefusalError as Refused\n"
            "def f(x):\n    raise Refused(f'bad {x}')\n"
        )
        assert self._count(src) == 1
        assert self._count(src.replace("f'bad {x}'", "'fixed'")) == 0

    def test_an_aliased_subclass_import_is_followed(self) -> None:
        src = (
            "class Sub(RefusalError):\n    pass\n"
            "from elsewhere import Sub as Renamed\n"
            "def f(x):\n    raise Renamed(f'bad {x}')\n"
        )
        assert self._count(src) == 1

    def test_a_base_named_through_a_module_is_followed(self) -> None:
        src = (
            "class Sub(refusal.RefusalError):\n    pass\n"
            "def f(x):\n    raise Sub(f'bad {x}')\n"
        )
        assert self._count(src) == 1
        assert self._count(src.replace("f'bad {x}'", "'fixed'")) == 0

    def test_an_annotated_alias_is_followed(self) -> None:
        src = (
            "class Sub(RefusalError):\n    pass\n"
            "E: type[Sub] = Sub\n"
            "def f(x):\n    raise E(f'bad {x}')\n"
        )
        assert self._count(src) == 1

    def test_an_alias_made_inside_a_function_is_followed(self) -> None:
        src = (
            "class Sub(RefusalError):\n    pass\n"
            "def f(x):\n    local = Sub\n    raise local(f'bad {x}')\n"
        )
        assert self._count(src) == 1
        assert self._count(src.replace("f'bad {x}'", "'fixed'")) == 0

    def test_new_called_on_the_class_outside_it_is_counted(self) -> None:
        src = (
            "class Sub(RefusalError):\n    pass\n"
            "def f(x):\n    return Sub.__new__(Sub, f'bad {x}')\n"
        )
        assert self._count(src) == 1
        assert self._count(src.replace("f'bad {x}'", "'fixed'")) == 0

    def test_init_called_on_the_class_outside_it_is_counted(self) -> None:
        src = (
            "class Sub(RefusalError):\n    pass\n"
            "def f(e, x):\n    Sub.__init__(e, f'bad {x}')\n"
        )
        assert self._count(src) == 1

    def test_extending_args_in_place_is_counted(self) -> None:
        assert self._count("def f(e, x):\n    e.args += (x,)\n") == 1

    def test_setattr_of_args_is_counted(self) -> None:
        assert self._count("def f(e, x):\n    setattr(e, 'args', (x,))\n") == 1
        assert self._count("def f(e, x):\n    setattr(e, 'note', x)\n") == 0


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
