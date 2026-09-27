# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx==0.28.1", "pydantic==2.13.5"]
# ///
"""Unofficial local adapter based on Dow Jones's public Retrieval API demo.

Live API compatibility is unverified until tested with an entitled account.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

AUTH_URL = "https://accounts.dowjones.com/oauth2/v1/token"
RETRIEVAL_URL = "https://api.dowjones.com/content/gen-ai/retrieve"
ENV_NAMES = ("FACTIVA_CLIENTID", "FACTIVA_USERNAME", "FACTIVA_PASSWORD", "FACTIVA_USER_ID")


class FactivaError(Exception):
    """A sanitized error suitable for display to the caller."""


class SearchFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: Literal["Language", "Organization", "NewsSubject", "Industry", "Source", "Region"]
    value: Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]
    limit: Annotated[int, Field(ge=1, le=20)] = 10
    filters: Annotated[list[SearchFilter], Field(max_length=12)] = Field(default_factory=list)
    days_range: Literal[
        "LastDay", "Last2Days", "LastWeek", "Last2Weeks", "LastMonth", "Last2Months",
        "Last3Months", "Last6Months", "LastYear", "Last2Years", "Last5Years", "AllDates",
    ] | None = None
    date_from: date | None = None
    date_to: date | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        if (self.date_from is None) != (self.date_to is None):
            raise ValueError("Supply both date_from and date_to.")
        if self.date_from is not None:
            if self.days_range is not None:
                raise ValueError("Use a custom date range OR days_range.")
            if self.date_from > self.date_to:
                raise ValueError("date_from must not exceed date_to.")
        return self


def build_payload(search: SearchQuery, user_id: str) -> dict:
    dates = {"days_range": search.days_range or "LastMonth"}
    if search.date_from is not None:
        dates = {"custom": {"from": search.date_from.isoformat(), "to": search.date_to.isoformat()}}
    return {"data": {
        "id": "GenAIRetrieval", "type": "genai-content",
        "attributes": {
            "response_limit": search.limit,
            "query": {"value": search.query.strip(), "search_filters": [
                item.model_dump() for item in search.filters
            ], "date": dates},
            "metrics_data": {"user_id": user_id, "work_id": uuid4().hex},
        },
    }}


async def post_json(client: httpx.AsyncClient, url: str, **kwargs) -> dict:
    # Never return response bodies or request headers in errors: they may contain secrets.
    try:
        response = await client.post(url, **kwargs)
    except httpx.RequestError:
        raise FactivaError("Factiva network request failed; no automatic retry was made.") from None
    if not 200 <= response.status_code < 300:
        hints = {401: "Authentication failed.", 403: "Check Retrieval API entitlement.",
                 429: "Rate limit reached; wait before retrying."}
        hint = hints.get(response.status_code, "Check the contracted API specification.")
        raise FactivaError(f"Factiva HTTP {response.status_code}. {hint}")
    try:
        body = response.json()
    except ValueError:
        raise FactivaError("Factiva returned invalid JSON.") from None
    if not isinstance(body, dict):
        raise FactivaError("Unexpected Factiva response format.")
    return body


def required_token(body: dict, key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise FactivaError("Factiva authentication response is missing a required token.")
    return value


async def authenticate(client: httpx.AsyncClient, settings: dict) -> str:
    authn = await post_json(client, AUTH_URL, data={
        "client_id": settings["FACTIVA_CLIENTID"],
        "username": settings["FACTIVA_USERNAME"], "password": settings["FACTIVA_PASSWORD"],
        "grant_type": "password", "connection": "service-account",
        "scope": "openid service_account_id",
    })
    authz = await post_json(client, AUTH_URL, data={
        "client_id": settings["FACTIVA_CLIENTID"],
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "connection": "service-account", "scope": "openid pib",
        "access_token": required_token(authn, "access_token"),
        "assertion": required_token(authn, "id_token"),
    })
    return required_token(authz, "access_token")


def normalize_results(body: dict) -> list[dict]:
    """Preserve each returned chunk and its provenance; never invent missing metadata."""
    if not isinstance(body.get("data"), list):
        raise FactivaError("Unexpected Retrieval API data format; expected a chunk list.")
    records = []
    try:
        for chunk in body["data"]:
            attributes, meta = chunk.get("attributes", {}), chunk.get("meta", {})
            record = {
                "document_id": meta.get("original_doc_id"),
                "resource_id": chunk.get("id"),
                "title": attributes.get("headline", {}).get("main", {}).get("text"),
                "publisher": meta.get("source", {}).get("name"),
                "source_code": meta.get("source", {}).get("code"),
                "publication_date": attributes.get("publication_date"),
                "publication_time": attributes.get("publication_time"),
                "byline": attributes.get("byline", {}).get("text"),
                "language": meta.get("language", {}).get("code"),
                "content_type": attributes.get("content_type"),
                "api_url": chunk.get("links", {}).get("self"),
                "copyright": attributes.get("copyright", {}).get("text"),
                "snippet": "\n".join(x["text"] for x in attributes.get("snippet", {}).get("content", []) if "text" in x),
                "retrieved_text": "\n".join(x["text"] for x in attributes.get("content", []) if "text" in x),
            }
            record["missing_citation_fields"] = [key for key in
                ("document_id", "title", "publisher", "publication_date") if not record[key]]
            records.append(record)
    except (AttributeError, TypeError, KeyError):
        raise FactivaError("Unexpected Retrieval API chunk format; check the current API schema.") from None
    return records


def prepare_search(search: SearchQuery) -> dict[str, Any]:
    """Validate and preview a request locally; no network, credentials, or real search results.

    Filters require verified Factiva taxonomy codes. Defaults to LastMonth, max 20 chunks.
    """
    return {"mode": "preview_only", "network_called": False,
            "endpoint": RETRIEVAL_URL, "required_environment": list(ENV_NAMES),
            "payload": build_payload(search, "PREVIEW_ONLY_USER_ID")}


async def search_factiva(search: SearchQuery) -> dict[str, Any]:
    """Search licensed Factiva content via the Retrieval API; may incur contracted usage.

    Use verified source codes for publisher filtering. No automatic retries or pagination.
    Credential values are read only from environment, never supplied as tool arguments.
    """
    settings = {name: os.environ.get(name, "") for name in ENV_NAMES}
    missing = [name for name, value in settings.items() if not value.strip()]
    if missing:
        raise FactivaError("Retrieval API not configured. Missing environment variables: " + ", ".join(missing))
    payload = build_payload(search, settings["FACTIVA_USER_ID"])
    # ponytail: authenticate per search; add expiry-aware in-memory reuse if volume warrants it.
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        token = await authenticate(client, settings)
        body = await post_json(client, RETRIEVAL_URL, json=payload, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/json",
        })
    records = normalize_results(body)
    return {"mode": "live", "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "search": search.model_dump(mode="json"), "retrieved_chunk_count": len(records),
            "records": records, "limitations": [
                "Retrieved chunks may be incomplete; count is not the total number of matching articles.",
                "Factiva inclusion does not establish primary-source status or factual accuracy.",
                "api_url is an API resource link, not necessarily a public reader URL.",
            ]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Query the Factiva Retrieval API directly; JSON search input on stdin.")
    parser.add_argument("action", choices=("prepare", "search"), help="prepare makes no Factiva request; search uses your API entitlement")
    args = parser.parse_args()
    try:
        search = SearchQuery.model_validate(json.load(sys.stdin))
        result = prepare_search(search) if args.action == "prepare" else asyncio.run(search_factiva(search))
    except (ValueError, ValidationError):
        print(json.dumps({"error": "Invalid search JSON. Check query, limit, filters and date range; see SKILL.md."}), file=sys.stderr)
        return 2
    except FactivaError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
