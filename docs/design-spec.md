# Outline MCP Server — Design Spec

**Status:** Draft v0.4
**Owner:** Geoffrey Ducournau
**Repo:** `outline-mcp-server` (standalone, public — to be published on GitHub)
**Last updated:** 2026-07-07

---

## 1. Overview

A standalone [Model Context Protocol](https://modelcontextprotocol.io) server that exposes an
[Outline](https://www.getoutline.com) knowledge base to MCP-capable clients — **Claude Desktop,
Claude.ai, ChatGPT Desktop**, and any other MCP host — so users can search, read, write, and
comment on their Outline documents through natural language.

The server is a **thin, near-stateless proxy** over Outline's documented public REST API. It has no
database and no persistent store; its only state is an ephemeral, in-memory per-session token cache
used in passthrough mode (§4). Every tool call is forwarded to `https://<outline-host>/api/…` with
an Outline API token — **the caller's own token in Passthrough mode, or the shared `OUTLINE_API_TOKEN`
in Gateway/Open/stdio** (§4). Actions therefore run with, and are limited by, whichever token's
Outline permissions apply.

It reuses the proven architecture of the internal **DimTrack MCP server** — a near-stateless REST
proxy built on the **Python MCP SDK (`FastMCP`)**, dual transport (stdio + Streamable HTTP), per-user
Bearer passthrough, and fail-fast auth. This is effectively the DimTrack `server.py` pattern
re-pointed at Outline's API.

### Why Python + FastMCP (and why it's fine next to Outline)

- **Maximum reuse.** Copies DimTrack's `server.py` structure almost verbatim — same SDK, same
  transport selection, same `ContextVar` auth passthrough, same compaction helpers.
- **No conflict with Outline.** The server is an **isolated process/container** that only speaks
  HTTP to `https://your-outline.example.com/api/…`. It never shares Outline's Node process,
  dependencies, or database. Outline is agnostic to the MCP server's language.
- **Publishable & instance-agnostic.** Ships under our own GitHub + license, no entanglement with
  Outline's BSL-1.1 source; works against any Outline (self-hosted or Cloud) via URL + token.

---

## 2. Goals & non-goals

### Goals
- One-command local use (`uvx outline-mcp-server`) for an individual with their own API token.
- Hosted multi-user mode (Streamable HTTP) where a single deployment serves a team, each request
  authenticated by the caller's own token.
- Full v1 surface: **read + write + comments**.
- Least-privilege safety switch even though Outline tokens are not natively scoped.

### Non-goals (v1)
- Implementing an OAuth 2.1 authorization server. (Bearer-token auth only; OAuth is future work —
  see §11.)
- Admin/team-management tools (user CRUD, collection permissions, groups).
- Document / API-response caching, sync, or any persistent store (the ephemeral in-memory
  session-token cache of §4 is the sole exception).
- Webhooks / real-time subscriptions.

---

## 3. Architecture

```
┌──────────────┐   MCP (stdio | Streamable HTTP)   ┌────────────────────┐   HTTPS + Bearer   ┌──────────────┐
│ Claude /     │ ────────────────────────────────▶ │ outline-mcp-server │ ─────────────────▶ │  Outline API │
│ ChatGPT host │ ◀──────────────────────────────── │ (near-stateless px)│ ◀───────────────── │  /api/*      │
└──────────────┘         tool calls / results       └────────────────────┘   JSON envelope    └──────────────┘
```

- **Language / SDK:** Python ≥ 3.11, official **`mcp[cli]` SDK via `FastMCP`** (same as DimTrack).
- **HTTP client:** `httpx` — one small wrapper `_request()`.
- **Input schemas:** Python type hints + `Literal` enums on each `@mcp.tool()` (FastMCP derives the
  JSON schema); Pydantic where a structured body helps.
- **Runtime deps:** `mcp[cli]>=1.28.1,<2`, `httpx>=0.27`, `uvicorn>=0.30`. The floor must ship
  **both** `streamable_http_app()` **and** `mcp.server.transport_security.TransportSecuritySettings`.
  Verified: `mcp==1.9.0` has the former but **not** the latter, so `>=1.9` is wrong. `1.28.1` is a
  known-good pin. The exact release where `transport_security` landed is unconfirmed; at scaffold,
  either lower the floor to that release once identified, or keep the known-good `1.28.1` — do not
  loosen below a version actually tested to import `TransportSecuritySettings`.

### Transports (selected by `MCP_TRANSPORT`)
| Value | How | Use |
|---|---|---|
| `stdio` (default) | `mcp.run()` | Per-user local — Claude/ChatGPT Desktop spawn the process. |
| `http` / `streamable-http` | `mcp.streamable_http_app()` served by **uvicorn** on `MCP_PORT` | Hosted, one deployment for a team. |

HTTP mode serves the Streamable HTTP ASGI app via uvicorn (default port `9000`). Streamable HTTP is
a single HTTP endpoint that may **stream responses SSE-style** (chunked `text/event-stream`) — it is
*not* a WebSocket upgrade. It must sit behind a reverse proxy with **response buffering off** and
long read timeouts so streamed chunks aren't held back (see §8).

---

## 4. Authentication model

Two **independent** concerns must not be conflated (the earlier draft did):

- **Inbound** — how an MCP *client* proves it may use this server.
- **Upstream** — which Outline API token the server sends to `/api/*`. Outline **always** requires
  `Authorization: Bearer <API_KEY>`; the key is a **personal API token** minted per user in Outline
  → **Settings → API Tokens**, carrying that user's full permissions.

The server never mints tokens or writes them to disk. It supports three **inbound strategies**, each
of which fixes where the **upstream** token comes from. In `http` mode the startup guard requires
**exactly one** inbound strategy; `stdio` mode has no inbound auth (the host spawns the process
locally) and always uses the shared env token.

| Inbound strategy | Enable | Client must send | Upstream token used | For |
|---|---|---|---|---|
| **Passthrough** (per-user) | `MCP_ALLOW_OUTLINE_TOKEN_AUTH=true` | `Authorization: Bearer <their Outline token>` | the forwarded per-caller token | hosted, multi-user |
| **Gateway** (shared identity) | `MCP_AUTH_TOKEN=<gateway secret>` | `Authorization: Bearer <gateway secret>` | `OUTLINE_API_TOKEN` (**required**) | hosted, single-identity |
| **Open** (no inbound auth) | `MCP_ALLOW_UNAUTHENTICATED=true` | — | `OUTLINE_API_TOKEN` (**required**) | trusted private network only |

- **Passthrough** forwards the caller's token via a Starlette `BaseHTTPMiddleware` that reads the
  incoming `Authorization` header into a request-scoped `ContextVar` (`_request_token`); `_request()`
  sends that exact token upstream, so actions attribute to the calling user. `OUTLINE_API_TOKEN` is
  **ignored** in this mode. → *direct copy of DimTrack's `ContextVar` + `_BearerAuth` middleware.*
- **Gateway** and **Open** both **require** `OUTLINE_API_TOKEN` — Outline cannot be called without a
  token, which is exactly why standalone "open MCP" still needs a shared upstream key. Gateway gates
  inbound requests behind a shared secret; Open does not and must run only on a trusted network.
- **stdio/local:** no inbound auth; `OUTLINE_API_TOKEN` supplied via the host-provided `env`.

**Session token cache (passthrough only).** Some bridges (`mcp-remote`) don't re-attach the
`Authorization` header on the session `GET`/SSE stream, so passthrough keeps a small **in-memory,
non-persistent** map `_session_tokens[mcp-session-id] → token`. This is the one piece of state the
server holds, so its lifecycle is specified explicitly:
- **Ephemeral:** memory only, never written to disk; lost (and required to be re-sent) on restart.
- **TTL:** entries expire after `MCP_SESSION_TTL` seconds of inactivity (default `1800`).
- **Bounded:** capped at `MCP_SESSION_MAX` entries (default `10000`), LRU-evicted.
- **Bootstrap:** the session id is assigned by the *server* in the initialize response, so the
  middleware wraps `send` and captures the response `mcp-session-id` header (pairing it with the
  bearer token) — otherwise a later header-less stream `GET` could never resolve.
- **Cleanup:** entry removed on the transport's session `DELETE`/close.
- **Secret handling:** `mcp-session-id` is a bearer-equivalent secret — high-entropy, never logged.

**Fail-fast:** in `http` mode the server refuses to start unless exactly one inbound strategy is set,
and errors if Gateway/Open is selected without `OUTLINE_API_TOKEN` (copies DimTrack's startup guard).
Host-header hardening via `TransportSecuritySettings(allowed_hosts=…)` guards against DNS-rebinding.

### Least-privilege safety switch
Because classic Outline tokens are **not** natively scoped (unlike DimTrack PATs, which had
`read` / `*:write` / `delete` scopes), a token that can read can also write. To preserve
least-privilege intent:

- `OUTLINE_MCP_READONLY=true` skips registration of all write/comment-create tools at startup, so a
  deployment can be guaranteed read-only regardless of token power.
- Write tools are clearly labelled destructive in their descriptions.

> Confirm during implementation whether the target instance's Outline version supports API-key
> scopes; if so, document recommended scopes as an additional layer.

---

## 5. Tools (v1 — full surface)

Every tool is a thin `@mcp.tool()` pass-through: type-hinted args (with `Literal` enums matching the
exact API strings) → `POST /api/<endpoint>` via `_request()` → compact the JSON
`{ data, pagination, policies }` envelope down to the fields an LLM needs (`_compact_*` helpers).
Results are capped (`MAX_LIMIT = 100`) with a `truncated` flag, matching DimTrack.

| Tool | Endpoint | Required args | Key optional args | Mode |
|---|---|---|---|---|
| `search_documents` | `POST /documents.search` | `query` | `collection_id`, `document_id`, `user_id`, `status_filter[]`, `date_filter`, `sort`, `direction`, `limit`, `offset` | read |
| `get_document` | `POST /documents.info` | `id` *(uuid or urlId)* | `share_id` | read |
| `list_documents` | `POST /documents.list` | — | `collection_id`, `parent_document_id`, `user_id`, `status_filter[]`, `sort`, `direction`, `limit`, `offset` | read |
| `list_collections` | `POST /collections.list` | — | `query`, `status_filter[]`, `sort`, `direction`, `limit`, `offset` | read |
| `list_comments` | `POST /comments.list` | — | `document_id`, `collection_id`, `include_anchor_text`, `limit`, `offset` | read |
| `whoami` | `POST /auth.info` | — | — | read |
| `create_document` | `POST /documents.create` | `title` | `text`, `collection_id`, `parent_document_id`, `template_id`, `publish`, `icon` | **write** |
| `update_document` | `POST /documents.update` | `id` | `title`, `text`, `publish`, `edit_mode` (`replace`\|`append`\|`prepend`\|`patch`), `find_text` (req. if `patch`) | **write** |
| `create_comment` | `POST /comments.create` | `document_id`, and one of (`text` \| `data`) | `parent_comment_id`, `anchor_text`, `anchor_prefix`, `anchor_suffix` | **write** |

Notes:
- Tool args are `snake_case` in Python; `_request()` maps them to Outline's `camelCase` body keys.
- `create_comment` accepts markdown via `text` (preferred for LLM use) or rich-text `data`; require
  at least one, mirroring the API. `anchor_prefix`/`anchor_suffix` disambiguate `anchor_text` when
  the same text repeats in a document.
- `update_document` exposes the **full** `edit_mode` surface: `replace` (default, overwrites `text`),
  `append`, `prepend`, and `patch` (targeted edit, requires `find_text`). Confirm the exact
  `TextEditMode` enum strings against the target instance's OpenAPI at implementation and map the
  `snake_case`/verb names to Outline's exact values.
- `create_document`: enforce that **`collection_id` or `parent_document_id` is provided when
  `publish=true`** (a published document must live somewhere) — validate before the API call and
  return a clear tool error rather than forwarding a request Outline will reject.
- `**write**` tools are not registered when `OUTLINE_MCP_READONLY=true`.

> "Full v1 surface" refers to **tool coverage** — read, write, and comment operations are all
> present. Each tool exposes a **curated, LLM-facing parameter subset**, not every documented Outline
> field. Intentionally omitted for now (add later if needed): `documents.search.shareId` /
> `snippetMinWords` / `snippetMaxWords`; `documents.list.backlinkDocumentId`;
> `documents.create`/`update` `color` / `fullWidth` / `dataAttributes`; `comments.create.id`.
> Admin/team-management endpoints remain deliberately out of scope (§2).

---

## 6. Configuration (env vars)

| Var | Default | Purpose |
|---|---|---|
| `OUTLINE_API_URL` | `https://your-outline.example.com/api` | Base API URL of the target Outline. |
| `OUTLINE_API_TOKEN` | — | Upstream token for **Gateway/Open** strategies and for `stdio`. Ignored in Passthrough. |
| `MCP_TRANSPORT` | `stdio` | `stdio` \| `http` / `streamable-http`. |
| `MCP_PORT` | `9000` | Port for `http` transport. |
| `MCP_ALLOW_OUTLINE_TOKEN_AUTH` | `false` | Inbound **Passthrough** strategy (per-user Bearer). *(stdio auto-uses the env token; do not combine with `OUTLINE_API_TOKEN`.)* |
| `MCP_AUTH_TOKEN` | — | Inbound **Gateway** strategy: shared secret clients must present; requires `OUTLINE_API_TOKEN`. |
| `MCP_ALLOW_UNAUTHENTICATED` | `false` | Inbound **Open** strategy (no client auth); requires `OUTLINE_API_TOKEN`; private nets only. |
| `MCP_SESSION_TTL` | `1800` | Passthrough session-token cache TTL (seconds). |
| `MCP_SESSION_MAX` | `10000` | Passthrough session-token cache max entries (LRU). |
| `MCP_ALLOWED_HOSTS` | `127.0.0.1:$MCP_PORT`, `localhost:$MCP_PORT` | Host allow-list for DNS-rebinding protection. **Hosted mode must set this explicitly** to the public host(s), e.g. `outline-mcp.example.com` — the localhost default rejects proxied domains. |
| `MCP_ALLOWED_ORIGINS` | `http(s)://<each allowed host>` | Allowed `Origin` values. Derived from `MCP_ALLOWED_HOSTS` when unset; must be set if clients send an `Origin` differing from the Host (empty would reject every non-empty Origin). |
| `OUTLINE_MCP_READONLY` | `false` | Drop all write tools. |
| `MAX_LIMIT` | `100` | Result cap per list/search tool. |
| `LOG_LEVEL` | `info` | Logging verbosity (never logs token values). |

> **`http` mode requires exactly one inbound strategy.** Default is `MCP_ALLOW_OUTLINE_TOKEN_AUTH=false`
> so a hosted deployment can't accidentally start in per-user mode without a token; you must
> explicitly choose Passthrough, Gateway, or Open. A hosted **shared-token** deployment sets
> `MCP_AUTH_TOKEN` (Gateway) — never `OUTLINE_API_TOKEN` alone, which the guard rejects.

---

## 7. Deployment

### Local (stdio) — individual user
Client config (Claude Desktop `claude_desktop_config.json`, ChatGPT Desktop equivalent):
```jsonc
{
  "mcpServers": {
    "outline": {
      "command": "uvx",
      "args": ["outline-mcp-server"],
      "env": {
        "OUTLINE_API_URL": "https://your-outline.example.com/api",
        "OUTLINE_API_TOKEN": "ol_api_…"
      }
    }
  }
}
```
(Alternatively `uv run outline-mcp-server` from a local checkout, matching DimTrack's stdio setup.)

### Hosted (Streamable HTTP) — team
- `docker compose up` a single container, `MCP_TRANSPORT=http`, behind Nginx / Nginx Proxy Manager.
- Reverse proxy for **streaming HTTP / SSE** (not WebSocket): `proxy_buffering off`,
  `proxy_read_timeout 3600s`, and `X-Accel-Buffering: no` so `text/event-stream` chunks flush
  immediately. No `Upgrade`/`Connection: upgrade` handling is needed — Streamable HTTP is plain HTTP.
- Remote clients connect via the `mcp-remote` bridge (until native remote-MCP UX is universal),
  attaching each user's `Authorization: Bearer ol_api_…` (Passthrough), or the `MCP_AUTH_TOKEN`
  secret (Gateway).
- Exposed only via the proxy at e.g. `outline-mcp.example.com/mcp`; container port not published
  to host.

---

## 8. Security considerations
- **No token persistence to disk / no token logging.** Upstream tokens live in a request-scoped
  `ContextVar`; in passthrough mode they may also sit in the **ephemeral in-memory** session cache
  (§4: TTL + LRU bound + delete-on-close). Redact tokens *and* `mcp-session-id` in all logs.
- **Fail-fast** auth guard prevents an accidentally wide-open public endpoint.
- **DNS-rebinding protection** via `TransportSecuritySettings(allowed_hosts=…, allowed_origins=…)`.
  Hosted mode **requires** `MCP_ALLOWED_HOSTS` set to the explicit public hostname(s); the server
  should refuse to start in `http` mode if it's left at the localhost default while bound to a
  non-loopback proxy. `allowed_origins` is derived as `http(s)://<host>` (overridable via
  `MCP_ALLOWED_ORIGINS`) so clients that send an `Origin` header aren't rejected outright.
- **Least-privilege** via `OUTLINE_MCP_READONLY` and clear destructive-action labelling.
- **Rate/size caps** (`MAX_LIMIT`, request timeouts) to bound blast radius and cost.
- Token power caveat surfaced in README: an Outline API token = full user permissions; advise users
  to create a dedicated token and, if the instance supports it, a limited-permission service user.

---

## 9. Repository layout
```
outline-mcp-server/
├── src/
│   └── outline_mcp/
│       ├── __init__.py
│       ├── server.py          # FastMCP instance, all @mcp.tool() defs, transport selection, main()
│       ├── client.py          # _request() (httpx) + ContextVar token + envelope/_compact_* helpers
│       ├── auth.py            # Starlette _BearerAuth middleware + fail-fast guard + session cache
│       └── config.py          # env parsing + defaults
├── docs/design-spec.md        # this file
├── Dockerfile                 # python:3.11-slim, hosted HTTP image
├── docker-compose.yml         # opt-in `--profile mcp` service
├── pyproject.toml             # deps + entry point: outline-mcp-server = "outline_mcp.server:main"
├── README.md                  # setup for stdio + hosted; tool list; security notes
├── LICENSE                    # (choose: MIT / Apache-2.0)
└── .env.example
```

(Mirrors DimTrack's `tools/dimtrack-mcp/` layout, promoted to its own repo.)

---

## 10. v1 milestones
1. **Scaffold** — `pyproject.toml`, `config.py`, `client.py` with shared-token auth + `_request()`.
2. **Read tools** — `search_documents`, `get_document`, `list_documents`, `list_collections`,
   `list_comments`, `whoami`; verify against `your-outline.example.com` over stdio (`uv run`).
3. **Write tools** — `create_document`, `update_document`, `create_comment` + `OUTLINE_MCP_READONLY`.
4. **HTTP transport** — `streamable_http_app()` + uvicorn + per-user passthrough auth (`ContextVar`
   + Starlette middleware) + fail-fast guard + `TransportSecuritySettings`.
5. **Packaging** — Dockerfile, docker-compose profile, README, `.env.example`, LICENSE; publish to
   PyPI + GitHub.

---

## 11. Open questions / future work
- **OAuth 2.1 authorization server** for a first-class "add remote MCP server" experience in
  Claude/ChatGPT without `mcp-remote`. Large; deferred.
- Confirm whether the target Outline version supports **API-key scopes**; if yes, add a recommended
  scope matrix.
- `create_comment` rich-text (`data`) shape — confirm ProseMirror JSON contract vs markdown `text`.
- License choice (MIT vs Apache-2.0).
- PyPI package name availability (`outline-mcp-server`).
