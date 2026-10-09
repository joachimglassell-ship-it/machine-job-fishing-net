import asyncio
import json
import os
import secrets
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from openai import OpenAI
from pydantic import BaseModel, Field
from cdp import CdpClient
from cdp.x402 import create_facilitator_config

from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
from x402.http import HTTPFacilitatorClient, PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer


PAY_TO_ADDRESS = "0x3b0946177F281eF9C7CcEE152ec1A7F41Cc5A468"
NETWORK = "eip155:84532"
PRICE = "$0.01"
PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL",
    "https://machine-job-fishing-net.onrender.com",
).rstrip("/")
A2A_MAX_CONCURRENT_JOBS = max(
    1,
    int(os.environ.get("A2A_MAX_CONCURRENT_JOBS", "1")),
)
A2A_RATE_LIMIT_PER_HOUR = max(
    1,
    int(os.environ.get("A2A_RATE_LIMIT_PER_HOUR", "3")),
)
A2A_INTERNAL_TEST_TOKEN = os.environ.get("A2A_INTERNAL_TEST_TOKEN", "")

FEED_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "feeds",
    "tsre_daily.json",
)


def load_tsre_daily() -> dict[str, Any]:
    with open(FEED_PATH, "r", encoding="utf-8") as feed_file:
        return json.load(feed_file)


# ---------------------------------------------------------------------------
# OPENAI CLIENT
# ---------------------------------------------------------------------------

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


# ---------------------------------------------------------------------------
# TSRE MCP SERVER
# ---------------------------------------------------------------------------

mcp = MCPServer(
    name="machine-job-fishing-net",
    title="TSRE Swedish Equity Intelligence",
    description=(
        "Machine-readable daily research intelligence for Swedish equities. "
        "Research decision support; not autonomous investment advice or order generation."
    ),
    instructions=(
        "Use the single tool to retrieve the latest published TSRE Daily Investment Desk. "
        "Preserve the included research contract when interpreting the data."
    ),
    website_url="https://machine-job-fishing-net.onrender.com",
    version="0.8.0",
)


@mcp.tool(
    name="get_sweden_market_intelligence",
    description=(
        "Retrieve the latest machine-readable TSRE Daily Investment Desk for "
        "Swedish equities, including market regime, breadth, sector rotation, "
        "RS20 candidates and Tactical Radar. Research decision support; not an "
        "autonomous buy/sell signal."
    ),
    structured_output=True,
)
def get_sweden_market_intelligence(ctx: Context) -> dict[str, Any]:
    request_id = ctx.request_id
    started = time.perf_counter()
    headers = ctx.headers or {}

    try:
        payload = load_tsre_daily()

        log_event = {
            "event": "mcp_tool_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": "get_sweden_market_intelligence",
            "request_id": request_id,
            "success": True,
            "observation_date": payload.get("date"),
            "schema_version": payload.get("schema_version"),
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "user_agent": headers.get("user-agent"),
            "protocol_version": ctx.protocol_version,
        }

        print(json.dumps(log_event), flush=True)
        return payload

    except Exception as exc:
        log_event = {
            "event": "mcp_tool_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": "get_sweden_market_intelligence",
            "request_id": request_id,
            "success": False,
            "result_status": type(exc).__name__,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "user_agent": headers.get("user-agent"),
            "protocol_version": ctx.protocol_version,
        }

        print(json.dumps(log_event), flush=True)
        raise


# ---------------------------------------------------------------------------
# COMPANY DELTA ANALYSIS ENGINE
# ---------------------------------------------------------------------------

