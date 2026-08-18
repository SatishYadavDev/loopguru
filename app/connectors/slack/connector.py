"""SlackConnector — implements SourceConnector for Slack (PRD §7, §9).

The sync layer (app/workers/sync.py) drives checkpoints/threads/locking and calls
the lower-level helpers here (`iter_history_pages`, `get_thread_replies`) for
resumable pagination. `fetch()` satisfies the generic Protocol.
"""

from __future__ import annotations

from collections.abc import Iterator

from app.connectors.slack.client import SlackApiPermanentError, SlackClient
from app.connectors.slack.files import FileSkip, extract_file_text, file_ref_from_slack
from app.connectors.slack.identity import (
    load_identity_map,
    refresh_channel_names,
    refresh_identity_cache,
)
from app.connectors.slack.normalizer import message_text
from app.connectors.slack.permalink import build_permalink
from app.core.config import settings
from app.core.ids import slack_file_doc_id, slack_message_doc_id
from app.core.logging import get_logger
from app.schemas.connector import RawItem, Scope, ScopeStatus, SyncCursor
from app.schemas.document import Document

log = get_logger(__name__)

SOURCE = "slack"

# System subtypes with no knowledge value (PRD §9.8).
DROP_SUBTYPES = {
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "tombstone",
    "bot_add",
    "bot_remove",
    "pinned_item",
}


