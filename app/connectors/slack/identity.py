"""Identity resolution: bulk user/channel name maps, cached in Postgres (PRD §9.6)."""

from __future__ import annotations

from app.connectors.slack.client import SlackClient
from app.core.logging import get_logger
from app.storage.db import session_scope
from app.storage.db.repositories import bulk_upsert_identities, identity_map

log = get_logger(__name__)

SOURCE = "slack"


def _display_name(user: dict) -> str:
    profile = user.get("profile", {}) or {}
    return (
        profile.get("display_name")
        or profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
        or user["id"]
    )


def refresh_identity_cache(client: SlackClient) -> None:
    """Bulk-fetch users.list once and persist ID -> display_name (PRD §9.6)."""
    users: dict[str, str] = {}
    for u in client.paginate("users_list", "members", page_limit=200):
        users[u["id"]] = _display_name(u)
    if users:
        with session_scope() as session:
            bulk_upsert_identities(session, SOURCE, users, kind="user")
        log.info("identity cache refreshed", extra={"users": len(users)})


def load_identity_map() -> dict[str, str]:
    with session_scope() as session:
        return identity_map(session, SOURCE)


def refresh_channel_names(client: SlackClient) -> dict[str, str]:
    """Fetch channel id -> name for the channels the bot can see; cache them."""
    channels: dict[str, str] = {}
    for ch in client.paginate(
        "users_conversations", "channels",
        types="public_channel,private_channel", page_limit=200,
    ):
        channels[ch["id"]] = ch.get("name", ch["id"])
    if channels:
        with session_scope() as session:
            bulk_upsert_identities(session, SOURCE, channels, kind="channel")
    return channels
