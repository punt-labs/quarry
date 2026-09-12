# Karpathy Prose

Technical writing in the style of Andrej Karpathy's blog posts,
documentation, and lecture explanations.

## Prose

- Lead with the simplest explanation that is still correct
- Build understanding progressively — start from zero, add complexity
- Explain ML concepts in software engineering terms: tensors are arrays,
  models are functions, inference is a function call
- Show the numbers — replace "faster" with "3,042 texts/s vs 9.4 texts/s"
- Short paragraphs, concrete examples, code over slides

## Code Comments

- Docstrings: imperative mood, one line if possible
  (`"""Select the fastest available execution provider."""`)
- Inline comments for non-obvious ML decisions:
  `# FP16 on CUDA: 2x throughput vs FP32, identical embedding quality`
- Comments explain why this precision/provider/batch size, not what the code does
- No commented-out code — version control remembers

## Error Messages

- Include what failed and what was expected:
  `msg = f"CUDAExecutionProvider unavailable, falling back to CPU"`
- Warnings for degraded performance, errors only for broken functionality
- Carry context: `raise RuntimeError(msg) from exc`

## Naming

- Snake_case for everything except classes (PascalCase)
- Short for locals: `sess`, `emb`, `vec`, `batch`, `tok`
- Descriptive for public API: `embed_texts`, `select_provider`
- Provider/model names: use the canonical names (`CUDAExecutionProvider`,
  not `cuda_ep`)
- Constants: `UPPER_SNAKE_CASE`

## Structure

- Module docstring: one sentence describing the module's purpose
- Imports: `from __future__ import annotations` first, then stdlib,
  third-party, local — each group separated by a blank line
- Lazy imports for heavy dependencies (`import onnxruntime as ort`
  inside functions, not at module level)
- Benchmark results in comments or docstrings when they justify a
  design choice
