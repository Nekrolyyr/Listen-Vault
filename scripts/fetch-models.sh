#!/usr/bin/env bash
# Downloads and prepares all models into ./models:
#   llm/Qwen3-8B-Q4_K_M.gguf     Qwen3-8B, the language model (about 5 GB; checked against Hugging Face's SHA-256)
#   embed/specter2_base|adapter  SPECTER2 with its proximity adapter: paper vectors (related papers, areas, topics)
#   embed/bge-small              bge-small-en-v1.5: passage vectors for the ask function
#   embed/kbir-inspec            keyword tagger (only used with KEYWORD_TAGGER=on)
#   kokoro/                      Kokoro-82M ONNX (fp32) + voices
#   piper/                       Piper fallback voice (en_US-lessac-medium)
#   llm/bloomz-7b1-Q4_K_M.gguf   BLOOMZ-7b1, the previous model, only with FETCH_BLOOMZ=1 (needs ~35 GB free to convert)
# Skips anything that already exists. Set MODELS_TARGET to another folder if the models live elsewhere.
set -euo pipefail
cd "$(dirname "$0")/.."

# MODELS_TARGET picks the folder (the production stack keeps it under LISTEN_CONFIG_DIR, see compose.yaml).
M="${MODELS_TARGET:-$PWD/models}"  # absolute, because docker run -v needs it
LLAMA_FULL=ghcr.io/ggml-org/llama.cpp:full-b11515
mkdir -p "$M/llm" "$M/kokoro" "$M/piper" "$M/embed/specter2_base" "$M/embed/specter2_adapter" \
         "$M/embed/bge-small" "$M/embed/kbir-inspec"

fetch() {  # fetch URL DEST
  [ -s "$2" ] && { echo "have $(basename "$2")"; return; }
  echo "downloading $(basename "$2")"
  curl -fL --retry 3 -o "$2.part" "$1" && mv "$2.part" "$2"
}

verify() {  # verify FILE SHA256: stop when the checksum differs
  local got
  got=$(sha256sum "$1" | cut -d' ' -f1)
  if [ "$got" != "$2" ]; then
    echo "checksum mismatch for $1: expected $2, got $got" >&2
    exit 1
  fi
  echo "verified $(basename "$1")"
}

H=https://huggingface.co

# The language model (Qwen3-8B, non-thinking mode is set in the prompts, see worker/llm.py)
QWEN_SHA256=d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785
fetch "$H/Qwen/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf" "$M/llm/Qwen3-8B-Q4_K_M.gguf"
verify "$M/llm/Qwen3-8B-Q4_K_M.gguf" "$QWEN_SHA256"

# SPECTER2 base and proximity adapter (paper vectors)
for f in config.json pytorch_model.bin special_tokens_map.json tokenizer.json tokenizer_config.json vocab.txt; do
  fetch "$H/allenai/specter2_base/resolve/main/$f" "$M/embed/specter2_base/$f"
done
for f in adapter_config.json pytorch_adapter.bin; do
  fetch "$H/allenai/specter2/resolve/main/$f" "$M/embed/specter2_adapter/$f"
done

# bge-small-en-v1.5 (passage vectors)
for f in config.json model.safetensors special_tokens_map.json tokenizer.json tokenizer_config.json vocab.txt; do
  fetch "$H/BAAI/bge-small-en-v1.5/resolve/main/$f" "$M/embed/bge-small/$f"
done

# KBIR keyword tagger (only read when KEYWORD_TAGGER=on)
for f in config.json pytorch_model.bin special_tokens_map.json tokenizer.json tokenizer_config.json vocab.json merges.txt; do
  fetch "$H/ml6team/keyphrase-extraction-kbir-inspec/resolve/main/$f" "$M/embed/kbir-inspec/$f"
done

K=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
fetch "$K/kokoro-v1.0.onnx" "$M/kokoro/kokoro-v1.0.onnx"
fetch "$K/voices-v1.0.bin" "$M/kokoro/voices-v1.0.bin"

P=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium
fetch "$P/en_US-lessac-medium.onnx" "$M/piper/en_US-lessac-medium.onnx"
fetch "$P/en_US-lessac-medium.onnx.json" "$M/piper/en_US-lessac-medium.onnx.json"

if [ "${FETCH_BLOOMZ:-0}" = 1 ]; then
  # the previous language model, for LLM_FORMAT=raw. Converted with llama.cpp, then quantized to Q4_K_M.
  GGUF="$M/llm/bloomz-7b1-Q4_K_M.gguf"
  if [ -s "$GGUF" ]; then
    echo "have $(basename "$GGUF")"
  else
    HF="$M/llm/hf-bloomz-7b1"
    mkdir -p "$HF"
    for f in config.json tokenizer.json tokenizer_config.json special_tokens_map.json model.safetensors; do
      fetch "$H/bigscience/bloomz-7b1/resolve/main/$f" "$HF/$f"
    done
    echo "converting to GGUF (f16), then quantizing to Q4_K_M"
    docker run --rm -v "$M/llm:/work" "$LLAMA_FULL" --convert --outtype f16 --outfile /work/bloomz-7b1-f16.gguf /work/hf-bloomz-7b1
    docker run --rm -v "$M/llm:/work" "$LLAMA_FULL" --quantize /work/bloomz-7b1-f16.gguf /work/bloomz-7b1-Q4_K_M.gguf Q4_K_M
    rm -f "$M/llm/bloomz-7b1-f16.gguf"
    echo "keeping $HF for re-conversion; delete it to free ~14 GB"
  fi
fi
echo "Models ready in $M"
