# outline-mcp-server

A [Model Context Protocol](https://modelcontextprotocol.io) server that exposes an
[Outline](https://www.getoutline.com) knowledge base to MCP clients — **Claude Desktop,
Claude.ai, ChatGPT Desktop**, and others — so you can search, read, write, and comment on your
documents through natural language.

It's a thin, near-stateless proxy over Outline's public REST API: point it at an Outline URL and an
API token and it works against any instance, self-hosted or cloud. See
[`docs/design-spec.md`](docs/design-spec.md) for the full design.

## Tools

| Tool | Does | Mode |
|---|---|---|
| `search_documents` | Full-text search, ranked snippets | read |
| `get_document` | Fetch one document with its markdown | read |
| `list_documents` | List docs (by collection / parent / author) | read |
| `list_collections` | List collections | read |
| `list_comments` | List comments on a document/collection | read |
| `whoami` | Current user + team | read |
| `create_document` | Create a document | write |
| `update_document` | Edit a document (`replace`/`append`/`prepend`/`patch`) | write |
| `create_comment` | Comment on a document | write |

Set `OUTLINE_MCP_READONLY=true` to drop all write tools.

# Setup

## 0. Prerequisites (once)

1. **Get an Outline API token** — in Outline, click your avatar → **Settings → API Tokens →
   *New token***. Copy it (looks like `ol_api_…`). Each person uses their **own** token; the server
   only ever acts with that user's permissions.
2. **Install `uv`** (a fast Python runner; the local setup launches the server with it):
   - **macOS / Linux:** `curl -LsSf https://astral.sh/uv/install.sh | sh`
   - **Windows (PowerShell):** `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`
3. **Get this repo:** `git clone <repo-url> && cd outline-mcp-server`

---

## 1. Claude Desktop — the easy way (script)

Runs on **macOS, Windows, and Linux**. From the repo folder:

```bash
# macOS / Linux
python3 scripts/setup.py

# Windows
python scripts\setup.py
```

It asks for your Outline URL + token, writes them into the right Claude Desktop config file for your
OS (backing up any existing one), then tells you to restart Claude Desktop. Done.

> Once the package is published to PyPI you can add `--published` to use the shorter `uvx` launcher.

---

## 2. Claude Desktop — manual

Prefer to edit the file yourself? Open the config for your OS and add the `outline` block below.

| OS | Config file |
|---|---|
| **macOS** | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| **Windows** | `%APPDATA%\Claude\claude_desktop_config.json` |
| **Linux** | no official Claude Desktop — use **Claude Code** (§3) |

```jsonc
{
  "mcpServers": {
    "outline": {
      "command": "uv",
      "args": ["run", "--directory", "/absolute/path/to/outline-mcp-server", "outline-mcp-server"],
      "env": {
        "OUTLINE_API_URL": "https://your-outline.example.com/api",
        "OUTLINE_API_TOKEN": "ol_api_…"
      }
    }
  }
}
```

Replace the path and token, save, then **fully quit and reopen** Claude Desktop. The Outline tools
appear under the tools/search icon. (After publishing to PyPI, this simplifies to
`"command": "uvx", "args": ["outline-mcp-server"]`.)

---

## 3. Claude Code (any OS, including Linux)

One command from the repo folder:

```bash
claude mcp add outline \
  -e OUTLINE_API_URL=https://your-outline.example.com/api \
  -e OUTLINE_API_TOKEN=ol_api_… \
  -- uv run --directory "$(pwd)" outline-mcp-server
```

---

## 4. ChatGPT (Desktop or web) — remote connector

ChatGPT connects to **hosted (remote) MCP servers only** — it can't launch a local process like
Claude Desktop can. So you first deploy the server (see **Hosted deployment** below), then in ChatGPT:

**Settings → Connectors → Add / Create** (available on paid plans / developer mode) → point it at
your server's URL, e.g. `https://outline-mcp.example.com/mcp`, and provide your Outline token as
the Bearer credential. macOS and Windows desktop apps use the same connector.

---

## 5. Remote server from Claude Desktop (via `mcp-remote`)

To connect Claude Desktop to a **hosted** instance instead of running it locally, use the
`mcp-remote` bridge (needs Node.js):

```jsonc
{
  "mcpServers": {
    "outline": {
      "command": "npx",
      "args": [
        "-y", "mcp-remote",
        "https://outline-mcp.example.com/mcp",
        "--header", "Authorization:Bearer ${OUTLINE_TOKEN}"
      ],
      "env": { "OUTLINE_TOKEN": "ol_api_…" }
    }
  }
}
```

(The `${OUTLINE_TOKEN}` indirection avoids a header-parsing quirk with spaces in some shells.)

---

# Hosted deployment (Streamable HTTP)

Run one container for a team behind a reverse proxy. Pick **exactly one** inbound auth strategy:

| Strategy | Set | Clients send | Upstream token |
|---|---|---|---|
| **Passthrough** (per-user) | `MCP_ALLOW_OUTLINE_TOKEN_AUTH=true` | their own Outline token | forwarded per caller |
| **Gateway** (shared) | `MCP_AUTH_TOKEN=<secret>` | the shared secret | `OUTLINE_API_TOKEN` |
| **Open** (private nets) | `MCP_ALLOW_UNAUTHENTICATED=true` | nothing | `OUTLINE_API_TOKEN` |

`MCP_ALLOWED_HOSTS` **must** be set to the public host(s), e.g. `outline-mcp.example.com`.

```bash
cp .env.example .env   # edit it
docker compose up -d --build   # joins the external `backend` network as `outline-mcp`
```

Point your reverse proxy (e.g. Nginx Proxy Manager → `http://outline-mcp:9000`) at it with
**streaming enabled** — Streamable HTTP streams SSE-style over plain HTTP (not a WebSocket):

```nginx
proxy_buffering off;
proxy_request_buffering off;
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
```

If you front it with Cloudflare on a **Tailscale IP**, the DNS record must be **grey-cloud
(DNS-only)** — a `100.x` address isn't publicly routable, so Cloudflare can't proxy it. Note that a
Tailscale-only endpoint is reachable by desktop apps **on your tailnet**, but not by web connectors
(claude.ai / ChatGPT web), which call from outside it.

## Configuration

All settings are environment variables — see [`.env.example`](.env.example) for the full list and
defaults. Nothing is hardcoded; everything is centralized in `src/outline_mcp/config.py`.

## Security notes

- Tokens are never written to disk or logged; in passthrough mode they live in a request-scoped
  context (plus an ephemeral, TTL-bounded session cache for bridges that drop the header).
- The server fails fast at startup on an ambiguous/unusable auth configuration.
- An Outline API token carries its user's **full permissions** — Outline API keys are not scoped.
  Prefer a dedicated token (and, if possible, a limited-permission service user), and use
  `OUTLINE_MCP_READONLY=true` where writes aren't needed.

## License

TBD (MIT or Apache-2.0) — chosen before the first published release.
