# Quickstart — your first agent in 10 lines

## Install

```sh
curl -LsSf https://raw.githubusercontent.com/Agentix-Kernel/agentix/main/scripts/install.sh | AGENTIX_EXTRAS=anthropic,daemon,sdk bash
source ~/.agentix/env.sh
```

All install variants (the extras matrix, custom `AGENTIX_HOME`, CLI tools,
developer install) live in the [README § Install](../README.md#install) —
single source, not repeated here.

The `daemon` extra adds FastAPI + uvicorn + pyyaml (runs `agentixd`).
The `sdk` extra adds httpx (runs `AgentixClient`).

## Configure the daemon

Create `~/.agentix/config.yaml`:

```yaml
sqlite_path: ~/.agentix/kernel.db
memory_path: ~/.agentix/memory

budget_usd: 200.0

drivers:
  - name: llm
    driver: anthropic
    modality: chat
    type: model
    default: true

# Optional: override socket path (default: ~/.agentix/agentixd.sock)
# daemon:
#   socket_path: ~/.agentix/agentixd.sock

# Optional: MinIO for blob checkpoints (omit to use local-fs fallback)
# minio:
#   endpoint: 10.0.99.1:9000
#   access_key: minioadmin
#   secret_key: minioadmin
#   bucket: agentix

# Optional: app plugin packages registered at boot
# plugin_packages:
#   - myapp
```

Set `ANTHROPIC_API_KEY` in your environment before starting the daemon.

Config key reference: [`docs/kernel-config-reference.md`](kernel-config-reference.md).

## Start the daemon

```sh
agentixd
```

The daemon binds to a Unix Domain Socket (`~/.agentix/agentixd.sock`) — no TCP
port. Override the socket path with `AGENTIXD_SOCKET` env or `daemon.socket_path`
in config.yaml.

The config path is resolved from (first wins):
1. `AGENTIXD_CONFIG` env
2. `AGENTIX_CONFIG` env
3. `~/.agentix/config.yaml`

On `SIGHUP` the daemon logs a reload notice (full live reload is not yet implemented).

## Run your first turn

```python
import asyncio
from agentix_sdk.client import AgentixClient

async def main():
    async with AgentixClient() as client:
        session = await client.create_session(customer_id="acme")
        turn = await client.run_turn(session.id, message="What is the capital of France?")
        print(turn)

asyncio.run(main())
```

`AgentixClient` auto-detects `~/.agentix/agentixd.sock`. Override with:
- `AGENTIXD_SOCKET` env
- `base_url="unix:///path/to/agentixd.sock"` constructor arg

## Add tools

Tools let the agent take actions — read files, call APIs, query databases.
Register them via a plugin package declared in `plugin_packages:` in config.yaml.
See [`docs/tools.md`](tools.md) for the `@tool` decorator and the registration pattern,
and [`docs/plugins.md`](plugins.md) for the plugin boot contract.

## SDK reference

`AgentixClient` methods (all `async`):

| Method | What it does |
|---|---|
| `create_session(customer_id, budget_usd?, app_meta?, control_plane_id?, parent_session_id?)` | Open a new session |
| `run_turn(session_id, message?)` | Run one turn; returns a `Turn` |
| `get_session(session_id)` | Fetch session state |
| `list_sessions(customer_id?, status?, limit?)` | List sessions from SQLite |
| `list_turns(session_id)` | All turns for a session |
| `list_drivers()` | Installed drivers |
| `install_driver(key, ...)` | pip-install + register a driver |
| `uninstall_driver(name)` | Remove a driver spec |
| `list_agents()` / `register_agent(card)` / `unregister_agent(name)` | A2A agent cards |
| `list_skill_roots()` / `list_skills()` / `get_skill(name)` / `reload_skills()` | Skill catalog |
| `scaffold_driver(name, modality?, description?)` | Generate a driver skeleton |
| `scaffold_agent(name, description?)` | Generate agent card files |
| `is_ready()` | Health probe — returns `bool` |

Errors surface as `AgentixError(status_code, detail)`.

## Direct kernel wiring (advanced)

If you embed the kernel in-process rather than talking to a running daemon, wire
the components directly — see the full example in [README § Quickstart](../README.md#quickstart).
This path requires the `daemon` extra for `KernelConfig` + stores, and you own the
event loop.

## Next steps

- [`docs/plugins.md`](plugins.md) — the plugin boot contract; how to inject tools, skills and middleware
- [`docs/seams.md`](seams.md) — the 13 extension points for plugging in app domain logic
- [`docs/drivers.md`](drivers.md) — choosing and configuring drivers
- [`docs/vendor-licenses.md`](vendor-licenses.md) — vendor extras and their ToS
- [`docs/kernel-config-reference.md`](kernel-config-reference.md) — all env vars and config keys
