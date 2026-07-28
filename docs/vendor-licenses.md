# Vendor Driver Licenses

Agentix ships a two-tier driver model:

- **Intrinsic drivers** — open-source infrastructure (SQLite, PostgreSQL, MinIO, HuggingFace,
  local filesystem). Their SDKs carry permissive licenses (MIT, Apache 2.0) and impose no
  commercial API ToS on the consumer.

- **OpenAI-compatible drivers** — commercial AI/LLM endpoints reached over the
  `/v1/chat/completions` wire. The `openai` SDK ships only as the HTTP client for that
  wire; the **underlying API services carry their own Terms of Service** which the
  consumer must independently review and accept before use.

The kernel ships **no first-party commercial provider driver** (Anthropic, OpenAI,
Groq and Grok were removed in 0.6). Supply those out-of-tree via seam #13 — you take on
their SDK dependency and their ToS directly, outside this table.

Agentix makes no representations about third-party ToS terms. Always verify current terms
directly with the provider.

## The `openai-compat` extra and its ToS

| Extra | Install | SDK | SDK license |
|-------|---------|-----|-------------|
| `[openai-compat]` | `pip install agentix[openai-compat]` | `openai` | MIT |

One extra, four adapters — each endpoint carries its own ToS:

| Adapter | Endpoint ToS |
|---------|--------------|
| **Melious** | per your Melious agreement |
| **Gemini** | https://ai.google.dev/gemini-api/terms |
| **Ollama** | https://ollama.com/legal/terms |
| **NVIDIA NIM** | https://www.nvidia.com/en-us/data-center/products/ai-enterprise/eula/ |

## Intrinsic extras

| Extra | SDK | License |
|-------|-----|---------|
| `[minio]` | `minio` | Apache 2.0 |
| `[hf]` | `huggingface_hub` | Apache 2.0 |
| `[postgresql]` | `asyncpg` | MIT |
