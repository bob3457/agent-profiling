#!/usr/bin/env bash
# setup_llamacpp_gh200.sh — build llama.cpp with CUDA on the GH200 and
# fetch candidate GGUF models. Run on the GH200 node (build needs the GPU
# toolchain; downloads need network — do those on the login node if compute
# nodes lack egress, the dirs are shared).
#
# NOTE model repo/file names drift on HuggingFace — if a download 404s,
# list the repo first:  huggingface-cli repo-files <repo>
set -euo pipefail
PREFIX=${PREFIX:-/projects/kzhou6/czhai/tools}
MODELS_DIR=${MODELS_DIR:-/scratch/czhai/models}
mkdir -p "$PREFIX" "$MODELS_DIR"

# ---- build (once) ----------------------------------------------------------
if [[ ! -x $PREFIX/llama.cpp/build/bin/llama-server ]]; then
  # module load cuda   # <- uncomment/adjust to your cluster's module name
  command -v nvcc >/dev/null || { echo "nvcc not found: load the CUDA module first"; exit 1; }
  cd "$PREFIX"
  [[ -d llama.cpp ]] || git clone --depth 1 https://github.com/ggml-org/llama.cpp
  cd llama.cpp
  cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=90 \
        -DCMAKE_BUILD_TYPE=Release
  cmake --build build --config Release -j "$(nproc)" --target llama-server llama-cli
fi
echo "llama-server: $PREFIX/llama.cpp/build/bin/llama-server"

# ---- models ----------------------------------------------------------------
pip show huggingface_hub >/dev/null 2>&1 || pip install -U "huggingface_hub[cli]" --user
# repo : file-glob  (Q5_K_M ~= best quality/size tradeoff for this task)
declare -A MODELS=(
  ["Qwen/Qwen2.5-Coder-7B-Instruct-GGUF"]="*q5_k_m*.gguf"
  ["Qwen/Qwen2.5-Coder-3B-Instruct-GGUF"]="*q5_k_m*.gguf"
  ["Qwen/Qwen2.5-Coder-1.5B-Instruct-GGUF"]="*q5_k_m*.gguf"
  ["bartowski/microsoft_Phi-4-mini-instruct-GGUF"]="*Q5_K_M*.gguf"
  ["Qwen/Qwen3-8B-GGUF"]="*Q5_K_M*.gguf"
)
for repo in "${!MODELS[@]}"; do
  echo ">>> $repo"
  huggingface-cli download "$repo" --include "${MODELS[$repo]}" \
      --local-dir "$MODELS_DIR/$(basename "$repo")" || \
    echo "    (failed — check the repo's actual filenames with: huggingface-cli repo-files $repo)"
done
ls -lh "$MODELS_DIR"/*/ 2>/dev/null | grep -i gguf || true

cat <<'USAGE'

---- serve one model (GH200, GPU otherwise idle during runs) ----
  $PREFIX/llama.cpp/build/bin/llama-server \
      -m /scratch/czhai/models/<dir>/<file>.gguf \
      -ngl 999 -c 8192 --port 8080 --host 127.0.0.1 &
  # NOTE: Qwen 7B split into multiple .gguf parts? pass the -00001-of- file;
  # llama.cpp picks up the rest automatically.

---- bake-off (repo root; one model at a time, results accumulate) ----
  python3 scripts/bakeoff_predictors.py --label qwen2.5-coder-7b
  # restart llama-server with the next model, change --label, repeat
  python3 scripts/bakeoff_predictors.py --compare

---- live (option B) after picking a winner ----
  export SPEC_LLM_MODE=openai
  export SPEC_LLM_ENDPOINT=http://127.0.0.1:8080/v1/chat/completions
  # worker runs inside apptainer: localhost is shared (host network), and
  # env passes through, so no bind changes needed. But run_option_b.sh
  # hardcodes SPEC_LLM_BIN inside the container — SPEC_LLM_MODE=openai
  # takes precedence in llm_predictor, so no script edit required.
USAGE
