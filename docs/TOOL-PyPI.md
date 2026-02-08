# PyPI Publishing Checklist for quarry-mcp

Build with `uv build`, publish with `twine` (reads `~/.pypirc` for credentials).

---

## 1. Bump the Version

Update the version in `pyproject.toml`:

- `version = "X.Y.Z"`

Runtime version is read via `importlib.metadata.version("quarry-mcp")` — no second file to update.

---

## 2. Build the Distribution

```sh
uv build
```

Creates `.whl` and `.tar.gz` files in the `dist/` directory.

---

## 3. Check the Distribution

```sh
uvx twine check dist/*
```

Validates the distribution metadata and README rendering.

---

## 4. Test Install Locally

```sh
uv pip install -e .
```

Installs in editable mode for local development. Uninstall with `uv pip uninstall quarry-mcp`.

---

## 5. Test the Build in a Clean Environment

```sh
uv venv /tmp/quarry-test
source /tmp/quarry-test/bin/activate
uv pip install dist/quarry_mcp-*.whl
quarry doctor
deactivate
```

---

## 6. Upload to TestPyPI

```sh
uvx twine upload --repository testpypi dist/*
```

Install from TestPyPI to verify:

```sh
uv pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ quarry-mcp
```

The `--extra-index-url` fallback is needed because dependencies (boto3, lancedb, etc.) are on PyPI, not TestPyPI.

---

## 7. Upload to PyPI

```sh
uvx twine upload dist/*
```

---

## 8. Verify on PyPI

- Check: https://pypi.org/project/quarry-mcp/
- Install in a fresh environment:

```sh
uv tool install quarry-mcp
quarry doctor
quarry install
```

---

## 9. Tag and Release

```sh
git tag vX.Y.Z
git push origin vX.Y.Z
```

---

## 10. Troubleshooting

- **Version conflict:** bump the version and rebuild — PyPI rejects re-uploads of the same version.
- **Missing dependencies:** add to `[project] dependencies` in `pyproject.toml`.
- **CLI not found:** verify `[project.scripts]` entry points.
- **TestPyPI dependency errors:** use `--extra-index-url https://pypi.org/simple/` to fall back to PyPI for dependencies.

---

## Credentials

Twine reads `~/.pypirc` automatically. Configure once:

```ini
[distutils]
index-servers =
    pypi
    testpypi

[pypi]
username = __token__
password = pypi-YOUR-API-TOKEN

[testpypi]
repository = https://test.pypi.org/legacy/
username = __token__
password = pypi-YOUR-TESTPYPI-TOKEN
```

Note: `uv publish` does not read `~/.pypirc` — that's why we use `twine` for uploads.
