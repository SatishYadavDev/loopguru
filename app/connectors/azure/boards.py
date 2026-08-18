"""Azure DevOps Boards client — create a work item via the REST API.

Auth: Basic with an empty username + PAT (Work Items read & write scope).
Docs: POST https://dev.azure.com/{org}/{project}/_apis/wit/workitems/${type}
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_API_VERSION = "7.1"


class AzureNotConfigured(Exception):
    """Raised when Azure DevOps env vars are missing."""


@dataclass
class WorkItem:
    id: int
    url: str  # human (browser) URL
    title: str
    type: str


class AzureBoardsClient:
    def __init__(self) -> None:
        self.org = settings.azure_devops_org
        self.project = settings.azure_devops_project
        self.pat = settings.azure_devops_pat
        self.default_type = settings.azure_devops_workitem_type or "Task"

    def _ensure(self) -> None:
        if not (self.org and self.project and self.pat):
            raise AzureNotConfigured(
                "Set AZURE_DEVOPS_ORG, AZURE_DEVOPS_PROJECT and AZURE_DEVOPS_PAT in .env"
            )

    def create_work_item(
        self,
        title: str,
        description_html: str,
        work_item_type: str | None = None,
        tags: list[str] | None = None,
        assigned_to: str | None = None,
    ) -> WorkItem:
        self._ensure()
        wtype = work_item_type or self.default_type
        url = (
            f"https://dev.azure.com/{self.org}/{quote(self.project)}"
            f"/_apis/wit/workitems/${quote(wtype)}?api-version={_API_VERSION}"
        )
        patch = [
            {"op": "add", "path": "/fields/System.Title", "value": title[:255]},
            {"op": "add", "path": "/fields/System.Description", "value": description_html},
        ]
        # Some project processes require System.AssignedTo — set it when configured.
        assignee = assigned_to or settings.azure_devops_assigned_to
        if assignee:
            patch.append({"op": "add", "path": "/fields/System.AssignedTo", "value": assignee})
        if tags:
            patch.append({"op": "add", "path": "/fields/System.Tags", "value": "; ".join(tags)})

        with httpx.Client(timeout=30) as client:
            resp = client.post(
                url,
                headers={"Content-Type": "application/json-patch+json"},
                auth=("", self.pat),  # Basic base64(":<PAT>")
                json=patch,
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Azure create failed {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        return self._to_work_item(data, fallback_title=title, fallback_type=wtype)

    def get_work_item(self, work_item_id: int) -> dict:
        """Fetch a work item's fields (System.Title, System.Description, etc.)."""
        self._ensure()
        url = (
            f"https://dev.azure.com/{self.org}/{quote(self.project)}"
            f"/_apis/wit/workitems/{work_item_id}?api-version={_API_VERSION}"
        )
        with httpx.Client(timeout=30) as client:
            resp = client.get(url, auth=("", self.pat))
        if resp.status_code >= 400:
            raise RuntimeError(f"Azure get failed {resp.status_code}: {resp.text[:300]}")
        return resp.json().get("fields", {}) or {}

    def update_work_item(self, work_item_id: int, fields: dict[str, str]) -> WorkItem:
        """PATCH fields on an existing work item. `fields` keys are Azure field refs,
        e.g. {'System.Title': '...', 'System.AssignedTo': 'a@b.com'}."""
        self._ensure()
        url = (
            f"https://dev.azure.com/{self.org}/{quote(self.project)}"
            f"/_apis/wit/workitems/{work_item_id}?api-version={_API_VERSION}"
        )
        patch = [{"op": "add", "path": f"/fields/{k}", "value": v} for k, v in fields.items()]
        with httpx.Client(timeout=30) as client:
            resp = client.patch(
                url,
                headers={"Content-Type": "application/json-patch+json"},
                auth=("", self.pat),
                json=patch,
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"Azure update failed {resp.status_code}: {resp.text[:400]}")
        return self._to_work_item(resp.json())

    def delete_work_item(self, work_item_id: int) -> int:
        """Delete a work item (moves it to the Azure recycle bin; recoverable)."""
        self._ensure()
        url = (
            f"https://dev.azure.com/{self.org}/{quote(self.project)}"
            f"/_apis/wit/workitems/{work_item_id}?api-version={_API_VERSION}"
        )
        with httpx.Client(timeout=30) as client:
            resp = client.delete(url, auth=("", self.pat))
        if resp.status_code >= 400:
            raise RuntimeError(f"Azure delete failed {resp.status_code}: {resp.text[:300]}")
        return work_item_id

    def _to_work_item(self, data: dict, fallback_title: str = "", fallback_type: str = "") -> WorkItem:
        f = data.get("fields", {}) or {}
        human_url = (
            (data.get("_links", {}).get("html", {}) or {}).get("href")
            or f"https://dev.azure.com/{self.org}/{quote(self.project)}/_workitems/edit/{data['id']}"
        )
        return WorkItem(
            id=int(data["id"]),
            url=human_url,
            title=f.get("System.Title", fallback_title),
            type=f.get("System.WorkItemType", fallback_type),
        )
