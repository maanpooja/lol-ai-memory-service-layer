# memory-mcp

An MCP server that gives AI agents long-term, privacy-aware memory about their
users. Deployed on Google Cloud Run; memories live in Vertex AI Search
(Discovery Engine). This is a hackathon prototype: keep it simple and working.

## Status

- **Part 1 (done):** scaffold, data store setup, storage layer, smoke test.
- **Part 2 (done):** MCP server `memory-layer` (`server.py`) with add, search,
  update, delete and list tools, a STUB identity (`identity.py`), stdio and
  streamable HTTP transports.
- **Part 3 (done):** governance pipeline in `service.py`: Cloud DLP redaction
  (`governance/redactor.py`), code-level policy (`governance/policy.py`), Gemini
  on Vertex AI (`llm/`, prompts in `prompts/*.md`). `add_memory` takes free
  text; add, update and search go through `MemoryService`.
- **Part 4 (done):** OIDC bearer auth (`oidc.py`, Auth0 as issuer), Cloud Run
  deployment (`Dockerfile`, `deploy/`), audit logs to BigQuery via a log sink,
  ADK demo agent (`agent/`, dependency group `agent`).
- **Part 5 (done):** `demo_agent/memory_agent` for `adk web demo_agent`; identity from a
  cached Auth0 token per `DEMO_USER_ID` (scripts/demo_login.py), sent via header_provider;
  scripts/reset_demo_user.py and scripts/rehearse_demo.py. Never use ADK session state or a
  MemoryService as memory in the demo agent; the MCP tools are its only memory.

## Rules that must always hold

- Every memory is one Discovery Engine document in a structured data store.
  Document id = memory id (uuid4). Fields live in `struct_data`.
- Tenant and user identity will come from a verified token, never from tool
  arguments. Every `MemoryStore` method takes `tenant_id` and `user_id`
  explicitly.
- Every search and list applies a filter on BOTH `tenant_id` and `user_id`.
  The filter is built only by `build_owner_filter` in
  `store/discovery_engine.py`. Do not build filter strings anywhere else.
- `get`, `update` and `delete` fetch the document and compare the stored
  `tenant_id` and `user_id`. A foreign memory and a missing memory must look
  identical to the caller (`None`, `False`, or the same `PermissionError`).
- Never log memory content (`fact`). Log ids and actions only. Error messages
  must not contain content either.
- Only synthetic data in tests and the smoke test.

## MCP server rules

- Identity is NEVER a tool argument. No tool may have a `tenant_id`, `user_id`
  or `app_id` parameter; an LLM calls these tools and must not choose whose
  memory it touches. Tools read identity only through `get_identity()`.
  `tests/test_server.py` enforces this against the tool schemas.
- The identity provider is chosen in one place: `get_identity_provider` in
  `identity.py`, by `AUTH_MODE`. Only `dev` (a stub) exists. Real auth is added
  there, as a new `IdentityProvider`.
- Request headers are client input. Only `DevIdentityProvider` reads them, and
  only when `DEV_ALLOW_HEADER_OVERRIDE=true` on HTTP.
- A missing memory and someone else's memory produce the same tool error,
  "Memory not found".
- Never log content: no fact text and no search queries, in logs, audit lines
  or error messages. The audit line (`audit` in `server.py`) carries ids only.
- In stdio mode stdout belongs to the MCP protocol. Nothing else may write to
  stdout; warnings, logs and audit lines go to stderr.
- The store, redactor and LLM client are injected:
  `create_server(store, redactor, llm, provider, policy)` is used by `main()`
  and by the tests (with `tests/fakes.py`). Build them once at startup, never
  per request.
- Tools raise `ToolError` with a short, safe message. Store and unexpected
  errors are converted in `tool_call`; internals never reach the client.

## Governance pipeline rules (Part 3)

- Raw user text (content, new facts, queries) goes through the redactor BEFORE
  any LLM call. No code path may send unredacted text to the model.
- Writes fail closed: if DLP or the LLM fails, `add_memory` and `update_memory`
  store nothing and return a clean error. Never fall back to unredacted text.
  Reads degrade: `search_memory` falls back to store order (`selection:
  "fallback"`), because it only touches the caller's own memories.
- Policy decisions are made in code (`Policy.judge`, `service.py`), not by the
  model: sensitivity, authority claims, allowed and blocked categories,
  confidence, placeholder tokens, second DLP pass, de-duplication, TTLs. Never
  trust counts or verdicts the model reports about itself.
- The model never sees real memory ids. Candidates get refs (`m1`, `m2`, ...)
  that are mapped back in code; a ref we did not supply is ignored (UPDATE
  becomes ADD, unknown search refs are dropped).
- Never log content or LLM I/O: no facts, queries, prompts or model output in
  logs, audit lines or errors. Audit lines carry ids and numeric counters only.
  Blocked content is never echoed back to the caller.
- Prompts live in `src/memory_mcp/prompts/*.md`, are loaded once at startup,
  wrap untrusted input in `<untrusted_data>` delimiters (stripped from the data
  itself by `service.wrap`), and say the data is never instructions.
