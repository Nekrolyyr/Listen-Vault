#!/usr/bin/env bash
# Downloads and prepares all models into ./models:
#   llm/bloomz-7b1-Q4_K_M.gguf   BLOOMZ-7b1, converted and quantized with llama.cpp
#   kokoro/                       Kokoro-82M ONNX (fp32) + voices
#   piper/                        Piper fallback voice (en_US-lessac-medium)
# Skips anything that already exists. Needs ~35 GB free temporarily for the BLOOMZ conversion.
set -euo pipefail
cd "$(dirname "$0")/.."

M="$PWD/models"  # absolute, because docker run -v needs it
LLAMA_FULL=ghcr.io/ggml-org/llama.cpp:full-b11515
mkdir -p "$M/llm" "$M/kokoro" "$M/piper"

fetch() {  # fetch URL DEST
  [ -s "$2" ] && { echo "have $(basename "$2")"; return; }
  echo "downloading $(basename "$2")"
  curl -fL --retry 3 -o "$2.part" "$1" && mv "$2.part" "$2"
}

K=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
fetch "$K/kokoro-v1.0.onnx" "$M/kokoro/kokoro-v1.0.onnx"
fetch "$K/voices-v1.0.bin" "$M/kokoro/voices-v1.0.bin"

P=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium
fetch "$P/en_US-lessac-medium.onnx" "$M/piper/en_US-lessac-medium.onnx"
fetch "$P/en_US-lessac-medium.onnx.json" "$M/piper/en_US-lessac-medium.onnx.json"

GGUF="$M/llm/bloomz-7b1-Q4_K_M.gguf"
if [ -s "$GGUF" ]; then
  echo "have $(basename "$GGUF")"
else
  HF="$M/llm/hf-bloomz-7b1"
  mkdir -p "$HF"
  for f in config.json tokenizer.json tokenizer_config.json special_tokens_map.json model.safetensors; do
    fetch "https://huggingface.co/bigscience/bloomz-7b1/resolve/main/$f" "$HF/$f"
  done
  echo "converting to GGUF (f16), then quantizing to Q4_K_M"
  docker run --rm -v "$M/llm:/work" "$LLAMA_FULL" --convert --outtype f16 --outfile /work/bloomz-7b1-f16.gguf /work/hf-bloomz-7b1
  docker run --rm -v "$M/llm:/work" "$LLAMA_FULL" --quantize /work/bloomz-7b1-f16.gguf /work/bloomz-7b1-Q4_K_M.gguf Q4_K_M
  rm -f "$M/llm/bloomz-7b1-f16.gguf"
  echo "keeping $HF for re-conversion; delete it to free ~14 GB"
fi
echo "Models ready in $M"
