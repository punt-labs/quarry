# Python Engineering

Deep expertise in Python systems programming, library design, and tooling.
Emphasis on readability, type safety, and leveraging the standard library.
Follows PEP 20: explicit is better than implicit, simple is better than
complex, readability counts.

## Idiomatic Principles

Write Python that experienced Python programmers expect to read. The
language has strong conventions codified in PEPs — follow them.

- PEP 8 is the baseline for formatting: 4-space indentation, snake_case
  for functions and variables, PascalCase for classes, UPPER_SNAKE for
  module-level constants.
- PEP 20 (The Zen of Python) is the design philosophy. When two approaches
  seem equivalent, the one that is more explicit, simpler, and flatter is
  correct.
- Use built-in types and operations before reaching for libraries. `dict`,
  `list`, `set`, `tuple`, comprehensions, and generators solve most data
  manipulation problems.
- Avoid cleverness. A list comprehension that fits on one line is clear; a
  nested comprehension with conditionals is not. Break it into a loop.
- Use `pathlib.Path` instead of `os.path` string manipulation. Paths are
  objects, not strings.
- Use f-strings for formatting. `format()` and `%` formatting are legacy.
- Prefer `dataclasses` or `NamedTuple` over plain dicts for structured
  data. Named fields catch typos at definition time, not at runtime.

## Type Hints

Python is dynamically typed, but modern Python uses type annotations to
catch errors before runtime. Type hints are documentation that a machine
can verify.

### PEP 484 and Beyond

Annotate all function signatures. Annotate module-level variables and
class attributes when the type is not obvious from the assignment.

```python
def resolve_identity(handle: str) -> Identity | None:
    """Look up an identity by handle. Returns None if not found."""
    ...
```

Use `|` union syntax (Python 3.10+) instead of `Union[X, Y]`. Use
`X | None` instead of `Optional[X]` — it is clearer about what is
happening.

### Generic Types

Use `collections.abc` for generic type annotations:

```python
from collections.abc import Sequence, Mapping, Callable, Iterator

def process_items(items: Sequence[str]) -> list[str]:
    ...

def apply(fn: Callable[[str], int], values: Sequence[str]) -> list[int]:
    ...
```

For custom generic classes, use `typing.Generic` and `TypeVar`:

```python
from typing import Generic, TypeVar

T = TypeVar("T")

class Stack(Generic[T]):
    def __init__(self) -> None:
        self._items: list[T] = []

    def push(self, item: T) -> None:
        self._items.append(item)

    def pop(self) -> T:
        return self._items.pop()
```

### Protocol (Structural Subtyping)

`Protocol` defines interfaces without inheritance. A class satisfies a
Protocol if it has the right methods — no base class needed.

```python
from typing import Protocol

class Readable(Protocol):
    def read(self, n: int = -1) -> bytes: ...

def process(source: Readable) -> bytes:
    return source.read()
```

Any object with a `read(n: int) -> bytes` method works. This is Go-style
interface satisfaction applied to Python.

### Type Checker Configuration

Run `mypy --strict` or `pyright` in strict mode. Enable these settings in
`pyproject.toml`:

```toml
[tool.mypy]
strict = true
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
```

Never use `# type: ignore` without a specific error code and a comment
explaining why: `# type: ignore[override]  # covariant return is safe here`.

## Testing

### pytest

pytest is the standard. Use it for all tests. Its fixture system,
parametrize decorator, and assertion introspection make tests shorter and
more informative than unittest.

```python
def test_resolve_valid_handle():
    identity = resolve("mal")
    assert identity.name == "Mal Reynolds"

def test_resolve_missing_handle():
    with pytest.raises(NotFoundError, match="nobody"):
        resolve("nobody")
```

### Fixtures

Fixtures provide test dependencies. Define them in `conftest.py` for
shared fixtures or in the test file for local ones.

```python
@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text("handle: mal\nname: Mal Reynolds\n")
    return config

def test_load_config(tmp_config: Path):
    cfg = load_config(tmp_config)
    assert cfg.handle == "mal"
```

Fixture scope controls lifetime: `function` (default, fresh per test),
`class`, `module`, `session`. Use the narrowest scope that works — shared
mutable state between tests causes ordering bugs.

`tmp_path` is a built-in fixture that provides a unique temporary
directory per test, cleaned up automatically.

### Parametrize

Table-driven tests in Python. One test function, many cases.

```python
@pytest.mark.parametrize(
    "handle, expected_name",
    [
        ("mal", "Mal Reynolds"),
        ("zoe", "Zoe Washburne"),
        ("wash", "Hoban Washburne"),
    ],
)
def test_resolve_names(handle: str, expected_name: str):
    identity = resolve(handle)
    assert identity.name == expected_name
```

