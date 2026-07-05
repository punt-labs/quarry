# Embedding quantization

Quarry embeds text with a local ONNX export of the snowflake-arctic-embed-m
model. The default build ships the int8-quantized weights, which cut the model
from roughly 220 MB to about 120 MB and speed up CPU inference, at a small and
measured cost in embedding quality.

## Int8 on CPU, FP16 on GPU

On a CPU-only machine the int8 model is the right default: it is smaller,
faster, and the quality delta against the float model is within the noise of
the retrieval metrics. When a CUDA GPU is present the harness selects the FP16
export instead, because the GPU runs half-precision at full throughput and the
extra precision is free.

## Measuring the quality cost

Quantization is free performance until it is not. The only way to know whether
int8 has moved retrieval quality is to measure it: embed the same corpus with
both exports, run the same query set, and compare success and MRR per bucket.
Intuition about quantization error is unreliable; the numbers are not.
