#!/usr/bin/env python3
"""Interactive setup for outline-mcp-server in Claude Desktop.

Cross-platform (macOS / Windows / Linux). Prompts for your Outline URL + API token and merges an
``outline`` entry into your Claude Desktop config, backing up any existing file first.

    python3 scripts/setup.py          # macOS / Linux
    python  scripts\\setup.py          # Windows

The generated entry launches the server from *this checkout* via ``uv run`` — no PyPI publish
needed. Pass ``--published`` once on PyPI to emit the ``uvx outline-mcp-server`` form instead.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import sys
from pathlib import Path

DEFAULT_API_URL = "https://your-outline.example.com/api"
SERVER_KEY = "outline"


def claude_config_path() -> Path:
    """Return the Claude Desktop config path for the current OS."""
    system = platform.system()
    if system == "Darwin":
        return Path.home() / "Library/Application Support/Claude/claude_desktop_config.json"
    if system == "Windows":
        root = Path(os.environ.get("APPDATA") or (Path.home() / "AppData/Roaming"))
        return root / "Claude" / "claude_desktop_config.json"
    # Linux / other: no official Claude Desktop, but community builds use XDG config.
    root = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    return root / "Claude" / "claude_desktop_config.json"


def _ask(label: str, default: str | None = None, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    raw = getpass.getpass(f"{label}{suffix}: ") if secret else input(f"{label}{suffix}: ").strip()
    return raw or (default or "")


def build_entry(repo_dir: Path, api_url: str, token: str, readonly: bool, published: bool) -> dict:
    env = {"OUTLINE_API_URL": api_url, "OUTLINE_API_TOKEN": token}
    if readonly:
        env["OUTLINE_MCP_READONLY"] = "true"
    if published:
        return {"command": "uvx", "args": ["outline-mcp-server"], "env": env}
    return {
        "command": "uv",
        "args": ["run", "--directory", str(repo_dir), "outline-mcp-server"],
        "env": env,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure outline-mcp-server for Claude Desktop.")
    parser.add_argument(
        "--published",
        action="store_true",
        help="Emit the `uvx outline-mcp-server` form (once published to PyPI).",
    )
    args = parser.parse_args()

    repo_dir = Path(__file__).resolve().parent.parent
    print("== outline-mcp-server -> Claude Desktop setup ==\n")

    api_url = _ask("Outline API URL", DEFAULT_API_URL)
    token = _ask("Outline API token (Outline -> Settings -> API Tokens)", secret=True)
    if not token:
        sys.exit("An API token is required. Create one in Outline -> Settings -> API Tokens.")
    readonly = _ask("Read-only, no write tools? y/N", "N").lower().startswith("y")

    cfg_path = claude_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    config: dict = {}
    if cfg_path.exists():
        backup = cfg_path.with_suffix(".json.bak")
        shutil.copy2(cfg_path, backup)
        print(f"Backed up existing config -> {backup}")
        try:
            config = json.loads(cfg_path.read_text() or "{}")
        except json.JSONDecodeError:
            sys.exit(f"Config at {cfg_path} is not valid JSON; fix or move it, then rerun.")

    config.setdefault("mcpServers", {})[SERVER_KEY] = build_entry(
        repo_dir, api_url, token, readonly, args.published
    )
    cfg_path.write_text(json.dumps(config, indent=2) + "\n")

    print(f"\n[ok] Wrote '{SERVER_KEY}' server to {cfg_path}")
    if shutil.which("uv") is None and not args.published:
        print(
            "[warn] 'uv' is not on PATH — install it from https://docs.astral.sh/uv/ ; "
            "the config launches the server via `uv run`."
        )
    print("Next: fully quit and reopen Claude Desktop, then find the Outline tools (search icon).")


if __name__ == "__main__":
    main()