For error cases, parametrize the expected exception:

```python
@pytest.mark.parametrize(
    "handle, exc_type",
    [
        ("", ValueError),
        ("nobody", NotFoundError),
    ],
)
def test_resolve_errors(handle: str, exc_type: type[Exception]):
    with pytest.raises(exc_type):
        resolve(handle)
```

### monkeypatch

Replace attributes, environment variables, or dict items for a single
test. Automatically restored after the test.

```python
def test_home_dir(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", "/fake/home")
    assert get_home() == Path("/fake/home")

def test_disabled_feature(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(config, "FEATURE_ENABLED", False)
    assert not is_feature_available()
```

Prefer `monkeypatch` over `unittest.mock.patch` — it is simpler, has
clearer scope, and does not require decorators or context managers.

### conftest.py

Shared fixtures, hooks, and plugins go in `conftest.py`. pytest discovers
these files automatically at each directory level.

- Root `conftest.py`: project-wide fixtures (database connections, app
  instances).
- Package `conftest.py`: fixtures specific to that package's tests.
- Never import from `conftest.py` directly — pytest handles discovery.

### Test Organization

- Mirror the source tree: `src/myapp/identity.py` is tested by
  `tests/test_identity.py` or `tests/identity/test_resolve.py`.
- Test files start with `test_`. Test functions start with `test_`.
- Test classes (when grouping related tests) start with `Test` and have no
  `__init__`.
- Keep tests focused: one behavior per test function. A test that checks
  five things is five tests pretending to be one.

## Error Handling

### EAFP: Easier to Ask Forgiveness than Permission

Python's idiom is to try the operation and handle the exception, not to
check preconditions first.

```python
# Good (EAFP):
try:
    value = mapping[key]
except KeyError:
    value = default

# Also good (when the language provides it):
value = mapping.get(key, default)

# Bad (LBYL — Look Before You Leap):
if key in mapping:
    value = mapping[key]
else:
    value = default
```

EAFP is preferred because it avoids race conditions (the state can change
between the check and the use) and because exceptions in Python are cheap
for the common case.

### Specific Exceptions

Catch the most specific exception possible. Never catch `Exception` or
`BaseException` without re-raising.

```python
# Bad:
try:
    data = load(path)
except Exception:
    data = default

# Good:
try:
    data = load(path)
except FileNotFoundError:
    data = default
```

Define custom exceptions for your domain. Inherit from a base exception
for the package so callers can catch broadly if they choose:

```python
class EthosError(Exception):
    """Base exception for all ethos errors."""

class IdentityNotFoundError(EthosError):
    """Raised when an identity handle does not resolve."""

class ValidationError(EthosError):
    """Raised when identity data fails validation."""
```

### Context Managers

Use context managers (`with` statements) for resource cleanup. Files,
database connections, locks, and temporary state changes should all use
context managers.

```python
with open(path) as f:
    data = f.read()
```

Write custom context managers with `contextlib.contextmanager` for
lightweight cases:

```python
from contextlib import contextmanager

@contextmanager
def temporary_env(key: str, value: str):
    old = os.environ.get(key)
    os.environ[key] = value
    try:
        yield
    finally:
        if old is None:
            del os.environ[key]
        else:
            os.environ[key] = old
```

For classes that manage resources, implement `__enter__` and `__exit__`.

## Package Structure

### src Layout

Use the src layout for installable packages. It prevents accidental imports
of the source directory during testing.

```text
myproject/
  pyproject.toml
  src/
    mypackage/
      __init__.py
      identity.py
      session.py
  tests/
    conftest.py
    test_identity.py
    test_session.py
```

### pyproject.toml

`pyproject.toml` is the single configuration file for builds, dependencies,
tools, and metadata. PEP 621 standardized project metadata here.

```toml
[project]
name = "ethos"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = [
    "pyyaml>=6.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "mypy>=1.8",
    "ruff>=0.3",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

No `setup.py`, no `setup.cfg`, no `requirements.txt` for project
dependencies. `pyproject.toml` is the source of truth.

### __init__.py

Keep `__init__.py` minimal. It defines the public API of the package.
Import the symbols you want to expose; do not import everything.

```python
from mypackage.identity import Identity, resolve
from mypackage.session import Session