def run_company_delta(
    company: str,
    since: str,
) -> dict[str, Any]:

    checked_at = datetime.now(timezone.utc)
    today = checked_at.date().isoformat()

    try:
        baseline_date = datetime.strptime(
            since,
            "%Y-%m-%d",
        ).date()
    except ValueError as exc:
        raise ValueError(
            "since must use ISO format YYYY-MM-DD."
        ) from exc

    if baseline_date > checked_at.date():
        raise ValueError("since cannot be in the future.")

    instructions = f"""
You are a conservative company change-detection engine working for another machine.

COMPANY:
{company}

BASELINE DATE:
{since}

CURRENT DATE:
{today}

Your task is NOT to summarize the company and NOT to list recent news.

Your task is to identify MATERIAL CHANGES between the company's state at
the baseline date and its subsequent state up to the current date.

A publication after the baseline date is NOT automatically a change.

For every reported change:

1. Establish the prior state at or reasonably close to the baseline date.
2. Establish the subsequent state.
3. Explain precisely what changed.
4. Assess whether the change is materially relevant to an external
   decision-maker.
5. Prefer primary-source evidence for both states.

PRIMARY SOURCE PRIORITY:
1. Official company filings, reports and investor-relations material.
2. Stock-exchange or regulatory disclosures.
3. Government agencies, regulators and courts.
4. Other authoritative primary sources.

Secondary sources may help discovery, but should not be used as substitutes
when primary evidence is available.

MATERIALITY:

HIGH:
A change likely to alter a rational external decision-maker's view of the
company in a significant way.

MEDIUM:
A genuine and potentially decision-relevant change that does not by itself
materially alter the overall company picture.

Do not return LOW-materiality items.

ALLOWED CATEGORIES:
guidance
financial_performance
financial_targets
capital_allocation
management
strategy
operations
products
markets
major_contracts
m_and_a
financing
legal_regulatory
ownership
other_material

CRITICAL RULES:

- Do not invent a prior state.
- Do not invent dates, facts, values, sources or URLs.
- Only return URLs supported by web search.
- Do not classify ordinary news as a delta merely because it is new.
- Prefer omission over a weak or speculative delta.
- If you identify potentially material new information but cannot establish
  the prior state, set comparison_status to "baseline_not_established".
- Such an item must NOT be described as a verified change.
- If no material changes can be established, return an empty changes array.
- This is research intelligence, not investment advice.

Return ONLY valid JSON using exactly this structure:

{{
  "status": "ok" or "not_found",
  "company": "{company}",
  "changes": [
    {{
      "category": "one allowed category",
      "materiality": "high|medium",
      "summary": "concise description",
      "before": "verified prior state or null",
      "after": "verified subsequent state",
      "effective_date": "YYYY-MM-DD or null",
      "comparison_status": "verified_change|baseline_not_established",
      "evidence": "brief explanation of the comparison and evidence",
      "sources": [
        {{
          "title": "...",
          "publisher": "...",
          "date": "YYYY-MM-DD or null",
          "url": "..."
        }}
      ],
      "confidence": 0.0
    }}
  ]
}}
"""

    response = client.responses.create(
        model="gpt-5-mini",
        tools=[{"type": "web_search"}],
        instructions=instructions,
        input=(
            f"Detect material changes at {company} "
            f"since {since}."
        ),
    )

    result = json.loads(response.output_text)

    if result.get("status") not in {"ok", "not_found"}:
        raise ValueError("Invalid Company Delta status.")

    raw_changes = result.get("changes", [])

    if not isinstance(raw_changes, list):
        raise ValueError("Company Delta changes must be a list.")

    verified_count = sum(
        1
        for change in raw_changes
        if change.get("comparison_status") == "verified_change"
    )

    return {
        "status": result["status"],
        "company": result.get("company", company),
        "period_from": since,
        "period_to": today,
        "changes": raw_changes,
        "material_changes_found": verified_count,
        "checked_at": checked_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# COMPANY DELTA MCP SERVER
# ---------------------------------------------------------------------------

company_delta_mcp = MCPServer(
    name="company-delta",
    title="Company Delta",
    description=(
        "Machine-readable detection of material changes in public companies "
        "between a baseline date and the current date, using primarily "
        "official and other primary sources."
    ),
    instructions=(
        "Use get_company_delta when you need to determine what materially "
        "changed at a public company since a specified date. The tool returns "
        "structured before-and-after changes with source provenance. "
        "Do not treat the output as investment advice."
    ),
    website_url="https://machine-job-fishing-net.onrender.com",
    version="1.0.0",
)


@company_delta_mcp.tool(
    name="get_company_delta",
    description=(
        "Identify material changes in a public company between a baseline "
        "date and the current date. Returns structured before-and-after "
        "changes with evidence and source provenance. Research intelligence only."
    ),
    structured_output=True,
)
def get_company_delta(
    company: str,
    since: str,
    ctx: Context,
) -> dict[str, Any]:

    request_id = ctx.request_id
    started = time.perf_counter()
    headers = ctx.headers or {}

    try:
        result = run_company_delta(
            company=company,
            since=since,
        )

        result["request_id"] = str(request_id)

        log_event = {
            "event": "company_delta_mcp_tool_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": "get_company_delta",
            "request_id": request_id,
            "company": company,
            "since": since,
            "success": True,
            "result_status": result.get("status"),
            "changes_returned": len(result.get("changes", [])),
            "verified_changes": result.get("material_changes_found", 0),
            "latency_ms": round(
                (time.perf_counter() - started) * 1000
            ),
            "user_agent": headers.get("user-agent"),
            "protocol_version": ctx.protocol_version,
        }

        print(json.dumps(log_event), flush=True)

        return result

    except Exception as exc:
        log_event = {
            "event": "company_delta_mcp_tool_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tool": "get_company_delta",
            "request_id": request_id,
            "company": company,
            "since": since,
            "success": False,
            "result_status": type(exc).__name__,
            "latency_ms": round(
                (time.perf_counter() - started) * 1000
            ),
            "user_agent": headers.get("user-agent"),
            "protocol_version": ctx.protocol_version,
        }

        print(json.dumps(log_event), flush=True)
        raise


# ---------------------------------------------------------------------------
# FASTAPI LIFESPAN
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    async with mcp.session_manager.run():
        async with company_delta_mcp.session_manager.run():
            yield


# ---------------------------------------------------------------------------
# FASTAPI APP
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Machine Job Fishing Net",
    version="0.9.0",
    description="Experimental machine-callable jobs.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# x402 PAYMENT INFRASTRUCTURE
# ---------------------------------------------------------------------------

facilitator = HTTPFacilitatorClient(
    create_facilitator_config()
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
        mime_type="application/json",
        description=(
            "Find the best available primary or official source for an "
            "entity, claim, document, or topic."
        ),
        extensions=declare_discovery_extension(
            input={
                "query": "Ericsson annual report 2025"
            },
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "minLength": 3,
                        "maxLength": 1000,
                        "description": (
                            "Entity, claim, document or topic for which "
                            "an official source is requested."
                        ),
                    }
                },
                "required": ["query"],
            },
            body_type="json",
            output=OutputConfig(
                example={
                    "status": "found",
                    "query": "Ericsson annual report 2025",
                    "official_source": {
                        "title": "Ericsson Annual Report 2025",
                        "url": "https://www.ericsson.com/",
                        "publisher": "Ericsson",
                        "source_type": "company",
                    },
                    "source_date": "2026-01-01",
                    "relevant_evidence": (
                        "Official company source containing the requested "
                        "annual report."
                    ),
                    "confidence": 0.95,
                    "checked_at": "2026-10-08T08:00:00+00:00",
                    "request_id": "example-request-id",
                },
                schema={
                    "type": "object",
                    "properties": {
                        "status": {
                            "type": "string",
                            "enum": ["found", "not_found"],
                        },
                        "query": {
                            "type": "string",
                        },
                        "official_source": {
                            "anyOf": [
                                {
                                    "type": "object",
                                    "properties": {
                                        "title": {"type": "string"},
                                        "url": {"type": "string"},
                                        "publisher": {"type": "string"},
                                        "source_type": {
                                            "type": "string",
                                            "enum": [
                                                "company",
                                                "government",
                                                "regulator",
                                                "court",
                                                "official_organization",
                                                "other_official",
                                            ],
                                        },
                                    },
                                    "required": [
                                        "title",
                                        "url",
                                        "publisher",
                                        "source_type",
                                    ],
                                },
                                {"type": "null"},
                            ]
                        },
                        "source_date": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                        "relevant_evidence": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "null"},
                            ]
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "checked_at": {
                            "type": "string",
                        },
                        "request_id": {
                            "type": "string",
                        },
                    },
                    "required": [
                        "status",
                        "query",
                        "confidence",
                        "checked_at",
                        "request_id",
                    ],
                },
            ),
        ),
    )
}


