from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from vip.payments.operations import OmegaPayments


def build_webhook_router(payments: OmegaPayments) -> APIRouter:
    router = APIRouter()

    @router.post("/webhooks/omega")
    async def omega_webhook(request: Request):
        payments.enabled()
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 256_000:
                raise HTTPException(status_code=413, detail="webhook_too_large")
        return await run_in_threadpool(payments.process_webhook, bytes(data))

    return router
