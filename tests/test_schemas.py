"""Output-schema declarations for the pilot tools + notification helpers."""

from __future__ import annotations

import json

from rocq_mcp import schemas, taxonomy

PILOT_TOOLS = {
    "rocq_check": schemas.CHECK_OUTPUT_SCHEMA,
    "rocq_step_multi": schemas.STEP_MULTI_OUTPUT_SCHEMA,
    "rocq_compile_file": schemas.COMPILE_FILE_OUTPUT_SCHEMA,
    "rocq_search": schemas.SEARCH_OUTPUT_SCHEMA,
}


class TestSchemaShape:
    def test_reason_enum_matches_taxonomy(self):
        expected = sorted(str(r) for r in taxonomy.FailureReason)
        for name, schema in PILOT_TOOLS.items():
            assert schema["properties"]["reason"]["enum"] == expected, name


class TestSchemasOverTheWire:
    async def test_declared_and_failure_envelope_round_trips(self):
        from fastmcp import Client

        from rocq_mcp.server import mcp

        async with Client(mcp) as client:
            tools = {t.name: t for t in await client.list_tools()}
            for name in PILOT_TOOLS:
                declared = tools[name].outputSchema
                assert declared is not None, name
                assert declared["required"] == ["success"], name

            # A validation failure must serialize cleanly through the
            # declared schema (structured content is not rejected).
            result = await client.call_tool("rocq_search", {"pattern": ""})
            payload = (
                result.data
                if isinstance(result.data, dict)
                else json.loads(result.content[0].text)
            )
            assert payload["success"] is False
            assert payload["reason"] == "validation"


class TestNotifyHelpers:
    def test_notify_never_raises_even_if_factory_explodes(self):
        from rocq_mcp import envelope as env

        def bomb(ctx):
            raise RuntimeError("factory bug")

        env._notify({}, bomb)  # must not raise
