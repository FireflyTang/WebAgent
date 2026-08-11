from fastapi import APIRouter, Depends

from app.api.dependencies import require_api_key
from app.openai_compat.schemas import ModelCard, ModelListResponse

router = APIRouter(prefix="/v1", tags=["openai"])


@router.get("/models", response_model=ModelListResponse, dependencies=[Depends(require_api_key)])
async def list_models() -> ModelListResponse:
    return ModelListResponse(data=[ModelCard(id="claude-code-agent")])
