# Kernel config reference

The kernel takes a *resolved* `KernelConfig` (apps own YAML/env loading — see
`config.py`). It does, however, read a handful of environment variables directly
at driver-construction time, mostly as fallbacks when the corresponding
`KernelConfig` field is unset. This is the single canonical list.

## Env vars the kernel reads

| Env var | Read by | Purpose / fallback semantics |
|---|---|---|
| `MELIOUS_BASE_URL` | `drivers/factory.py` | Melious base URL when `melious.base_url` is unset. |
| `MELIOUS_API_KEY` | `drivers/factory.py` | Melious key when `melious.api_key` is unset. |
| `LLMHUB_URL` | `drivers/adapters/huble.py` | HUBLE gateway URL fallback (`huble.base_url`). |
| `LLMHUB_API_KEY` | `drivers/adapters/huble.py` | HUBLE key fallback (`huble.api_key`). |
| `NVIDIA_API_KEY` | `drivers/adapters/vendor/nvidia.py` | NVIDIA NIM chat key. |
| `HF_TOKEN` | `drivers/adapters/hf.py` | HuggingFace Inference API token (stt driver) when no `api_key`/`api_key_env` is declared. |

The kernel reads no ambient credential for a first-party commercial provider — it
ships none. Out-of-tree drivers (seam #13) declare their own `api_key_env`.

Chat **activation** (which of Melious/HUBLE is live, and in what failover
order) is decided in one place — `agentix.config.enabled_providers` /
`select_enabled_provider`, which also feeds `derive_driver_specs`. Both the driver
factory and app config loaders consume it, so the "which backend is active"
predicate can't drift.

## The `drivers:` block — canonical driver declaration

`KernelConfig.drivers: tuple[DriverSpec, ...]`. The `DriverSpec` field table is canonical
in [`drivers.md`](drivers.md) §6 (`name`, `driver` = factory key or dotted path, `type`/
`modality`, `model`, `base_url`, `api_key_env` = the env-var **name** not the secret,
`default`, `scope`, `options`); to *call* a configured model see [`llm.md`](llm.md).

**Empty `drivers:` is valid and the default**: `derive_driver_specs` maps the legacy
`huble:` / `melious:` blocks onto specs, so existing operator YAML
keeps working unchanged. The `drivers:` block is the canonical form going forward;
collapsing the legacy provider blocks into it is the **v0.6 config migration**.

Example:

```yaml
drivers:
  - name: hf-stt
    driver: hf-stt
    modality: stt
    model: openai/whisper-large-v3
    api_key_env: HF_TOKEN
```

## `KernelConfig.llm_pricing`

Empty `llm_pricing` is valid: any model id missing from the table falls through to
`FALLBACK_PRICING['__unknown__']` in `CostTrackingMiddleware` (over-counts rather
than under-counts). Date-stamped model ids are prefix-matched. See the field
docstring in `config.py` and `core/middleware/cost_tracking.py`. Recorded spend is
chat-only in v0.5 ([`budgets.md`](budgets.md) §3); `DriverDescriptor.pricing_ref =
None` marks non-token-priced drivers.

Cluster-wide secret policy (fail-fast in stag/prod, secret vs publishable) lives in
[`ludo-agent/docs/cluster/env-and-secrets.md`](https://github.com/Ludo-Odoo-Migrations/ludo-agent/blob/main/docs/cluster/env-and-secrets.md);
this page is the kernel-specific list.

## `plugin_packages:` — app plugin registration

**agentixd only** (not read by the kernel itself). Declared in `~/.agentix/config.yaml` (or the
file named by `AGENTIXD_CONFIG`):

```yaml
plugin_packages:
  - ludo
```

Each entry is a Python package name. At daemon boot, `build_kernel()` does:

```python
importlib.import_module(f"{pkg}.plugin").register(state, tool_registry)
```

The package must be installed in the same venv as `agentixd`. Order matters: first plugin's
skill roots take priority in `SkillCatalog`.

**Plugin contract:** the `{pkg}.plugin` module must expose `register(state, tool_registry)`.
Optionally it may expose `skills_roots() -> list[str]`.

Full plugin authoring guide: [`plugins.md`](plugins.md).
