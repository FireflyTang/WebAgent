from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, str]:
    """Unauthenticated liveness endpoint; later milestones may enrich checks."""
    checker = getattr(request.app.state, "health_check", None)
    if checker is not None:
        return await checker()
    return {"status": "ok"}
