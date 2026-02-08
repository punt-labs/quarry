# Non-Functional Design: Logging & Exception Handling

## Logging

### Standard

All logging follows PEP 282 and the Python `logging` module best practices.

### Logger declaration

Every module that logs declares a module-level logger:

```python
logger = logging.getLogger(__name__)
```

Modules that perform no I/O, contain only data definitions, or are pure functions
with no side effects need not declare a logger. Examples: `models.py`, `types.py`,
`config.py`, `chunker.py`.

### Configuration

Logging is configured **once**, at the application entry point only:

- **CLI** (`__main__.py`): `logging.basicConfig(level=logging.INFO)` at the top of
  the CLI entry function.
- **MCP server** (`mcp_server.py`): `logging.basicConfig(level=logging.INFO)` at
  module level (the module is only imported when the server starts).

Library modules never call `logging.basicConfig`. They emit via `getLogger(__name__)`
and let the application configure the handler.

### Log levels

| Level | Use |
|-------|-----|
| `DEBUG` | Internal state useful during development: variable values, branch decisions, intermediate computation results. |
| `INFO` | Operational milestones a user or operator would want to see: starting a job, completing a step, resource counts. |
| `WARNING` | Unexpected but recoverable conditions: empty input producing zero output, deprecated parameter usage. |
| `ERROR` | Failures that prevent an operation from completing but do not crash the process. Always accompanied by exception context. |

`CRITICAL` is not used. If the process must exit, it raises an exception.

### What to log

**INFO-level events (the operational narrative):**

- External service calls: S3 upload/download, Textract start/complete, model loading.
- Database mutations: inserts, deletes, with row counts.
- Pipeline stage transitions: "Analyzing", "Chunking", "Embedding", "Storing".
- Resource identifiers: document names, job IDs, S3 keys.

**DEBUG-level events (development diagnostics):**

- Page classification decisions (text vs image, threshold values).
- Section splitting results (section count, format detected).
- Chunk boundaries (chunk count, sizes).
- Query parameters and result counts in search.

**WARNING-level events:**

- A file produces zero extractable content.
- An ingestion overwrites an existing document (user-requested but worth noting).

**ERROR-level events:**

- External service failures (Textract, S3) — logged with `logger.exception()`.
- File I/O failures — logged with `logger.exception()`.

### What not to log

- Passwords, API keys, access tokens.
- Full document text (log document name and character count instead).
- Per-iteration progress in tight loops (log summary counts).

### Format

Log messages use `%s`-style formatting (lazy evaluation), not f-strings:

```python
logger.info("Inserted %d chunks for %s", count, name)     # correct
logger.info(f"Inserted {count} chunks for {name}")         # incorrect
```

### Structured context

Include enough context to trace an operation without reading code:

```python
logger.info("Textract job started: %s (%d pages)", job_id, total_pages)
```

Not:

```python
logger.info("Job started")
```

---

## Exceptions

### Standard

Exception handling follows PEP 8 and the principle that **exceptions represent
contracts**: a function either succeeds and returns its documented type, or raises a
documented exception. There is no middle ground.

### Exception hierarchy

The codebase uses **built-in exception types** and does not define custom exceptions
unless a caller needs to distinguish between failure modes programmatically. The
current types are sufficient:

| Exception | Raised when |
|-----------|-------------|
| `ValueError` | Invalid input: unsupported format, bad parameter value. |
| `FileNotFoundError` | A file path argument does not exist. |
| `RuntimeError` | An external service reports failure (e.g., Textract job failed). |
| `TimeoutError` | An external service exceeds its time budget. |

If future code needs a domain-specific exception (e.g., to let callers distinguish
"document already exists" from "database unreachable"), define it in `models.py` as a
subclass of `Exception` with a clear docstring.

### Rules

1. **Never swallow exceptions.** Every `except` block must either:
   - Re-raise the exception (bare `raise`), or
   - Log it with `logger.exception()` and raise a different exception, or
   - Log it with `logger.exception()` and return a documented sentinel (only at
     system boundaries like MCP tool handlers).

2. **Never use bare `except:`.** Always catch a specific type.

3. **Never catch `Exception` broadly** in library code. Broad catches are permitted
   only at the outermost boundary (MCP tool handler, CLI command) where they
   produce a user-facing error message. Even then, log with `logger.exception()`.

4. **Exceptions carry context.** Every `raise` includes a message with the values
   that caused the failure:

   ```python
   msg = f"Unsupported file format: {suffix}"
   raise ValueError(msg)
   ```

   Not:

   ```python
   raise ValueError("bad format")
   ```

5. **Document raised exceptions** in docstrings using the `Raises:` section. Every
   public function that raises documents what and when.

6. **Use `try/finally` for cleanup**, not `try/except`. The `ocr_client.py` S3
   cleanup pattern is correct: the `finally` block ensures S3 objects are deleted
   regardless of Textract outcome, and the exception propagates naturally.

7. **Boundary handlers (MCP tools, CLI commands)** may catch exceptions to produce
   user-friendly messages. They must still log the full traceback:

   ```python
   try:
       result = ingest_document(path, db, settings)
   except FileNotFoundError:
       logger.exception("Ingestion failed for %s", path)
       return f"Error: file not found: {path}"
   ```

8. **Do not use exceptions for flow control.** Check preconditions explicitly:

   ```python
   if TABLE_NAME not in db.list_tables().tables:
       return []
   ```

   Not:

   ```python
   try:
       table = db.open_table(TABLE_NAME)
   except TableNotFoundError:
       return []
   ```