app.add_middleware(
    PaymentMiddlewareASGI,
    routes=payment_routes,
    server=payment_server,
)


# ---------------------------------------------------------------------------
# PYDANTIC MODELS
# ---------------------------------------------------------------------------

class SourceRequest(BaseModel):
    query: str = Field(
        min_length=3,
        max_length=1000,
        description=(
            "Entity, claim, document or topic for which an official "
            "source is requested."
        ),
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
        "other_official",
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


class CompanyDeltaRequest(BaseModel):
    company: str = Field(
        min_length=2,
        max_length=200,
        description="Public company name or ticker.",
    )
    since: str = Field(
        description="Baseline date in ISO format YYYY-MM-DD.",
    )


class DeltaSource(BaseModel):
    title: str
    publisher: str
    date: Optional[str] = None
    url: str


class CompanyChange(BaseModel):
    category: Literal[
        "guidance",
        "financial_performance",
        "financial_targets",
        "capital_allocation",
        "management",
        "strategy",
        "operations",
        "products",
        "markets",
        "major_contracts",
        "m_and_a",
        "financing",
        "legal_regulatory",
        "ownership",
        "other_material",
    ]
    materiality: Literal["high", "medium"]
    summary: str
    before: Optional[str] = None
    after: str
    effective_date: Optional[str] = None
    comparison_status: Literal[
        "verified_change",
        "baseline_not_established",
    ]
    evidence: str
    sources: list[DeltaSource]
    confidence: float = Field(ge=0.0, le=1.0)


class CompanyDeltaResponse(BaseModel):
    status: Literal["ok", "not_found"]
    company: str
    period_from: str
    period_to: str
    changes: list[CompanyChange]
    material_changes_found: int
    checked_at: str
    request_id: str


# ---------------------------------------------------------------------------
# MINIMAL A2A V1 TASK STORE
# ---------------------------------------------------------------------------

A2A_TASKS: dict[str, dict[str, Any]] = {}
A2A_MESSAGE_TASKS: dict[str, str] = {}
A2A_ACTIVE_JOBS = 0
A2A_STATE_LOCK = asyncio.Lock()
A2A_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()
A2A_REQUEST_TIMES: dict[str, deque[float]] = defaultdict(deque)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def a2a_client_metadata(request: Request) -> dict[str, Any]:
    forwarded = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded.split(",", 1)[0].strip() or (
        request.client.host if request.client else "unknown"
    )
    supplied_token = request.headers.get("x-a2a-test-token", "")
    internal = bool(
        A2A_INTERNAL_TEST_TOKEN
        and supplied_token
        and secrets.compare_digest(supplied_token, A2A_INTERNAL_TEST_TOKEN)
    )
    return {
        "client": client_ip,
        "user_agent": request.headers.get("user-agent"),
        "traffic_class": "internal_verification" if internal else "external",
    }


def a2a_log(event: str, **fields: Any) -> None:
    print(json.dumps({"event": event, "timestamp": utc_now(), **fields}), flush=True)


def a2a_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def a2a_task_view(task: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in task.items()
        if not key.startswith("_")
    }


def extract_company_delta_input(params: Any) -> tuple[str, str, str, str]:
    if not isinstance(params, dict):
        raise ValueError("params must be an object.")
    message = params.get("message")
    if not isinstance(message, dict):
        raise ValueError("params.message must be an object.")
    if message.get("role") != "ROLE_USER":
        raise ValueError("message.role must be ROLE_USER.")
    message_id = message.get("messageId")
    if not isinstance(message_id, str) or not message_id.strip():
        raise ValueError("message.messageId is required.")
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise ValueError("message.parts must contain structured input.")

    payload = next(
        (part.get("data") for part in parts if isinstance(part, dict) and isinstance(part.get("data"), dict)),
        None,
    )
    if payload is None:
        raise ValueError('Use a data part containing {"company": "...", "since": "YYYY-MM-DD"}.')
    company = payload.get("company")
    since = payload.get("since")
    if not isinstance(company, str) or not 2 <= len(company.strip()) <= 200:
        raise ValueError("company must be a string of 2-200 characters.")
    if not isinstance(since, str):
        raise ValueError("since must use ISO format YYYY-MM-DD.")
    try:
        baseline = datetime.strptime(since, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("since must use ISO format YYYY-MM-DD.") from exc
    if baseline > datetime.now(timezone.utc).date():
        raise ValueError("since cannot be in the future.")
    context_id = message.get("contextId") or str(uuid.uuid4())
    return company.strip(), since, message_id.strip(), str(context_id)


async def run_a2a_company_delta(task_id: str) -> None:
    global A2A_ACTIVE_JOBS
    task = A2A_TASKS[task_id]
    started = time.perf_counter()
    task["status"] = {"state": "TASK_STATE_WORKING", "timestamp": utc_now()}
    a2a_log(
        "a2a_job_started",
        task_id=task_id,
        message_id=task["_message_id"],
        request_id=task["_request_id"],
        company=task["_company"],
        since=task["_since"],
        status="TASK_STATE_WORKING",
        **task["_client_metadata"],
    )
    try:
        result = await asyncio.to_thread(
            run_company_delta,
            task["_company"],
            task["_since"],
        )
        result["request_id"] = task["_request_id"]
        task["artifacts"] = [{
            "artifactId": str(uuid.uuid4()),
            "name": "company_delta_result",
            "description": "Structured Company Delta analysis result.",
            "parts": [{"data": result, "mediaType": "application/json"}],
        }]
        task["status"] = {"state": "TASK_STATE_COMPLETED", "timestamp": utc_now()}
        a2a_log(
            "a2a_job_completed",
            task_id=task_id,
            message_id=task["_message_id"],
            request_id=task["_request_id"],
            company=task["_company"],
            since=task["_since"],
            status="TASK_STATE_COMPLETED",
            material_changes_found=result.get("material_changes_found", 0),
            latency_ms=round((time.perf_counter() - started) * 1000),
            **task["_client_metadata"],
        )
    except Exception as exc:
        task["status"] = {
            "state": "TASK_STATE_FAILED",
            "timestamp": utc_now(),
            "message": {
                "messageId": str(uuid.uuid4()),
                "taskId": task_id,
                "contextId": task["contextId"],
                "role": "ROLE_AGENT",
                "parts": [{"text": "Company Delta analysis failed."}],
            },
        }
        a2a_log(
            "a2a_job_failed",
            task_id=task_id,
            message_id=task["_message_id"],
            request_id=task["_request_id"],
            company=task["_company"],
            since=task["_since"],
            status="TASK_STATE_FAILED",
            error_type=type(exc).__name__,
            latency_ms=round((time.perf_counter() - started) * 1000),
            **task["_client_metadata"],
        )
    finally:
        async with A2A_STATE_LOCK:
            A2A_ACTIVE_JOBS -= 1


# ---------------------------------------------------------------------------
# BASIC ROUTES
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {
        "service": "Machine Job Fishing Net",
        "version": "0.9.0",
        "jobs": [
            "find_official_source",
            "tsre_daily_investment_desk",
            "company_delta",
        ],
        "mcp_servers": [
            {
                "name": "TSRE Swedish Equity Intelligence",
                "transport": "streamable-http",
                "endpoint": (
                    "https://machine-job-fishing-net.onrender.com/mcp/"
                ),
                "tools": [
                    "get_sweden_market_intelligence"
                ],
            },
            {
                "name": "Company Delta",
                "transport": "streamable-http",
                "endpoint": (
                    "https://machine-job-fishing-net.onrender.com/"
                    "company-delta/mcp/"
                ),
                "tools": [
                    "get_company_delta"
                ],
            },
        ],
        "payment": {
            "protocol": "x402",
            "price": PRICE,
            "network": NETWORK,
        },
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# TSRE REST ENDPOINT
# ---------------------------------------------------------------------------

@app.get("/v1/tsre/daily")
def tsre_daily(request: Request):
    request_id = str(uuid.uuid4())
    started = time.perf_counter()

    try:
        payload = load_tsre_daily()

        latency_ms = round(
            (time.perf_counter() - started) * 1000
        )

        log_event = {
            "event": "tsre_daily_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job": "tsre_daily_investment_desk",
            "request_id": request_id,
            "success": True,
            "observation_date": payload.get("date"),
            "schema_version": payload.get("schema_version"),
            "latency_ms": latency_ms,
            "client": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        }

        print(json.dumps(log_event), flush=True)
        return payload

    except Exception as exc:
        latency_ms = round(
            (time.perf_counter() - started) * 1000
        )

        log_event = {
            "event": "tsre_daily_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job": "tsre_daily_investment_desk",
            "request_id": request_id,
            "success": False,
            "result_status": type(exc).__name__,
            "latency_ms": latency_ms,
            "client": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
        }

        print(json.dumps(log_event), flush=True)

        raise HTTPException(
            status_code=502,
            detail="Current TSRE Daily Investment Desk is unavailable.",
        )


# ---------------------------------------------------------------------------
# OFFICIAL SOURCE FINDER
# ---------------------------------------------------------------------------

def write_call_log(
    request_id: str,
    request: Request,
    success: bool,
    result_status: str,
    latency_ms: int,
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
        "user_agent": request.headers.get("user-agent"),
    }

    print(json.dumps(log_event), flush=True)


@app.post("/v1/find-official-source", response_model=SourceResponse)
def find_official_source(
    payload: SourceRequest,
    request: Request,
):
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
            input=payload.query,
        )

        result = json.loads(response.output_text)

        latency_ms = round(
            (time.perf_counter() - started) * 1000
        )

        write_call_log(
            request_id=request_id,
            request=request,
            success=True,
            result_status=result["status"],
            latency_ms=latency_ms,
        )

        return SourceResponse(
            status=result["status"],
            query=payload.query,
            official_source=result.get("official_source"),
            source_date=result.get("source_date"),
            relevant_evidence=result.get("relevant_evidence"),
            confidence=result.get("confidence", 0.0),
            checked_at=datetime.now(timezone.utc).isoformat(),
            request_id=request_id,
        )

    except json.JSONDecodeError:
        latency_ms = round(
            (time.perf_counter() - started) * 1000
        )

        write_call_log(
            request_id=request_id,
            request=request,
            success=False,
            result_status="invalid_output",
            latency_ms=latency_ms,
        )

        raise HTTPException(
            status_code=502,
            detail="Search completed but returned invalid structured output.",
        )

    except Exception as exc:
        latency_ms = round(
            (time.perf_counter() - started) * 1000
        )

        write_call_log(
            request_id=request_id,
            request=request,
            success=False,
            result_status=type(exc).__name__,
            latency_ms=latency_ms,
        )

        raise HTTPException(
            status_code=500,
            detail=f"Job failed: {type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# COMPANY DELTA REST ENDPOINT
# ---------------------------------------------------------------------------

@app.post(
    "/v1/company-delta",
    response_model=CompanyDeltaResponse,
)
def company_delta(
    payload: CompanyDeltaRequest,
    request: Request,
):
    request_id = str(uuid.uuid4())
    started = time.perf_counter()

    try:
        result = run_company_delta(
            company=payload.company,
            since=payload.since,
        )

        result["request_id"] = request_id

        latency_ms = round(
            (time.perf_counter() - started) * 1000
        )

        log_event = {
            "event": "company_delta_call",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "job": "company_delta",
            "request_id": request_id,
            "company": payload.company,
            "since": payload.since,
            "success": True,
            "result_status": result["status"],
            "changes_returned": len(result.get("changes", [])),
            "verified_changes": result.get(
                "material_changes_found",
                0,
            ),
            "latency_ms": latency_ms,
            "client": (
                request.client.host
                if request.client
                else None
            ),
            "user_agent": request.headers.get("user-agent"),
        }

        print(json.dumps(log_event), flush=True)

        return CompanyDeltaResponse(**result)

    except ValueError as exc:
        latency_ms = round(
            (time.perf_counter() - started) * 1000
        )

        print(
            json.dumps({
                "event": "company_delta_call",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "job": "company_delta",
                "request_id": request_id,
                "company": payload.company,
                "since": payload.since,
                "success": False,
                "result_status": "invalid_request",
                "latency_ms": latency_ms,
                "client": (
                    request.client.host
                    if request.client
                    else None
                ),
                "user_agent": request.headers.get("user-agent"),
            }),
            flush=True,
        )

        raise HTTPException(
            status_code=422,
            detail=str(exc),
        )

    except json.JSONDecodeError:
        latency_ms = round(
            (time.perf_counter() - started) * 1000
        )

        print(
            json.dumps({
                "event": "company_delta_call",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "job": "company_delta",
                "request_id": request_id,
                "company": payload.company,
                "since": payload.since,
                "success": False,
                "result_status": "invalid_output",
                "latency_ms": latency_ms,
                "client": (
                    request.client.host
                    if request.client
                    else None
                ),
                "user_agent": request.headers.get("user-agent"),
            }),
            flush=True,
        )

        raise HTTPException(
            status_code=502,
            detail="Company Delta returned invalid structured output.",
        )

    except Exception as exc:
        latency_ms = round(
            (time.perf_counter() - started) * 1000
        )

        print(
            json.dumps({
                "event": "company_delta_call",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "job": "company_delta",
                "request_id": request_id,
                "company": payload.company,
                "since": payload.since,
                "success": False,
                "result_status": type(exc).__name__,
                "latency_ms": latency_ms,
                "client": (
                    request.client.host
                    if request.client
                    else None
                ),
                "user_agent": request.headers.get("user-agent"),
            }),
            flush=True,
        )

        raise HTTPException(
            status_code=500,
            detail=f"Company Delta failed: {type(exc).__name__}",
        )


# ---------------------------------------------------------------------------
# AGENT DISCOVERY
# ---------------------------------------------------------------------------

@app.get("/.well-known/agent.json")
def agent_discovery():
    return {
        "name": "Machine Job Fishing Net",
        "version": "0.9.0",
        "description": "Experimental machine-callable jobs.",
        "jobs": [
            {
                "name": "tsre_daily_investment_desk",
                "description": (
                    "Free daily machine-readable research intelligence for "
                    "Swedish equities, including market regime, breadth, "
                    "sector rotation, RS20 candidates and Tactical Radar. "
                    "Research decision support; not an autonomous buy/sell signal."
                ),
                "method": "GET",
                "endpoint": (
                    "https://machine-job-fishing-net.onrender.com/"
                    "v1/tsre/daily"
                ),
                "payment": None,
                "input": None,
                "output": {
                    "schema_version": "daily_investment_desk_v1.1",
                    "producer": "TSRE Daily Investment Desk",
                    "format": "application/json",
                    "research_use": True,
                },
            },
            {
                "name": "company_delta",
                "description": (
                    "Detect material changes in a public company between "
                    "a baseline date and the current date using primarily "
                    "official and other primary sources."
                ),
                "method": "POST",
                "endpoint": (
                    "https://machine-job-fishing-net.onrender.com/"
                    "v1/company-delta"
                ),
                "mcp_endpoint": (
                    "https://machine-job-fishing-net.onrender.com/"
                    "company-delta/mcp/"
                ),
                "mcp_tool": "get_company_delta",
                "payment": None,
                "input": {
                    "company": "string",
                    "since": "YYYY-MM-DD",
                },
                "output": {
                    "status": "ok | not_found",
                    "company": "string",
                    "period_from": "YYYY-MM-DD",
                    "period_to": "YYYY-MM-DD",
                    "changes": "array",
                    "material_changes_found": "integer",
                    "checked_at": "string",
                    "request_id": "string",
                },
            },
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
                    "environment": "testnet",
                },
                "input": {
                    "query": "string",
                },
                "output": {
                    "status": "found | not_found",
                    "query": "string",
                    "official_source": "object | null",
                    "source_date": "string | null",
                    "relevant_evidence": "string",
                    "confidence": "number",
                    "checked_at": "string",
                    "request_id": "string",
                },
            },
        ],
        "openapi": (
            "https://machine-job-fishing-net.onrender.com/openapi.json"
        ),
        "documentation": (
            "https://machine-job-fishing-net.onrender.com/docs"
        ),
    }


# ---------------------------------------------------------------------------
# A2A V1 DISCOVERY AND JSON-RPC BINDING
# ---------------------------------------------------------------------------

@app.get("/.well-known/agent-card.json")
def a2a_agent_card(request: Request, response: Response):
    card = {
        "name": "Company Delta",
        "description": (
            "Detects material changes in a public company between a baseline "
            "date and today, with source provenance. Research intelligence only."
        ),
        "supportedInterfaces": [{
            "url": f"{PUBLIC_BASE_URL}/a2a/company-delta",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }],
        "version": "1.0.0",
        "documentationUrl": f"{PUBLIC_BASE_URL}/docs",
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["application/json"],
        "skills": [{
            "id": "company_delta",
            "name": "Company Delta",
            "description": (
                "Find verified, material company changes since a supplied ISO date."
            ),
            "tags": ["company research", "change detection", "primary sources"],
            "examples": [
                '{"company":"Intel Corporation","since":"2024-12-01"}'
            ],
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        }],
    }
    etag = '"company-delta-a2a-v1"'
    response.headers["Cache-Control"] = "public, max-age=300"
    response.headers["ETag"] = etag
    metadata = a2a_client_metadata(request)
    a2a_log(
        "a2a_agent_card_fetch",
        request_id=request.headers.get("x-request-id") or str(uuid.uuid4()),
        status="ok",
        **metadata,
    )
    return card


@app.post("/a2a/company-delta")
async def a2a_company_delta(request: Request):
    global A2A_ACTIVE_JOBS
    protocol_started = time.perf_counter()
    metadata = a2a_client_metadata(request)
    try:
        body = await request.json()
    except Exception:
        return a2a_error(None, -32700, "Invalid JSON payload")

    rpc_id = body.get("id") if isinstance(body, dict) else None
    method = body.get("method") if isinstance(body, dict) else None
    params = body.get("params") if isinstance(body, dict) else None
    a2a_log(
        "a2a_message_received",
        request_id=rpc_id,
        method=method,
        status="received",
        **metadata,
    )
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or "id" not in body:
        return a2a_error(rpc_id, -32600, "Request payload validation error")
    requested_version = request.headers.get("a2a-version")
    if requested_version and requested_version != "1.0":
        return a2a_error(rpc_id, -32009, "Version not supported")

    if method == "GetTask":
        task_id = params.get("id") if isinstance(params, dict) else None
        task = A2A_TASKS.get(task_id) if isinstance(task_id, str) else None
        if task is None:
            return a2a_error(rpc_id, -32001, "Task not found")
        return {"jsonrpc": "2.0", "id": rpc_id, "result": a2a_task_view(task)}

    if method != "SendMessage":
        return a2a_error(rpc_id, -32601, "Method not found")

    try:
        company, since, message_id, context_id = extract_company_delta_input(params)
    except ValueError as exc:
        return a2a_error(rpc_id, -32602, str(exc))

    existing_task_id = A2A_MESSAGE_TASKS.get(message_id)
    if existing_task_id:
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {"task": a2a_task_view(A2A_TASKS[existing_task_id])},
        }

    now = time.monotonic()
    client_key = metadata["client"]
    async with A2A_STATE_LOCK:
        recent = A2A_REQUEST_TIMES[client_key]
        while recent and now - recent[0] >= 3600:
            recent.popleft()
        if len(recent) >= A2A_RATE_LIMIT_PER_HOUR:
            return a2a_error(rpc_id, -32603, "A2A job rate limit reached; retry later.")
        if A2A_ACTIVE_JOBS >= A2A_MAX_CONCURRENT_JOBS:
            return a2a_error(rpc_id, -32603, "Company Delta is busy; retry later.")
        recent.append(now)
        A2A_ACTIVE_JOBS += 1

        task_id = str(uuid.uuid4())
        task = {
            "id": task_id,
            "contextId": context_id,
            "status": {"state": "TASK_STATE_SUBMITTED", "timestamp": utc_now()},
            "history": [params["message"]],
            "_company": company,
            "_since": since,
            "_message_id": message_id,
            "_request_id": str(rpc_id),
            "_client_metadata": metadata,
        }
        A2A_TASKS[task_id] = task
        A2A_MESSAGE_TASKS[message_id] = task_id

    background = asyncio.create_task(run_a2a_company_delta(task_id))
    A2A_BACKGROUND_TASKS.add(background)
    background.add_done_callback(A2A_BACKGROUND_TASKS.discard)
    a2a_log(
        "a2a_protocol_interaction",
        request_id=rpc_id,
        method=method,
        task_id=task_id,
        message_id=message_id,
        company=company,
        since=since,
        status="TASK_STATE_SUBMITTED",
        latency_ms=round((time.perf_counter() - protocol_started) * 1000),
        **metadata,
    )
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {"task": a2a_task_view(task)},
    }


