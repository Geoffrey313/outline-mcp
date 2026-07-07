"""MCP tool definitions.

Each tool validates its (curated, LLM-facing) arguments, delegates the HTTP call and response
shaping to :class:`~outline_mcp.client.OutlineClient`, and returns compact JSON. Write tools are
registered only when ``OUTLINE_MCP_READONLY`` is false.

Argument names are ``snake_case``; the payloads below map them to Outline's ``camelCase`` body keys.
"""

from __future__ import annotations

from typing import Literal

from mcp.server.fastmcp import FastMCP

from .client import (
    OutlineClient,
    compact_auth_info,
    compact_collection,
    compact_comment,
    compact_document,
    compact_document_with_text,
    compact_search_result,
)
from .config import Settings

StatusFilter = Literal["draft", "archived", "published"]
# Outline's collections.list.statusFilter only accepts "archived" (include archived collections).
CollectionStatus = Literal["archived"]
DateFilter = Literal["day", "week", "month", "year"]
DocumentSort = Literal["relevance", "createdAt", "updatedAt", "title", "index"]
Direction = Literal["ASC", "DESC"]
# NOTE: confirm the exact Outline `TextEditMode` enum strings against the target instance's OpenAPI.
EditMode = Literal["replace", "append", "prepend", "patch"]


def register_tools(mcp: FastMCP, client: OutlineClient, settings: Settings) -> None:
    """Register all read tools, plus write tools unless the server is read-only."""
    _register_read_tools(mcp, client)
    if not settings.outline_mcp_readonly:
        _register_write_tools(mcp, client)


def _register_read_tools(mcp: FastMCP, client: OutlineClient) -> None:
    @mcp.tool()
    async def search_documents(
        query: str,
        collection_id: str | None = None,
        document_id: str | None = None,
        user_id: str | None = None,
        status_filter: list[StatusFilter] | None = None,
        date_filter: DateFilter | None = None,
        sort: DocumentSort | None = None,
        direction: Direction | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        """Full-text search documents by keyword. Returns ranked snippets with their documents."""
        payload = {
            "query": query,
            "collectionId": collection_id,
            "documentId": document_id,
            "userId": user_id,
            "statusFilter": status_filter,
            "dateFilter": date_filter,
            "sort": sort,
            "direction": direction,
            "limit": limit,
            "offset": offset,
        }
        return await client.query_list("documents.search", payload, compact_search_result)

    @mcp.tool()
    async def get_document(id: str, share_id: str | None = None) -> dict:
        """Fetch a single document (including its markdown text) by UUID, urlId, or share id."""
        return await client.query_one(
            "documents.info",
            {"id": id, "shareId": share_id},
            compact_document_with_text,
        )

    @mcp.tool()
    async def list_documents(
        collection_id: str | None = None,
        parent_document_id: str | None = None,
        user_id: str | None = None,
        status_filter: list[StatusFilter] | None = None,
        sort: DocumentSort | None = None,
        direction: Direction | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        """List documents, optionally scoped to a collection, parent, or author."""
        payload = {
            "collectionId": collection_id,
            "parentDocumentId": parent_document_id,
            "userId": user_id,
            "statusFilter": status_filter,
            "sort": sort,
            "direction": direction,
            "limit": limit,
            "offset": offset,
        }
        return await client.query_list("documents.list", payload, compact_document)

    @mcp.tool()
    async def list_collections(
        query: str | None = None,
        status_filter: list[CollectionStatus] | None = None,
        sort: DocumentSort | None = None,
        direction: Direction | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        """List collections, optionally filtered by name or status."""
        payload = {
            "query": query,
            "statusFilter": status_filter,
            "sort": sort,
            "direction": direction,
            "limit": limit,
            "offset": offset,
        }
        return await client.query_list("collections.list", payload, compact_collection)

    @mcp.tool()
    async def list_comments(
        document_id: str | None = None,
        collection_id: str | None = None,
        include_anchor_text: bool | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> dict:
        """List comments, optionally scoped to a document or collection."""
        payload = {
            "documentId": document_id,
            "collectionId": collection_id,
            "includeAnchorText": include_anchor_text,
            "limit": limit,
            "offset": offset,
        }
        return await client.query_list("comments.list", payload, compact_comment)

    @mcp.tool()
    async def whoami() -> dict:
        """Return the authenticated user and team for the current token."""
        return await client.query_one("auth.info", {}, compact_auth_info)


def _register_write_tools(mcp: FastMCP, client: OutlineClient) -> None:
    @mcp.tool()
    async def create_document(
        title: str,
        text: str | None = None,
        collection_id: str | None = None,
        parent_document_id: str | None = None,
        template_id: str | None = None,
        publish: bool | None = None,
        icon: str | None = None,
    ) -> dict:
        """Create a document. Publishing requires a collection or parent document to live in."""
        if publish and not (collection_id or parent_document_id):
            raise ValueError(
                "publish=true requires either collection_id or parent_document_id"
            )
        payload = {
            "title": title,
            "text": text,
            "collectionId": collection_id,
            "parentDocumentId": parent_document_id,
            "templateId": template_id,
            "publish": publish,
            "icon": icon,
        }
        return await client.query_one("documents.create", payload, compact_document_with_text)

    @mcp.tool()
    async def update_document(
        id: str,
        title: str | None = None,
        text: str | None = None,
        publish: bool | None = None,
        edit_mode: EditMode | None = None,
        find_text: str | None = None,
    ) -> dict:
        """Update a document. `edit_mode=patch` requires `find_text` to locate the edit."""
        if edit_mode == "patch" and not find_text:
            raise ValueError("edit_mode='patch' requires find_text")
        payload = {
            "id": id,
            "title": title,
            "text": text,
            "publish": publish,
            "editMode": edit_mode,
            "findText": find_text,
        }
        return await client.query_one("documents.update", payload, compact_document_with_text)

    @mcp.tool()
    async def create_comment(
        document_id: str,
        text: str | None = None,
        data: dict | None = None,
        parent_comment_id: str | None = None,
        anchor_text: str | None = None,
        anchor_prefix: str | None = None,
        anchor_suffix: str | None = None,
    ) -> dict:
        """Add a comment to a document. Provide markdown `text` (preferred) or rich-text `data`."""
        if text is None and data is None:
            raise ValueError("create_comment requires either text or data")
        payload = {
            "documentId": document_id,
            "text": text,
            "data": data,
            "parentCommentId": parent_comment_id,
            "anchorText": anchor_text,
            "anchorPrefix": anchor_prefix,
            "anchorSuffix": anchor_suffix,
        }
        return await client.query_one("comments.create", payload, compact_comment)
