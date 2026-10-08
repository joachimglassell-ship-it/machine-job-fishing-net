import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from openai import OpenAI
from pydantic import BaseModel, Field
from cdp import CdpClient

from x402.http import FacilitatorConfig, HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer


PAY_TO_ADDRESS = "0x3b0946177F281eF9C7CcEE152ec1A7F41Cc5A468"
NETWORK = "eip155:84532"
PRICE = "$0.01"
FACILITATOR_URL = "https://x402.org/facilitator"


app = FastAPI(
    title="Machine Job Fishing Net",
    version="0.3.0",
    description="Experimental machine-callable jobs."
)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


# x402 payment infrastructure
facilitator = HTTPFacilitatorClient(
    FacilitatorConfig(url=FACILITATOR_URL)
)

payment_server = x402ResourceServer(facilitator)
payment_server.register(
    NETWORK,
    ExactEvmServerScheme()
)

payment_routes = {
    "POST /v1/find-official-source": RouteConfig(
        accepts=[
            PaymentOption(
                scheme="exact",
                price=PRICE,
                network=NETWORK,
                pay_to=PAY_TO_ADDRESS,
            )
        ],
        description=(
            "Find the best available primary or official source for an "
            "entity, claim, document, or topic."
        ),
    )
}

app.add_middleware(
    PaymentMiddlewareASGI,
    routes=payment_routes,
    server=payment_server
)


class SourceRequest(BaseModel):
    query: str = Field(
        min_length=3,
        max_length=1000,
        description=(
            "Entity, claim, document or topic for which an official "
            "source is requested."
        )
    )


class OfficialSource(BaseModel):
    title: str
    url: str
    publisher: str
    source_type: Literal[
        "company",
        "government",
        "regulator",
        "court",
        "official_organization",
        "other_official"
    ]


class SourceResponse(BaseModel):
    status: Literal["found", "not_found"]
    query: str
    official_source: Optional[OfficialSource] = None
    source_date: Optional[str] = None
    relevant_evidence: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)
    checked_at: str
    request_id: str


@app.get("/")
def root():
    return {
        "service": "Machine Job Fishing Net",
        "version": "0.3.0",
        "jobs": ["find_official_source"],
        "payment": {
            "protocol": "x402",
            "price": PRICE,
            "network": NETWORK
        }
    }


@app.get("/health")
def health():
    return {"status": "ok"}


def write_call_log(
    request_id: str,
    request: Request,
    success: bool,
    result_status: str,
    latency_ms: int
):
    log_event = {
        "event": "machine_job_call",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "job": "find_official_source",
        "request_id": request_id,
        "success": success,
        "result_status": result_status,
        "latency_ms": latency_ms,
        "client": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent")
    }

    print(json.dumps(log_event), flush=True)


@app.post("/v1/find-official-source", response_model=SourceResponse)
def find_official_source(payload: SourceRequest, request: Request):

    request_id = str(uuid.uuid4())
    started = time.perf_counter()

    instructions = """
You are performing a provenance job for another machine.

Find the best PRIMARY or OFFICIAL source that directly addresses the user's query.

Prefer, in this order where appropriate:
1. Official company or issuer websites and investor-relations pages.
2. Government agencies and public authorities.
3. Regulators.
4. Courts and official legal repositories.
5. Official organizations responsible for the information.

Do NOT use Wikipedia, newspapers, blogs, aggregators, social media,
SEO pages, or other secondary sources as the final source.

CRITICAL RULES:
- Never invent or reconstruct a URL.
- Only return a URL supported by your web search.
- If no sufficiently reliable official source can be found, return status "not_found".
- relevant_evidence must briefly state what in the source makes it relevant.
- confidence must represent confidence that this is both an official source
  and directly relevant to the query.

Return ONLY valid JSON using exactly this structure:

{
  "status": "found" or "not_found",
  "official_source": {
    "title": "...",
    "url": "...",
    "publisher": "...",
    "source_type": "company|government|regulator|court|official_organization|other_official"
  } or null,
  "source_date": "YYYY-MM-DD or null",
  "relevant_evidence": "brief evidence or null",
  "confidence": 0.0
}
"""

    try:
        response = client.responses.create(
            model="gpt-5-mini",
            tools=[{"type": "web_search"}],
            instructions=instructions,
            input=payload.query
        )

        result = json.loads(response.output_text)

        latency_ms = round((time.perf_counter() - started) * 1000)

        write_call_log(
            request_id=request_id,
            request=request,
            success=True,
            result_status=result["status"],
            latency_ms=latency_ms
        )

        return SourceResponse(
            status=result["status"],
            query=payload.query,
            official_source=result.get("official_source"),
            source_date=result.get("source_date"),
            relevant_evidence=result.get("relevant_evidence"),
            confidence=result.get("confidence", 0.0),
            checked_at=datetime.now(timezone.utc).isoformat(),
            request_id=request_id
        )

    except json.JSONDecodeError:
        latency_ms = round((time.perf_counter() - started) * 1000)

        write_call_log(
            request_id=request_id,
            request=request,
            success=False,
            result_status="invalid_output",
            latency_ms=latency_ms
        )

        raise HTTPException(
            status_code=502,
            detail="Search completed but returned invalid structured output."
        )

    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000)

        write_call_log(
            request_id=request_id,
            request=request,
            success=False,
            result_status=type(exc).__name__,
            latency_ms=latency_ms
        )

        raise HTTPException(
            status_code=500,
            detail=f"Job failed: {type(exc).__name__}"
        )


@app.get("/.well-known/agent.json")
def agent_discovery():
    return {
        "name": "Machine Job Fishing Net",
        "version": "0.3.0",
        "description": "Experimental machine-callable jobs.",
        "jobs": [
            {
                "name": "find_official_source",
                "description": (
                    "Find the best available primary or official source "
                    "for an entity, claim, document, or topic."
                ),
                "method": "POST",
                "endpoint": (
                    "https://machine-job-fishing-net.onrender.com/"
                    "v1/find-official-source"
                ),
                "payment": {
                    "protocol": "x402",
                    "scheme": "exact",
                    "price": PRICE,
                    "network": NETWORK,
                    "environment": "testnet"
                },
                "input": {
                    "query": "string"
                },
                "output": {
                    "status": "found | not_found",
                    "query": "string",
                    "official_source": "object | null",
                    "source_date": "string | null",
                    "relevant_evidence": "string",
                    "confidence": "number",
                    "checked_at": "string",
                    "request_id": "string"
                }
            }
        ],
        "openapi": (
            "https://machine-job-fishing-net.onrender.com/openapi.json"
        ),
        "documentation": (
            "https://machine-job-fishing-net.onrender.com/docs"
        )
    }


@app.get("/internal/wallet-test")
async def wallet_test():
    async with CdpClient() as cdp:
        account = await cdp.evm.get_or_create_account(
            name="machine-job-receiver"
        )

        return {
            "status": "ok",
            "address": account.address
        } 
        
@app.get("/internal/buyer-wallet-test")
async def buyer_wallet_test():
    async with CdpClient() as cdp:
        account = await cdp.evm.get_or_create_account(
            name="machine-job-buyer"
        )

        return {
            "status": "ok",
            "address": account.address
        }
