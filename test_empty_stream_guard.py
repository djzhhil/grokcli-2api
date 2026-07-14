"""Guards against empty/malformed HTTP 200 Responses streams."""

from __future__ import annotations

import openai_responses as r


def test_incomplete_tool_does_not_open_envelope() -> None:
    s = r.ResponsesLiveStreamer(response_id="resp_a", model="grok-4.5")
    frames = s.on_tool_delta(
        [
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"file_path":'},
            }
        ]
    )
    assert frames == []
    assert s.has_client_payload() is False
    assert s._started is False
    # Truncated non-JSON must not become response.completed.
    assert s.complete() == []


def test_complete_tool_emits_midstream_when_required_keys_present() -> None:
    s = r.ResponsesLiveStreamer(response_id="resp_b", model="grok-4.5")
    frames = s.on_tool_delta(
        [
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "Read", "arguments": '{"file_path":"a.py"}'},
            }
        ]
    )
    assert frames
    assert s.has_client_payload() is True
    done = s.complete()
    assert any("response.completed" in x for x in done)


def test_terminal_flush_ships_parseable_json_without_required_keys() -> None:
    """Mid-stream hold is strict; end-of-stream flushes parseable JSON objects
    for tools without a known required-key schema. Known incomplete Update is
    still dropped to avoid wrong edits.
    """
    s = r.ResponsesLiveStreamer(response_id="resp_b2", model="grok-4.5")
    # Unknown custom tool with complete JSON — ship at terminal even if we have
    # no required-key schema for it.
    frames = s.on_tool_delta(
        [
            {
                "index": 0,
                "id": "call_2",
                "type": "function",
                "function": {"name": "CustomTool", "arguments": '{"foo":"bar"}'},
            }
        ]
    )
    # Custom tools with non-empty object are ready mid-stream too.
    assert frames
    assert s.has_client_payload() is True
    done = s.complete(force_flush_partial_tools=True)
    assert done
    assert any("response.completed" in x for x in done)


def test_empty_complete_refuses_silent_completed() -> None:
    s = r.ResponsesLiveStreamer(response_id="resp_c", model="grok-4.5")
    assert s.complete(reasoning="thinking only") == []
    assert s.has_client_payload() is False


def test_text_stream_completes() -> None:
    s = r.ResponsesLiveStreamer(response_id="resp_d", model="grok-4.5")
    assert s.on_text_delta("hi")
    done = s.complete()
    assert any("response.completed" in x for x in done)


def test_incomplete_then_complete_same_tool() -> None:
    s = r.ResponsesLiveStreamer(response_id="resp_e", model="grok-4.5")
    assert (
        s.on_tool_delta(
            [
                {
                    "index": 0,
                    "id": "call_x",
                    "function": {"name": "Bash", "arguments": '{"command":'},
                }
            ]
        )
        == []
    )
    frames = s.on_tool_delta(
        [
            {
                "index": 0,
                "id": "call_x",
                "function": {"name": "Bash", "arguments": '{"command":"ls"}'},
            }
        ]
    )
    assert frames
    assert s.has_client_payload()


def test_local_fallback_holds_missing_required_keys() -> None:
    """Import-failure fallback must not open envelope on incomplete tool objects.

    Alias normalization maps path→file_path, so Read with path is ready. Still
    hold true incompletes (empty object / truncated JSON) so we never open
    response.created on a turn that may end empty/malformed for Claude Code.
    """
    # path is an alias of file_path — treat as ready after normalize.
    assert r._local_tool_args_ready('{"path":"a.py"}', tool_name="Read") is True
    assert r._local_tool_args_ready('{"file_path":"a.py"}', tool_name="Read") is True
    assert r._local_tool_args_ready('{"command":"ls"}', tool_name="Bash") is True
    assert r._local_tool_args_ready("{}", tool_name="Bash") is False
    assert r._local_tool_args_ready('{"file_path":', tool_name="Read") is False
    # Update without old/new must stay held.
    assert (
        r._local_tool_args_ready('{"file_path":"a.py"}', tool_name="Update")
        is False
    )
    assert (
        r._local_tool_args_ready(
            '{"file_path":"a.py","old_string":"x","new_string":"y"}',
            tool_name="Update",
        )
        is True
    )
    # camelCase aliases
    assert (
        r._local_tool_args_ready(
            '{"filePath":"a.py","oldString":"x","newString":"y"}',
            tool_name="Update",
        )
        is True
    )

    s = r.ResponsesLiveStreamer(response_id="resp_fb", model="grok-4.5")
    s._args_ready = lambda args, *, tool_name=None: r._local_tool_args_ready(
        args or "", tool_name=tool_name
    )
    # Incomplete Update (path only) must not open envelope mid-stream.
    frames = s.on_tool_delta(
        [
            {
                "index": 0,
                "id": "call_fb",
                "type": "function",
                "function": {"name": "Update", "arguments": '{"file_path":"a.py"}'},
            }
        ]
    )
    assert frames == []
    assert s.has_client_payload() is False
    assert s._started is False
    # Terminal flush must NOT ship schema-incomplete Update (wrong edit risk).
    done = s.complete(force_flush_partial_tools=True)
    assert done == []
    assert s.has_client_payload() is False


