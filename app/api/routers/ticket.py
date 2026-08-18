"""Azure Boards ticket endpoints: draft (LLM) and create (Azure DevOps)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.auth import require_api_key
from app.connectors.azure.boards import AzureBoardsClient, AzureNotConfigured
from app.rag.ticket_drafter import draft_ticket

router = APIRouter(prefix="/ticket", tags=["ticket"], dependencies=[Depends(require_api_key)])


class DraftRequest(BaseModel):
    problem: str = Field(min_length=1)
    work_item_type: str | None = None


class CreateRequest(BaseModel):
    title: str = Field(min_length=1)
    description_html: str = ""
    work_item_type: str | None = None
    tags: list[str] = Field(default_factory=list)
    assigned_to: str | None = None


@router.post("/draft")
def draft(req: DraftRequest) -> dict:
    """LLM-draft a work item (title, description, tags) from a problem statement."""
    return draft_ticket(req.problem.strip(), req.work_item_type)


@router.post("/create")
def create(req: CreateRequest) -> dict:
    """Create the work item on Azure DevOps and return its id + link."""
    try:
        wi = AzureBoardsClient().create_work_item(
            title=req.title,
            description_html=req.description_html,
            work_item_type=req.work_item_type,
            tags=req.tags,
            assigned_to=req.assigned_to,
        )
    except AzureNotConfigured as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Azure error: {e}") from e
    return {"id": wi.id, "url": wi.url, "title": wi.title, "type": wi.type}
