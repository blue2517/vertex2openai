# -*- coding: utf-8 -*-
"""Client schema limits fail as OpenAI-compatible 400s before upstream work."""

import asyncio
import json

from models import OpenAIMessage, OpenAIRequest
from schema_validation import SchemaValidationError, validate_json_schema
from upstreams.cookie_proxy import CookieProxyUpstream
from upstreams.express_sdk import ExpressSDKUpstream


def _request(schema):
    return OpenAIRequest(
        model="gemini-3.7-flash",
        messages=[OpenAIMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "bad", "parameters": schema}}],
    )


def _deep_schema(depth=40):
    root = {"type": "object", "properties": {}}
    cursor = root
    for index in range(depth):
        child = {"type": "object", "properties": {}}
        cursor["properties"][f"level_{index}"] = child
        cursor = child
    return root


def test_schema_validator_rejects_malformed_and_bounded_depth():
    try:
        validate_json_schema({"type": "object", "properties": []}, label="tool schema")
    except SchemaValidationError as exc:
        assert "properties must be an object" in str(exc)
    else:
        raise AssertionError("malformed schema was accepted")

    try:
        validate_json_schema(_deep_schema(), label="tool schema")
    except SchemaValidationError as exc:
        assert "maximum depth" in str(exc)
    else:
        raise AssertionError("over-deep schema was accepted")

    too_many = {"type": "object", "properties": {
        f"p{i}": {"type": "string"} for i in range(10_001)
    }}
    try:
        validate_json_schema(too_many, label="tool schema")
    except SchemaValidationError as exc:
        assert "maximum node count" in str(exc)
    else:
        raise AssertionError("over-wide schema was accepted")

    try:
        validate_json_schema({"type": "string", "description": "x" * 1_000_001},
                             label="tool schema")
    except SchemaValidationError as exc:
        assert "serialized-size limit" in str(exc)
    else:
        raise AssertionError("oversized schema was accepted")


def test_both_upstreams_return_clear_400_before_auth_or_network():
    request = _request({"type": "object", "required": "not-an-array"})

    class NeverNeededRequest:
        @property
        def app(self):
            raise AssertionError("validation must happen before Express key lookup")

    cookie_response = asyncio.run(CookieProxyUpstream().chat_completions(request, NeverNeededRequest()))
    express_response = asyncio.run(ExpressSDKUpstream().chat_completions(request, NeverNeededRequest()))
    for response in (cookie_response, express_response):
        assert response.status_code == 400
        payload = json.loads(response.body)
        assert payload["error"]["type"] == "invalid_request_error"
        assert "required must be an array of strings" in payload["error"]["message"]