class SlackConnector:
    source_name = SOURCE

    def __init__(self, client: SlackClient | None = None) -> None:
        self.client = client or SlackClient()
        self.names: dict[str, str] = {}
        self.subdomain: str = settings.slack_workspace_subdomain
        self._prepared = False

    # ---- lifecycle ---------------------------------------------------------

    def prepare(self) -> None:
        """Refresh identity cache + resolve workspace subdomain once per run."""
        if self._prepared:
            return
        auth = self.client.auth_test()
        if not self.subdomain:
            url = auth.get("url", "")  # e.g. https://your-workspace.slack.com/
            self.subdomain = url.replace("https://", "").replace("http://", "").split(".")[0]
        refresh_identity_cache(self.client)
        refresh_channel_names(self.client)
        self.names = load_identity_map()
        self._prepared = True
        log.info("slack connector prepared", extra={"subdomain": self.subdomain})

    # ---- scopes ------------------------------------------------------------

    def list_scopes(self) -> list[Scope]:
        scopes: list[Scope] = []
        for cid in settings.slack_channels:
            scopes.append(self._resolve_scope(cid))
        return scopes

    def _resolve_scope(self, channel_id: str) -> Scope:
        try:
            info = self.client.call("conversations_info", channel=channel_id)
        except SlackApiPermanentError as e:
            status = (
                ScopeStatus.NOT_FOUND
                if e.error == "channel_not_found"
                else ScopeStatus.NOT_ACCESSIBLE
            )
            return Scope(SOURCE, channel_id, status=status, detail=e.error)

        ch = info.get("channel", {})
        name = ch.get("name", channel_id)
        is_private = bool(ch.get("is_private", False))
        if ch.get("is_archived"):
            return Scope(SOURCE, channel_id, name, is_private, ScopeStatus.ARCHIVED, "archived")
        if not ch.get("is_member", False):
            if not is_private and settings.slack_auto_join:
                try:
                    self.client.call("conversations_join", channel=channel_id)
                    return Scope(SOURCE, channel_id, name, is_private, ScopeStatus.OK)
                except SlackApiPermanentError as e:
                    return Scope(SOURCE, channel_id, name, is_private,
                                 ScopeStatus.NOT_ACCESSIBLE, e.error)
            return Scope(
                SOURCE, channel_id, name, is_private, ScopeStatus.NOT_ACCESSIBLE,
                "bot not a member — invite the bot to this channel",
            )
        return Scope(SOURCE, channel_id, name, is_private, ScopeStatus.OK)

    # ---- fetching ----------------------------------------------------------

    def iter_history_pages(
        self, channel_id: str, oldest: str | None = None, start_cursor: str | None = None
    ) -> Iterator[tuple[list[dict], str | None]]:
        """Yield (messages, next_cursor) per page so the sync layer can persist the
        cursor for resume (PRD §11.1). conversations.history returns newest-first.
        """
        cursor = start_cursor
        while True:
            resp = self.client.call(
                "conversations_history",
                channel=channel_id,
                oldest=oldest,
                cursor=cursor,
                limit=200,
                inclusive=False,
            )
            messages = resp.get("messages", []) or []
            next_cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            yield messages, next_cursor
            if not next_cursor:
                return
            cursor = next_cursor

    def get_thread_replies(self, channel_id: str, thread_ts: str) -> list[dict]:
        replies: list[dict] = []
        for m in self.client.paginate(
            "conversations_replies", "messages", channel=channel_id, ts=thread_ts
        ):
            if m.get("ts") == thread_ts:
                continue  # skip parent duplicate (PRD §9.8)
            replies.append(m)
        return replies

    def fetch(self, scope: Scope, cursor: SyncCursor | None) -> Iterator[RawItem]:
        """Generic Protocol fetch: flatten history into top-level RawItems."""
        oldest = cursor.oldest if cursor else None
        for messages, _ in self.iter_history_pages(scope.scope_id, oldest=oldest):
            for msg in messages:
                yield RawItem(SOURCE, scope.scope_id, "message", msg)

    # ---- normalization -----------------------------------------------------

    def _keep_message(self, msg: dict) -> bool:
        subtype = msg.get("subtype")
        if subtype in DROP_SUBTYPES:
            return False
        if subtype == "bot_message":
            bot_id = msg.get("bot_id") or msg.get("user", "")
            return bot_id in settings.useful_bot_ids
        return True

    def to_documents(self, item: RawItem) -> list[Document]:
        msg = item.payload
        channel_id = item.scope_id
        if not self._keep_message(msg):
            return []

        ts = msg.get("ts", "")
        if not ts:
            return []
        created_epoch = int(float(ts))
        thread_ts = msg.get("thread_ts")
        author = self.names.get(msg.get("user", ""), msg.get("user", ""))
        permalink = build_permalink(self.subdomain, channel_id, ts, thread_ts)
        text = message_text(msg, self.names, self.names)
        # Files can be direct uploads (msg.files) OR inside a shared/forwarded message's
        # attachments (attachments[].files). Collect both, deduped by file id.
        files: list[dict] = list(msg.get("files", []) or [])
        for att in msg.get("attachments", []) or []:
            files.extend(att.get("files", []) or [])
        _seen_ids: set[str] = set()
        files = [
            f for f in files
            if f.get("id") and not (f["id"] in _seen_ids or _seen_ids.add(f["id"]))
        ]

        file_errors: list[dict] = []
        docs: list[Document] = []

        # -- file documents (one per file, deduped downstream by file_id) --
        for f in files:
            ref = file_ref_from_slack(f)
            if not ref.downloadable:
                file_errors.append(
                    {"file_id": ref.file_id, "reason": "external/hosted or no url",
                     "retryable": False}
                )
                continue
            try:
                ftext = extract_file_text(ref)
            except FileSkip as e:
                file_errors.append(
                    {"file_id": ref.file_id, "reason": str(e), "retryable": False}
                )
                continue
            except Exception as e:  # noqa: BLE001 - network/parse; retry next run
                file_errors.append(
                    {"file_id": ref.file_id, "reason": str(e), "retryable": True}
                )
                continue
            docs.append(
                Document(
                    doc_id=slack_file_doc_id(ref.file_id),
                    source=SOURCE,
                    kind="file",
                    scope_ids=[channel_id],
                    text=ftext,
                    title=ref.name,
                    author=author,
                    created_ts=ts,
                    created_epoch=created_epoch,
                    permalink=permalink,
                    thread_id=thread_ts,
                    metadata={
                        "file_id": ref.file_id,
                        "mime": ref.mime,
                        "bytes": ref.size,
                        "sharing_text": text,
                        "message_doc_id": slack_message_doc_id(channel_id, ts),
                        "edited_ts": (msg.get("edited") or {}).get("ts"),
                    },
                )
            )

        # -- message document (skip empty text-only-file messages) --
        if text.strip():
            docs.insert(
                0,
                Document(
                    doc_id=slack_message_doc_id(channel_id, ts),
                    source=SOURCE,
                    kind="message",
                    scope_id=channel_id,
                    text=text,
                    title=None,
                    author=author,
                    created_ts=ts,
                    created_epoch=created_epoch,
                    permalink=permalink,
                    thread_id=thread_ts,
                    attachments=[file_ref_from_slack(f) for f in files],
                    metadata={
                        "subtype": msg.get("subtype"),
                        "edited_ts": (msg.get("edited") or {}).get("ts"),
                        "file_errors": file_errors,
                    },
                ),
            )
        elif file_errors:
            # surface file errors even when there's no message doc to carry them
            for fdoc in docs:
                fdoc.metadata.setdefault("file_errors", file_errors)

        return docs
