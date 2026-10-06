import json
import os
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pydantic import BaseModel, Field


app = FastAPI(
    title="Machine Job Fishing Net",
    version="0.1.0",
    description="Experimental machine-callable jobs."
)

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


class SourceRequest(BaseModel):
    query: str = Field(
        min_length=3,
        max_length=1000,
        description="Entity, claim, document or topic for which an official source is requested."
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


@app.get("/")
def root():
    return {
        "service": "Machine Job Fishing Net",
        "version": "0.1.0",
        "jobs": ["find_official_source"]
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/v1/find-official-source", response_model=SourceResponse)
def find_official_source(request: SourceRequest):

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
            input=request.query
        )

        result = json.loads(response.output_text)

        return SourceResponse(
            status=result["status"],
            query=request.query,
            official_source=result.get("official_source"),
            source_date=result.get("source_date"),
            relevant_evidence=result.get("relevant_evidence"),
            confidence=result.get("confidence", 0.0),
            checked_at=datetime.now(timezone.utc).isoformat()
        )

    except json.JSONDecodeError:
        raise HTTPException(
            status_code=502,
            detail="Search completed but returned invalid structured output."
        )

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Job failed: {type(exc).__name__}"
        )