def test_update_doubled_json_prefers_richer_object() -> None:
    """Grok sometimes emits partial+full Update args in one SSE chunk."""
    partial_full = (
        '{"file_path":"/x.py"}'
        '{"file_path":"/x.py","old_string":"a","new_string":"b"}'
    )
    # Local sanitize (import-safe path)
    out = r._local_sanitize_tool_arguments_json(partial_full)
    assert "old_string" in out and "new_string" in out
    assert r._local_tool_args_ready(out, tool_name="Update") is True

    s = r.ResponsesLiveStreamer(response_id="resp_upd", model="grok-4.5")
    frames = s.on_tool_delta(
        [
            {
                "index": 0,
                "id": "call_u1",
                "type": "function",
                "function": {"name": "Update", "arguments": partial_full},
            }
        ]
    )
    assert frames
    assert s.has_client_payload() is True
    # Emitted arguments should include old/new, not just file_path.
    blob = "".join(frames)
    assert "old_string" in blob and "new_string" in blob


def test_update_alias_keys_ready() -> None:
    s = r.ResponsesLiveStreamer(response_id="resp_alias", model="grok-4.5")
    frames = s.on_tool_delta(
        [
            {
                "index": 0,
                "id": "call_alias",
                "type": "function",
                "function": {
                    "name": "Update",
                    "arguments": '{"path":"/x.py","oldString":"a","newString":"b"}',
                },
            }
        ]
    )
    assert frames
    blob = "".join(frames)
    assert "file_path" in blob or "path" in blob
    assert "old_string" in blob or "oldString" in blob


def test_classify_upstream_body_error_empty_and_gateway() -> None:
    try:
        import app as app_mod
    except ModuleNotFoundError as e:
        # Host unit runs may lack fastapi; container/selftest still covers this.
        print(f"skip classify test: {e}")
        return

    assert "empty body" in (app_mod._classify_upstream_body_error(b"") or "")
    assert "empty body" in (app_mod._classify_upstream_body_error(None) or "")
    html_err = app_mod._classify_upstream_body_error(
        b"<!DOCTYPE html><html>cloudflare</html>"
    )
    assert html_err and "proxy/gateway" in html_err
    ctype_err = app_mod._classify_upstream_body_error(
        b"<html>x</html>", content_type="text/html; charset=utf-8"
    )
    assert ctype_err and "HTML" in ctype_err
    assert app_mod._classify_upstream_body_error(b'{"choices":[]}') is None


def _parse_frames(frames: list[str]) -> list[dict]:
    import json

    out: list[dict] = []
    for fr in frames:
        for line in fr.splitlines():
            if line.startswith("data: ") and line.strip() != "data: [DONE]":
                out.append(json.loads(line[6:]))
    return out


def test_tool_stream_sequence_numbers_monotonic_and_created_first() -> None:
    """response.created must be seq 0; tool frames must not steal lower numbers.

    The previous bug emitted tool frames first (seq 0..) then created/in_progress
    with higher numbers — Claude Code reports that as empty/malformed HTTP 200.
    """
    s = r.ResponsesLiveStreamer(response_id="resp_seq", model="grok-4.5")
    frames = s.on_tool_delta(
        [
            {
                "index": 0,
                "id": "call_seq",
                "type": "function",
                "function": {"name": "Bash", "arguments": '{"command":"ls"}'},
            }
        ]
    )
    assert frames
    events = _parse_frames(frames)
    assert events[0]["type"] == "response.created"
    assert events[0]["sequence_number"] == 0
    assert events[1]["type"] == "response.in_progress"
    assert events[1]["sequence_number"] == 1
    seqs = [e["sequence_number"] for e in events]
    assert seqs == list(range(len(seqs))), seqs
    # First tool body event must come after envelope.
    types = [e["type"] for e in events]
    assert types.index("response.output_item.added") > types.index("response.created")