__all__ = ["Identity", "resolve", "Session"]
```

`__all__` controls what `from mypackage import *` exports. Define it
explicitly even if you discourage star imports.

## Dependency Management

### uv

`uv` is the package manager. It replaces pip, pip-tools, and virtualenv
with a single fast tool.

```bash
uv sync              # Install dependencies from pyproject.toml + lockfile
uv add requests      # Add a dependency
uv remove requests   # Remove a dependency
uv lock              # Update the lockfile
uv run pytest        # Run a command in the virtualenv
```

- `uv.lock` is committed. It pins exact versions for reproducible builds.
- `uv sync` creates and manages the virtualenv automatically.
- `uv add --dev pytest` adds to the dev dependency group.

### Pinned Dependencies

Pin direct dependencies to compatible ranges (`>=1.0,<2.0` or `~=1.0`).
The lockfile (`uv.lock`) pins transitive dependencies to exact versions.

Review dependency updates before applying them. Run the full test suite
after updating. A dependency update that breaks tests is a regression, not
a minor version bump.

### Extras

Use optional dependency groups for different deployment contexts:

```toml
[project.optional-dependencies]
dev = ["pytest", "mypy", "ruff"]
docs = ["sphinx", "furo"]
```

Install with `uv sync --extra dev` or `uv sync --all-extras`.

## Performance

### Profiling First

Never optimize without profiling. Use `cProfile` for function-level
profiling and `line_profiler` for line-level.

```bash
python -m cProfile -s cumulative myprogram.py
```

For targeted profiling:

```python
import cProfile

with cProfile.Profile() as pr:
    expensive_function()
pr.print_stats(sort="cumulative")
```

### Generators

Use generators for lazy evaluation of large sequences. A generator yields
one item at a time, using constant memory regardless of sequence length.

```python
def read_lines(path: Path) -> Iterator[str]:
    with open(path) as f:
        for line in f:
            yield line.strip()
```

Use generator expressions instead of list comprehensions when you only
need to iterate once: `sum(x * x for x in values)` instead of
`sum([x * x for x in values])`.

### List Comprehensions

List comprehensions are faster than equivalent for-loops with append
because the iteration happens in C. Use them for simple transformations:

```python
names = [identity.name for identity in identities if identity.active]
```

Do not nest comprehensions beyond one level. A nested comprehension is
harder to read than an explicit loop.

### __slots__

For classes with many instances, `__slots__` reduces memory usage by
eliminating the per-instance `__dict__`.

```python
class Point:
    __slots__ = ("x", "y")

    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y
```

Use `__slots__` when you have thousands of instances of a class. Do not
use it as a default — it prevents dynamic attribute assignment and
complicates inheritance.

### String Operations

- Use `str.join()` for concatenating many strings: `"".join(parts)`.
- Use f-strings for formatting, not `+` concatenation.
- Use `io.StringIO` for building strings in a loop.
- `str` methods (`startswith`, `endswith`, `split`, `strip`) are
  implemented in C and fast.

## Common Anti-Patterns

### Mutable Default Arguments

Default argument values are evaluated once at function definition, not at
each call. Mutable defaults are shared across all calls.

```python
# Bug:
def append_to(item, target=[]):
    target.append(item)
    return target

# append_to(1) returns [1]
# append_to(2) returns [1, 2] — not [2]

# Fix:
def append_to(item, target: list | None = None):
    if target is None:
        target = []
    target.append(item)
    return target
```

This applies to lists, dicts, sets, and any mutable object.

### Bare except

`except:` catches everything, including `KeyboardInterrupt` and
`SystemExit`. This makes programs impossible to interrupt and hides real
errors.

```python
# Bad:
try:
    do_work()
except:
    pass

# Also bad:
try:
    do_work()
except Exception:
    pass  # silently swallows all errors

# Good:
try:
    do_work()
except SpecificError as e:
    logger.warning("work failed: %s", e)
    handle_failure()
```

### import *

`from module import *` pollutes the namespace with unknown names, makes it
impossible to trace where a name came from, and breaks static analysis.

```python
# Bad:
from os.path import *

# Good:
from pathlib import Path
```

Always import specific names or use the module as a namespace.

### God Classes

A class with 20 methods and 15 attributes is doing too much. Split it by
responsibility. If a class needs a comment like "handles identity
resolution, session management, and configuration loading," it is three
classes.

Signs of a god class:

- Methods that do not use most of the instance attributes.
- Groups of methods that only interact with each other, not the rest.
- The class file is longer than 300 lines.

### Overuse of Classes

Not everything needs to be a class. A module with functions and a
dataclass is often simpler than a class with methods. If a class has no
state (only methods), it should be a module with functions.

```python
# Unnecessary class:
class Formatter:
    @staticmethod
    def format_name(identity):
        return f"{identity.first} {identity.last}"

# Just a function:
def format_name(identity: Identity) -> str:
    return f"{identity.first} {identity.last}"