# ---------------------------------------------------------------------------
# TEMPORARY WALLET TEST ENDPOINTS
# ---------------------------------------------------------------------------

@app.get("/internal/wallet-test")
async def wallet_test():
    async with CdpClient() as cdp:
        account = await cdp.evm.get_or_create_account(
            name="machine-job-receiver"
        )

        return {
            "status": "ok",
            "address": account.address,
        }


@app.get("/internal/buyer-wallet-test")
async def buyer_wallet_test():
    async with CdpClient() as cdp:
        account = await cdp.evm.get_or_create_account(
            name="machine-job-buyer"
        )

        return {
            "status": "ok",
            "address": account.address,
        }


# ---------------------------------------------------------------------------
# MCP TRANSPORT SECURITY
# ---------------------------------------------------------------------------

transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "machine-job-fishing-net.onrender.com",
        "127.0.0.1:*",
        "localhost:*",
    ],
    allowed_origins=[
        "https://machine-job-fishing-net.onrender.com",
        "http://127.0.0.1:*",
        "http://localhost:*",
    ],
)


# ---------------------------------------------------------------------------
# TSRE MCP MOUNT
# ---------------------------------------------------------------------------

mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=transport_security,
)

app.mount("/mcp", mcp_app)


# ---------------------------------------------------------------------------
# COMPANY DELTA MCP MOUNT
# ---------------------------------------------------------------------------

company_delta_mcp_app = company_delta_mcp.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=transport_security,
)

app.mount(
    "/company-delta/mcp",
    company_delta_mcp_app,
)
