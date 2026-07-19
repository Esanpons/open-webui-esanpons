from open_webui.utils.middleware import _parse_unwrapped_tool_call


AVAILABLE_TOOLS = {
    'get_current_timestamp': {'spec': {}},
    'search_knowledge_bases': {'spec': {}},
}


def test_recovers_qwen_bare_json_tool_call():
    tool_call = _parse_unwrapped_tool_call(
        '{"name":"get_current_timestamp","arguments":{}}', AVAILABLE_TOOLS
    )

    assert tool_call is not None
    assert tool_call['function'] == {
        'name': 'get_current_timestamp',
        'arguments': '{}',
    }


def test_rejects_unknown_tool_name():
    assert (
        _parse_unwrapped_tool_call(
            '{"name":"delete_everything","arguments":{}}', AVAILABLE_TOOLS
        )
        is None
    )


def test_rejects_normal_json_response_even_when_it_mentions_a_tool():
    assert (
        _parse_unwrapped_tool_call(
            '{"name":"get_current_timestamp","arguments":{},"explanation":"example"}',
            AVAILABLE_TOOLS,
        )
        is None
    )


def test_rejects_tool_json_embedded_in_prose():
    assert (
        _parse_unwrapped_tool_call(
            'Here is an example: {"name":"get_current_timestamp","arguments":{}}',
            AVAILABLE_TOOLS,
        )
        is None
    )


def test_accepts_json_encoded_argument_object():
    tool_call = _parse_unwrapped_tool_call(
        '{"name":"search_knowledge_bases","arguments":"{\\"query\\":\\"hola\\"}"}',
        AVAILABLE_TOOLS,
    )

    assert tool_call is not None
    assert tool_call['function']['arguments'] == '{"query": "hola"}'
