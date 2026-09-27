"""Offline tests only: synthetic content and tokens, no Dow Jones requests."""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import ValidationError

from factiva_mcp import api as app

ToolError = app.FactivaError

SYNTHETIC_CHUNK = {
    "id": "synthetic-resource", "meta": {
        "original_doc_id": "TEST-ONLY-001", "source": {"name": "Synthetic Test Publisher", "code": "TEST"},
        "language": {"code": "en"},
    },
    "links": {"self": "https://example.invalid/test-only"},
    "attributes": {
        "headline": {"main": {"text": "Synthetic test fixture, not research evidence"}},
        "publication_date": "2026-01-01", "copyright": {"text": "Synthetic fixture"},
        "snippet": {"content": [{"text": "Test snippet"}]},
        "content": [{"text": "Test chunk A"}, {"text": "Test chunk B"}],
    },
}
SETTINGS = dict(zip(app.ENV_NAMES, ("test-client", "test-user", "test-password", "test-human")))


class OfflineTests(unittest.IsolatedAsyncioTestCase):
    def test_payload_dates_and_source_filter(self):
        query = app.SearchQuery(query="  test market  ", date_from="2026-01-01", date_to="2026-02-01",
                                filters=[{"scope": "Source", "value": "TEST"}], limit=3)
        payload = app.build_payload(query, "test-human")["data"]["attributes"]
        self.assertEqual(payload["query"], {"value": "test market", "search_filters": [
            {"scope": "Source", "value": "TEST"}], "date": {"custom": {"from": "2026-01-01", "to": "2026-02-01"}}})
        self.assertEqual(payload["response_limit"], 3)
        self.assertEqual(payload["metrics_data"]["user_id"], "test-human")
        self.assertNotEqual(payload["metrics_data"]["work_id"], app.build_payload(query, "test-human")["data"]["attributes"]["metrics_data"]["work_id"])

    def test_invalid_search_inputs(self):
        for fields in ({"query": " "}, {"limit": 21}, {"date_from": "2026-01-01"},
                       {"date_from": "2026-02-01", "date_to": "2026-01-01"},
                       {"date_from": "2026-01-01", "date_to": "2026-02-01", "days_range": "LastDay"},
                       {"filters": [{"scope": "FakeScope", "value": "x"}]},
                       {"filters": [{"scope": "Source", "value": " "}]}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                app.SearchQuery(**({"query": "test"} | fields))

    def test_preview_is_not_evidence(self):
        result = app.prepare_search(app.SearchQuery(query="test"))
        self.assertFalse(result["network_called"])
        self.assertEqual(result["mode"], "preview_only")
        self.assertNotIn("records", result)
        self.assertEqual(result["payload"]["data"]["attributes"]["query"]["date"], {"days_range": "LastMonth"})

    def test_provenance_and_missing_metadata(self):
        records = app.normalize_results({"data": [SYNTHETIC_CHUNK, {"attributes": {}}]})
        self.assertEqual(records[0]["document_id"], "TEST-ONLY-001")
        self.assertEqual(records[0]["retrieved_text"], "Test chunk A\nTest chunk B")
        self.assertEqual(records[0]["copyright"], "Synthetic fixture")
        self.assertEqual(records[0]["missing_citation_fields"], [])
        self.assertIsNone(records[1]["api_url"])
        self.assertIn("publisher", records[1]["missing_citation_fields"])
        self.assertEqual(app.normalize_results({"data": []}), [])

    def test_malformed_results_fail_explicitly(self):
        for body in ({}, {"data": {}}, {"data": [None]}, {"data": [{"attributes": {"content": [{"text": None}]}}]}):
            with self.subTest(body=body), self.assertRaises(ToolError):
                app.normalize_results(body)

    async def test_missing_configuration_makes_no_request(self):
        with patch.dict("os.environ", {}, clear=True), patch.object(app.httpx, "AsyncClient") as client:
            with self.assertRaisesRegex(ToolError, "not configured"):
                await app.search_factiva(app.SearchQuery(query="test"))
            client.assert_not_called()

    async def test_full_search_with_mock_http(self):
        requests = []

        def handler(request):
            requests.append(request)
            if len(requests) == 1:
                self.assertEqual(str(request.url), app.AUTH_URL)
                form = parse_qs(request.content.decode())
                self.assertEqual(form["grant_type"], ["password"])
                return httpx.Response(200, json={"id_token": "synthetic-id-token", "access_token": "synthetic-authn"})
            if len(requests) == 2:
                form = parse_qs(request.content.decode())
                self.assertEqual(form["assertion"], ["synthetic-id-token"])
                self.assertEqual(form["scope"], ["openid pib"])
                return httpx.Response(200, json={"access_token": "synthetic-authz"})
            self.assertEqual(str(request.url), app.RETRIEVAL_URL)
            self.assertEqual(request.headers["Authorization"], "Bearer synthetic-authz")
            self.assertEqual(json.loads(request.content)["data"]["attributes"]["metrics_data"]["user_id"], "test-human")
            return httpx.Response(200, json={"data": [SYNTHETIC_CHUNK]})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.dict("os.environ", SETTINGS, clear=True), patch.object(app.httpx, "AsyncClient", return_value=client):
            result = await app.search_factiva(app.SearchQuery(query="test"))
        self.assertEqual(len(requests), 3)
        self.assertEqual(result["retrieved_chunk_count"], 1)
        self.assertNotIn("synthetic-authz", json.dumps(result))
        self.assertNotIn("test-password", json.dumps(result))

    async def test_http_errors_are_sanitized_without_retries(self):
        for status in (302, 401, 403, 429, 500):
            calls = []
            def handler(request):
                calls.append(request)
                return httpx.Response(status, text="secret-response-value", headers={"Location": "https://example.invalid"})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
                with self.assertRaises(ToolError) as error:
                    await app.post_json(client, app.AUTH_URL, data={"password": "synthetic-password"})
                self.assertIn(str(status), str(error.exception))
                self.assertNotIn("secret-response-value", str(error.exception))
                self.assertEqual(len(calls), 1)

    async def test_invalid_json_and_timeout(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="invalid"))) as client:
            with self.assertRaisesRegex(ToolError, "invalid JSON"):
                await app.post_json(client, app.AUTH_URL)
        def timeout(request):
            raise httpx.ReadTimeout("synthetic secret must not leak", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
            with self.assertRaises(ToolError) as error:
                await app.post_json(client, app.AUTH_URL)
            self.assertNotIn("synthetic secret", str(error.exception))

    async def test_missing_token(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))) as client:
            with self.assertRaisesRegex(ToolError, "missing a required token"):
                await app.authenticate(client, SETTINGS)

    async def test_real_stdio_process(self):
        parameters = StdioServerParameters(command=sys.executable,
            args=[str(Path(__file__).with_name("factiva_mcp.py"))], env={key: "" for key in app.ENV_NAMES})
        async with asyncio.timeout(30):
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({tool.name for tool in tools.tools}, {"factiva_prepare_search", "factiva_search"})
                    self.assertTrue(all(tool.outputSchema for tool in tools.tools))
                    preview = await session.call_tool("factiva_prepare_search", {"search": {"query": "test"}})
                    self.assertFalse(preview.isError)
                    self.assertEqual(preview.structuredContent["mode"], "preview_only")
                    missing = await session.call_tool("factiva_search", {"search": {"query": "test"}})
                    self.assertTrue(missing.isError)
                    invalid = await session.call_tool("factiva_prepare_search", {"search": {"query": "test", "limit": 1000}})
                    self.assertTrue(invalid.isError)


if __name__ == "__main__":
    unittest.main()