def test_completed_reuses_stream_item_ids() -> None:
    """response.completed.output[].id must match streamed item ids."""
    s = r.ResponsesLiveStreamer(response_id="resp_ids", model="grok-4.5")
    live = s.on_tool_delta(
        [
            {
                "index": 0,
                "id": "call_ids",
                "type": "function",
                "function": {"name": "Bash", "arguments": '{"command":"pwd"}'},
            }
        ]
    )
    done = s.complete()
    events = _parse_frames(live + done)
    stream_fc_ids = [
        e["item"]["id"]
        for e in events
        if e.get("type") == "response.output_item.done"
        and (e.get("item") or {}).get("type") == "function_call"
    ]
    completed = next(e for e in events if e.get("type") == "response.completed")
    out_ids = [o["id"] for o in completed["response"]["output"] if o.get("type") == "function_call"]
    assert stream_fc_ids
    assert out_ids == stream_fc_ids

    # Text path too.
    s2 = r.ResponsesLiveStreamer(response_id="resp_ids2", model="grok-4.5")
    live2 = s2.on_text_delta("hi")
    done2 = s2.complete()
    events2 = _parse_frames(live2 + done2)
    msg_ids = [
        e["item"]["id"]
        for e in events2
        if e.get("type") == "response.output_item.done"
        and (e.get("item") or {}).get("type") == "message"
    ]
    completed2 = next(e for e in events2 if e.get("type") == "response.completed")
    out_msg = [o["id"] for o in completed2["response"]["output"] if o.get("type") == "message"]
    assert msg_ids
    assert out_msg == msg_ids


def test_failed_never_opened_opens_envelope_first() -> None:
    """Bare response.failed at seq 0 is reported as empty/malformed HTTP 200."""
    frames = r.failed_responses_sse(
        response_id="resp_fail0",
        message="Upstream empty",
        model="grok-4.5",
    )
    events = _parse_frames(frames)
    assert [e["type"] for e in events] == [
        "response.created",
        "response.in_progress",
        "response.failed",
    ]
    assert [e["sequence_number"] for e in events] == [0, 1, 2]
    assert events[-1]["response"]["status"] == "failed"
    assert events[-1]["response"]["error"]["message"] == "Upstream empty"


def test_streamer_fail_continues_sequence_after_live_delta() -> None:
    """Mid-stream fail must not rewind sequence_number to 0."""
    s = r.ResponsesLiveStreamer(response_id="resp_fail_live", model="grok-4.5")
    live = s.on_text_delta("partial")
    failed = s.fail("Proxy error: boom")
    events = _parse_frames(live + failed)
    types = [e["type"] for e in events]
    seqs = [e["sequence_number"] for e in events]
    assert types[0] == "response.created"
    assert types[-1] == "response.failed"
    assert seqs == list(range(len(seqs))), seqs
    assert events[-1]["sequence_number"] > 0
    assert events[-1]["response"]["error"]["message"] == "Proxy error: boom"


def test_streamer_fail_before_start_opens_envelope() -> None:
    s = r.ResponsesLiveStreamer(response_id="resp_fail_pre", model="grok-4.5")
    events = _parse_frames(s.fail("no accounts left"))
    assert [e["type"] for e in events] == [
        "response.created",
        "response.in_progress",
        "response.failed",
    ]
    assert [e["sequence_number"] for e in events] == [0, 1, 2]


def test_forced_function_tool_choice_maps_to_required() -> None:
    """sub2api forced tools must not keep nested function tool_choice.

    cli-chat-proxy returns empty HTTP 200 for
    ``{"type":"function","function":{"name":...}}`` / flat name form, which
    becomes an empty Anthropic envelope after sub2api conversion.
    """
    try:
        import app as app_mod
    except ModuleNotFoundError as e:
        print(f"skip tool_choice sanitize test: {e}")
        return

    cases = [
        {"type": "function", "name": "Bash"},
        {"type": "function", "function": {"name": "Bash"}},
        {"type": "tool", "name": "Bash"},
        {"type": "any"},
        "required",
    ]
    for tc in cases:
        body = {
            "model": "grok-4.5",
            "messages": [{"role": "user", "content": "x"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "Bash",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    },
                }
            ],
            "tool_choice": tc,
        }
        app_mod._sanitize_upstream_body(body, model="grok-4.5")
        assert body.get("tool_choice") == "required", (tc, body.get("tool_choice"))
        assert app_mod._normalize_tool_choice(tc) == "required"


if __name__ == "__main__":
    test_incomplete_tool_does_not_open_envelope()
    test_complete_tool_emits_midstream_when_required_keys_present()
    test_terminal_flush_ships_parseable_json_without_required_keys()
    test_empty_complete_refuses_silent_completed()
    test_text_stream_completes()
    test_incomplete_then_complete_same_tool()
    test_local_fallback_holds_missing_required_keys()
    test_classify_upstream_body_error_empty_and_gateway()
    test_tool_stream_sequence_numbers_monotonic_and_created_first()
    test_completed_reuses_stream_item_ids()
    test_failed_never_opened_opens_envelope_first()
    test_streamer_fail_continues_sequence_after_live_delta()
    test_streamer_fail_before_start_opens_envelope()
    test_forced_function_tool_choice_maps_to_required()
    print("ALL PASS")