```

### Stringly Typed Code

Using strings where enums, constants, or types belong. Strings are not
checked at definition time — typos become runtime bugs.

```python
# Bad:
def set_status(status: str) -> None:
    if status not in ("active", "inactive", "pending"):
        raise ValueError(status)

# Good:
from enum import Enum

class Status(Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    PENDING = "pending"

def set_status(status: Status) -> None:
    ...
```

## Async Patterns

### asyncio

Use `asyncio` for I/O-bound concurrency: network requests, file I/O,
subprocess management. Do not use it for CPU-bound work — use
`multiprocessing` or `concurrent.futures.ProcessPoolExecutor` for that.

```python
import asyncio

async def fetch_identity(handle: str) -> Identity:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"/api/identity/{handle}") as resp:
            data = await resp.json()
            return Identity(**data)
```

### async/await

Every coroutine is defined with `async def` and called with `await`. A
coroutine that is called without `await` does nothing — this is a common
bug.

```python
# Bug: coroutine is created but never awaited
fetch_identity("mal")

# Correct:
identity = await fetch_identity("mal")
```

### gather

Run multiple coroutines concurrently with `asyncio.gather`:

```python
async def fetch_all(handles: list[str]) -> list[Identity]:
    tasks = [fetch_identity(h) for h in handles]
    return await asyncio.gather(*tasks)
```

Use `return_exceptions=True` if you want to collect exceptions instead of
failing on the first one:

```python
results = await asyncio.gather(*tasks, return_exceptions=True)
for result in results:
    if isinstance(result, Exception):
        logger.error("fetch failed: %s", result)
```

### TaskGroup (Python 3.11+)

`TaskGroup` is the structured concurrency primitive. It ensures all tasks
complete (or are cancelled) before the group exits.

```python
async def fetch_all(handles: list[str]) -> list[Identity]:
    results: list[Identity] = []
    async with asyncio.TaskGroup() as tg:
        for handle in handles:
            tg.create_task(fetch_and_append(handle, results))
    return results
```

If any task raises an exception, the remaining tasks are cancelled and the
exception propagates. This prevents orphaned tasks.

### Async Context Managers

Resources that need async cleanup implement `__aenter__` and `__aexit__`:

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def managed_connection(url: str):
    conn = await connect(url)
    try:
        yield conn
    finally:
        await conn.close()
```

### Common Async Mistakes

- Blocking the event loop with synchronous I/O. Use
  `asyncio.to_thread()` to run blocking code in a thread pool.
- Forgetting to await a coroutine. The coroutine object is created but
  never executed.
- Using `asyncio.sleep(0)` as a "yield" — it works but signals a design
  problem. If you need to yield, your coroutine is doing too much
  synchronous work.
- Mixing `async` and `sync` code without clear boundaries. Pick one model
  per layer and convert at the edges.

## Linting and Formatting

### ruff

`ruff` replaces flake8, isort, pyupgrade, and dozens of other linters with
a single fast tool. Configure in `pyproject.toml`:

```toml
[tool.ruff]
target-version = "py311"
line-length = 88

[tool.ruff.lint]
select = [
    "E",    # pycodestyle errors
    "W",    # pycodestyle warnings
    "F",    # pyflakes
    "I",    # isort
    "UP",   # pyupgrade
    "B",    # bugbear
    "SIM",  # simplify
    "RUF",  # ruff-specific
]
```

Run `ruff check .` for linting, `ruff check --fix .` for auto-fixes.

### ruff format

`ruff format` replaces black. It formats code to a consistent style with
no configuration needed beyond line length.

```bash
ruff format .           # Format all files
ruff format --check .   # Check without modifying (CI mode)
```

Format before committing. Formatting diffs are noise in code review.

### mypy Strict Mode

Run mypy in strict mode for maximum type safety:

```bash
mypy --strict src/
```

Strict mode enables all optional checks: no untyped defs, no untyped
calls, no implicit optional, warn on return any, warn on unused ignores.

Fix type errors rather than suppressing them. Every `# type: ignore` is
tech debt. When suppression is truly necessary (C extensions, dynamic
metaprogramming), use the specific error code and document why.

### Makefile Integration

A Python project Makefile should have at minimum:

- `lint`: run `ruff check` and `mypy --strict`.
- `format`: run `ruff format` and `ruff check --fix`.
- `test`: run `pytest` with coverage.
- `check`: run lint + test. This is the gate.

```makefile
.PHONY: check lint format test

check: lint test

lint:
    uv run ruff check src/ tests/
    uv run mypy --strict src/

format:
    uv run ruff format src/ tests/
    uv run ruff check --fix src/ tests/

test:
    uv run pytest -x --tb=short tests/
```
