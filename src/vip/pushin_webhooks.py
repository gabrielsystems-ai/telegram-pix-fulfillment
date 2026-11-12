"""Authenticate the configured custom header before accepting durable signals."""

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool


def build_pushin_webhook_router(payments):
    router = APIRouter()

    @router.post("/webhooks/pushinpay")
    async def receive(request: Request):
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536:
                raise HTTPException(status_code=413, detail="webhook_too_large")
            chunks.append(chunk)
        return await run_in_threadpool(
            payments.process_webhook, b"".join(chunks), request.headers.get("X-PushinPay-Token")
        )

    return router
