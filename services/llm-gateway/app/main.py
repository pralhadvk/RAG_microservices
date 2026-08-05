"""LLM gateway: provider-agnostic completions proxy with tracing and retries."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

import anthropic
from fastapi import FastAPI, HTTPException
from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from python_common.schemas import CompletionRequest, CompletionResponse, HealthResponse
from python_common.telemetry import (
    configure_logging,
    get_meter,
    get_tracer,
    set_span_attributes,
    setup_telemetry,
)

SERVICE_NAME = "llm-gateway"
configure_logging(SERVICE_NAME)
logger = logging.getLogger(SERVICE_NAME)

DEFAULT_PROVIDER = os.getenv("DEFAULT_LLM_PROVIDER", "openai")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "").strip() or None
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

openai_client: AsyncOpenAI | None = None
anthropic_client: anthropic.AsyncAnthropic | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global openai_client, anthropic_client, token_counter, error_counter
    setup_telemetry(SERVICE_NAME, app)
    meter = get_meter(SERVICE_NAME)
    token_counter = meter.create_counter(
        "llm_tokens_total",
        description="LLM token usage by type",
    )
    error_counter = meter.create_counter(
        "llm_request_errors_total",
        description="LLM request errors by provider/model",
    )
    openai_client = AsyncOpenAI(
        api_key=OPENAI_API_KEY or "missing",
        base_url=OPENAI_BASE_URL,
    )
    anthropic_client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY or "missing")
    logger.info("LLM client base_url=%s", OPENAI_BASE_URL or "https://api.openai.com/v1")
    yield
    openai_client = None
    anthropic_client = None


app = FastAPI(title=SERVICE_NAME, lifespan=lifespan)
token_counter = None
error_counter = None


def _tracer():
    return get_tracer(SERVICE_NAME)


@app.get("/health/live", response_model=HealthResponse)
async def live() -> HealthResponse:
    return HealthResponse(status="ok", service=SERVICE_NAME)


@app.get("/health/ready", response_model=HealthResponse)
async def ready() -> HealthResponse:
    if not OPENAI_API_KEY and not ANTHROPIC_API_KEY:
        raise HTTPException(status_code=503, detail="No LLM API keys configured")
    return HealthResponse(status="ok", service=SERVICE_NAME)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=4))
async def _openai_complete(req: CompletionRequest) -> CompletionResponse:
    assert openai_client is not None
    resp = await openai_client.chat.completions.create(
        model=req.model,
        messages=[{"role": m.role, "content": m.content} for m in req.messages],
        temperature=req.temperature,
        max_tokens=req.max_tokens,
    )
    usage = {
        "prompt_tokens": resp.usage.prompt_tokens if resp.usage else 0,
        "completion_tokens": resp.usage.completion_tokens if resp.usage else 0,
        "total_tokens": resp.usage.total_tokens if resp.usage else 0,
    }
    content = resp.choices[0].message.content or ""
    return CompletionResponse(
        content=content,
        model=resp.model or req.model,
        provider="openai",
        usage=usage,
    )


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=4))
async def _anthropic_complete(req: CompletionRequest) -> CompletionResponse:
    assert anthropic_client is not None
    system = "\n".join(m.content for m in req.messages if m.role == "system")
    messages = [
        {"role": m.role, "content": m.content}
        for m in req.messages
        if m.role in ("user", "assistant")
    ]
    if not messages:
        raise HTTPException(status_code=400, detail="No user/assistant messages")

    model = req.model if req.model.startswith("claude") else "claude-3-5-haiku-latest"
    resp = await anthropic_client.messages.create(
        model=model,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        system=system or "You are a helpful assistant.",
        messages=messages,
    )
    content = "".join(block.text for block in resp.content if hasattr(block, "text"))
    usage = {
        "prompt_tokens": resp.usage.input_tokens,
        "completion_tokens": resp.usage.output_tokens,
        "total_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
    }
    return CompletionResponse(
        content=content,
        model=resp.model,
        provider="anthropic",
        usage=usage,
    )


@app.post("/completions", response_model=CompletionResponse)
async def completions(req: CompletionRequest) -> CompletionResponse:
    provider = (req.provider or DEFAULT_PROVIDER).lower()
    with _tracer().start_as_current_span("llm.completions") as span:
        set_span_attributes(
            {
                "rag.model": req.model,
                "rag.temperature": req.temperature,
                "rag.max_tokens": req.max_tokens,
                "rag.provider": provider,
                "rag.message_count": len(req.messages),
            }
        )
        try:
            if provider == "openai":
                if not OPENAI_API_KEY:
                    raise HTTPException(status_code=503, detail="OPENAI_API_KEY not set")
                result = await _openai_complete(req)
            elif provider == "anthropic":
                if not ANTHROPIC_API_KEY:
                    raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")
                result = await _anthropic_complete(req)
            else:
                raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
        except HTTPException:
            error_counter.add(1, {"provider": provider, "model": req.model})
            raise
        except Exception as exc:  # noqa: BLE001
            error_counter.add(1, {"provider": provider, "model": req.model})
            span.record_exception(exc)
            logger.exception("LLM call failed provider=%s model=%s", provider, req.model)
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        for kind, value in result.usage.items():
            token_counter.add(value, {"provider": provider, "model": result.model, "type": kind})
        span.set_attribute("rag.prompt_tokens", result.usage.get("prompt_tokens", 0))
        span.set_attribute("rag.completion_tokens", result.usage.get("completion_tokens", 0))
        span.set_attribute("rag.total_tokens", result.usage.get("total_tokens", 0))
        logger.info(
            "completion provider=%s model=%s tokens=%s",
            provider,
            result.model,
            result.usage.get("total_tokens", 0),
        )
        return result