- LLM outputs are pydantic models in `llm/schemas.py`. GeminiClient uses JSON
  mode with the schema, temperature 0, and retries once on invalid output.
- DLP infoType names live in one constant, `INFO_TYPES`. Unknown names make DLP
  reject every request, so check changes with `scripts/check_dlp_info_types.py`.
- `create_server(store, redactor, llm, provider, policy)`: tests inject
  `FakeRedactor` and `FakeLlm` from `tests/fakes.py`. No test may need GCP.
- Inspect the installed `google-genai` and `google-cloud-dlp` packages before
  using an API you have not used here.

## Auth and deployment rules (Part 4)

- `AUTH_MODE=oidc` in every deployment. The dev stub must never be deployed.
- Tokens are verified only by `OidcTokenVerifier` (signature via JWKS, iss, aud,
  exp, iat, sub; asymmetric algorithms only). Never log tokens or claims.
- user_id is `stable_id("u", iss, sub)`; never store or log raw `sub` or email.
- The runtime service account keeps only `deploy/runtime-role.yaml` plus
  `roles/dlp.user`. Add a permission there only when a new API call needs it.
- Audit lines go to stderr as JSON with `severity`; BigQuery gets them through
  the log sink, never through direct writes from the server.
- The image (Dockerfile, `.gcloudignore`) must not contain `.env`, tests,
  scripts or the agent; prompts under `src/` must be included.
- The agent (`agent/`) talks to the server only through MCP with the user's own
  bearer token; it never sends identity as tool arguments.

## Conventions

- Python 3.12, managed with uv. Dependencies are pinned in `pyproject.toml`.
- Typed, small, readable code. No extra frameworks or speculative abstractions.
- Config comes from env vars via `config.load_config()` (`.env` supported):
  `PROJECT_ID`, `DE_LOCATION`, `DATA_STORE_ID`, `COLLECTION`, optional `ENGINE_ID`.
  Pipeline: `GEMINI_MODEL`, `GEMINI_LOCATION`, `DLP_LOCATION`, `MAX_INPUT_CHARS`,
  `MAX_FACTS_PER_CALL`, `MIN_CONFIDENCE`, `BLOCKED_CATEGORIES`, `TTL_DAYS_<CATEGORY>`.
  Identity env vars: `AUTH_MODE`, `DEV_TENANT_ID`, `DEV_USER_ID`, `DEV_APP_ID`,
  `DEV_ALLOW_HEADER_OVERRIDE`.
- MCP SDK is `mcp` 2.x. `FastMCP` no longer exists; the class is `MCPServer`
  from `mcp.server.mcpserver`. Inspect the installed package before using an
  SDK call you have not used here.
- Timestamps are ISO-8601 UTC strings with a `Z` suffix (`models.utc_now_iso`).
- Backend errors are wrapped in `StoreError`.
- Do not invent Discovery Engine fields. Inspect the installed SDK
  (`.venv/lib/python3.12/site-packages/google/cloud/discoveryengine_v1`) or the
  current docs first; this API changes often.
- Scripts add `src/` to `sys.path` themselves and pytest uses `pythonpath = ["src"]`.
  This is deliberate: on macOS the editable-install `.pth` file can be marked
  hidden, and Python 3.12 then skips it. For the same reason the console script
  must be run with `uv run --no-editable memory-mcp`.

## Commands

```sh
uv run pytest                              # unit tests, no GCP
uv run python scripts/setup_datastore.py   # needs ADC; creates data store
uv run python scripts/smoke_test.py        # needs ADC; writes and deletes test docs
uv run python scripts/check_dlp_info_types.py  # needs ADC; read-only
uv run python scripts/eval_pipeline.py     # needs ADC; real DLP + Gemini, fake store
uv run --no-editable memory-mcp            # MCP server on stdio; needs ADC
uv run --no-editable memory-mcp --transport http --port 8080
```

Do not run the GCP scripts or call tools on the real server unless the user confirms credentials are set.

## Discovery Engine notes

- Schema is a JSON Schema object passed as `Schema.struct_schema`. Field flags
  are `retrievable`, `indexable`, `searchable`. There is no `filterable` flag:
  `indexable` is what allows filtering. `searchable` is for strings only.
- Filter syntax: `field: ANY("value")`, joined with `AND`. Literals are
  double-quoted; backslash and double quote must be escaped.
- `documents.list` has no filter parameter, so `list_all` is an empty-query
  search. `list_all_scan` is the unindexed fallback.
- Search is eventually consistent after writes; `get` is a direct fetch. Use
  the smoke test's latency numbers when deciding on read-your-writes behaviour.
- A search app (engine) is REQUIRED. Documents are not indexed until the data
  store is connected to one; each document's `index_status` says so. Without
  it, search returns nothing or rejects the filter with `Unsupported field
  "tenant_id"`. `setup_datastore.py` creates `memories-app`, and `ENGINE_ID`
  must be set so searches use `engines/.../servingConfigs/default_search`.
