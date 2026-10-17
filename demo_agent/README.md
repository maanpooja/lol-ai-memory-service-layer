# Memory agent demo (`adk web`)

An ADK agent whose **only** long-term memory is the deployed memory-mcp server.
ADK sessions carry nothing between them: no ADK MemoryService, no session state.
A new session "remembers" you only because the agent reads the MCP memory tools.

This replaces `agent/demo.py` (kept for reference) as the primary demo.

## Prerequisites

- The server is deployed (Part 4): `https://memory-mcp-140715294513.us-central1.run.app`.
- APIs on `ltm-memory-server`: Vertex AI (for the agent's own model) is already enabled.
- Your Google login for the agent's model: `gcloud auth application-default login`.
- Auth0 demo users `alice@demo.test` and `bob@demo.test` (Part 4 setup).
- Dependencies: `uv sync --group agent` (from `memory-mcp/`).
- Settings: `cp demo_agent/.env.example demo_agent/.env`. **Create this file**: if it is
  missing, ADK keeps walking up and loads `memory-mcp/.env`, the server's settings.

| Variable | Example | Meaning |
| --- | --- | --- |
| `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` | The agent's model runs on Vertex AI. |
| `GOOGLE_CLOUD_PROJECT` | `ltm-memory-server` | |
| `GOOGLE_CLOUD_LOCATION` | `global` | `gemini-3.5-flash` returns 404 from `us-central1`. |
| `DEMO_AGENT_MODEL` | `gemini-3.5-flash` | `gemini-2.5-flash` retires on 2026-10-20. |
| `MEMORY_MCP_URL` | `https://memory-mcp-140715294513.us-central1.run.app/mcp` | Must end in `/mcp`. |
| `DEMO_TENANT_ID` / `DEMO_USER_ID` | `demo-tenant` / `alice` | Which cached Auth0 token to use. |
| `AUTH0_DOMAIN`, `AUTH0_CLIENT_ID`, `AUTH0_AUDIENCE` | | For `scripts/demo_login.py`. |

### How identity works

The server takes identity only from the Auth0 token: tenant = the deployment's
`TENANT_ID` (`demo-tenant`), user = a hash of the Auth0 subject. `DEMO_USER_ID`
picks *which signed-in Auth0 user's* token the agent sends; it is never sent as a
tool argument, and ADK's own user id (`user` in adk web) is ignored.

`scripts/demo_login.py` caches the token in `~/.cache/memory-mcp/tokens/` (owner-only
file) after checking you signed in as `<user>@demo.test`. The agent reads it on every
MCP request (ADK `header_provider`), so if it expires mid-demo you just sign in again;
no restart needed. Tokens last 24 hours (Auth0 API setting).

## Commands (from `memory-mcp/`)

```sh
uv sync --group agent
cp demo_agent/.env.example demo_agent/.env

# 1. Sign in the demo user(s) once (browser sign-in; add --password for the password grant)
uv run --group agent python scripts/demo_login.py --user alice
uv run --group agent python scripts/demo_login.py --user bob        # only for the isolation step

# 2. Clean slate, then wait ~1 minute for search to settle
uv run --group agent python scripts/reset_demo_user.py --user alice

# 3. Rehearse headlessly (resets, runs both sessions, prints PASS/FAIL)
uv run --group agent python scripts/rehearse_demo.py

# 4. Reset again, wait ~1 minute, then start the UI
uv run --group agent python scripts/reset_demo_user.py --user alice
uv run --group agent adk web demo_agent
# open http://127.0.0.1:8000 and pick "memory_agent"
```

Isolation demo (second terminal, different port):

```sh
DEMO_USER_ID=bob uv run --group agent adk web demo_agent --port 8001
```

Variables set on the command line win over `demo_agent/.env`.

## Live demo script

Keep the **Events** (trace) panel open on the left of the ADK web UI.

| # | You type | The agent should | Point at |
| --- | --- | --- | --- |
| 1 | "Hi! I'm a backend engineer based in Pune. I'm vegetarian and I mostly work on a ThinkPad X1." | Call `list_memories` + `search_memory` (empty), reply naturally, call `add_memory`. | Events: the `add_memory` call and its result (`saved`, ADD ×3). |
| 2 | "What do you remember about me?" | Call `list_memories`, summarise by topic, no ids. | The `list_memories` call. If it is empty, wait ~60s (indexing) and ask again. |
| 3 | "I've moved to Bengaluru." | Call `add_memory`; the server returns `op: UPDATE`. Ask step 2 again. | `UPDATE` in the result; the list shows Bengaluru, Pune is gone, no duplicate. |
| 4 | "I'm taking medical leave next week for surgery." | Call `add_memory`; result `sensitive_not_saved: 1`; says kindly that it wasn't saved for privacy, without repeating it. | The counter in the result; ask step 2 again: unchanged. |
| 5 | (wait ~60s, explain the architecture slide) then click **New Session** | | New session id, empty chat. |
| 6 | "Suggest a good dinner spot and a laptop bag for me." | Call `list_memories` and `search_memory` FIRST, then open with "Welcome back…" and suggest vegetarian dinner in Bengaluru and a bag that fits a ThinkPad X1. | Tool calls before the first text; nothing typed about you in this session. |
| 7 | (optional) In the bob UI: "What do you remember about me?" | Nothing remembered. | Same server, other token, empty memory. |
| 8 | "Forget my city." | Find the city memory, say which one, ask to confirm. You: "yes". Then `delete_memory`. | The `delete_memory` call happens only after your "yes". |

## Timing you should expect

- `list_memories` and `search_memory` both read the search index, which picks up new
  writes after about **60 seconds** (measured on this deployment). Calling both at the
  start of a session does not make it fresher; the wait before the new session does.
  `add_memory` itself sees recent memories for UPDATE/NONE decisions.
- `add_memory` takes about 3-12s (DLP + 1-2 Gemini calls + writes); the agent's own model
  adds a few seconds per turn.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Terminal shows `WARNING: No token for demo user` or "expired" | Run `scripts/demo_login.py --user <user>`. No restart needed. |
| Agent answers without memory; log says "failed to load" toolset | Token missing/expired or wrong `MEMORY_MCP_URL`. Check both; run `scripts/call.sh` with a fresh token to test the server directly. |
| `401` from the server | Token audience must be `https://memory-mcp.example` (`AUTH0_AUDIENCE`). The server does its own token check; Cloud Run IAM (`roles/run.invoker`) is not used, the service allows unauthenticated invocation by design. A `403` would come from Cloud Run itself: check the service still allows unauthenticated invocation. |
| Memory empty right after writing | Indexing delay, ~60s. Wait and ask again. |
| Agent doesn't call the tools | Check `DEMO_AGENT_MODEL` and that the tools loaded (Events panel). Tune `memory_agent/instructions.py`. |
| `memory_agent` not in the app list | Run `adk web demo_agent` from `memory-mcp/` (the folder that *contains* `memory_agent/`). |
| First call slow (5-10s) | Cloud Run cold start. Run the rehearsal shortly before the demo, or set `--min-instances 1` on the service for the demo window. |
| `MEMORY_MCP_URL is not set` | `demo_agent/.env` missing. Copy it from `.env.example`. |

`adk web` keeps its sessions in `demo_agent/memory_agent/.adk/session.db` (git-ignored).
That is chat history only; the agent never reads it as memory.
