"""Wire parsing tests (docs/04 §3).

Unit (offline): parse_incremental/merge_docs against hand-crafted JSON
text and the live-captured 1675012 error envelope — no fixtures, no
session, no network.
"""
import json

from graphql.parsing import merge_docs, parse_incremental, strip_legacy_prefix


class TestIncrementalParsing:
    """Pins incremental stream parsing: NDJSON, glued docs, partial-tail
    tolerance, and the legacy for(;;); prefix."""

    def test_single_document(self):
        assert parse_incremental('{"a": 1}') == [{"a": 1}]

    def test_concatenated_ndjson(self):
        text = '{"a": 1}\n{"b": 2}\n{"c": 3}'
        assert parse_incremental(text) == [{"a": 1}, {"b": 2}, {"c": 3}]

    def test_no_separator_between_docs(self):
        text = '{"a": 1}{"b": 2}'
        assert parse_incremental(text) == [{"a": 1}, {"b": 2}]

    def test_partial_trailing_object_is_dropped(self):
        text = '{"a": 1}\n{"b": 2},{"incomplete"'
        docs = parse_incremental(text)
        assert docs == [{"a": 1}, {"b": 2}]

    def test_empty_and_whitespace(self):
        assert parse_incremental("") == []
        assert parse_incremental("   \n\t ") == []

    def test_legacy_prefix_stripped(self):
        assert strip_legacy_prefix('for (;;);{"ok":true}') == '{"ok":true}'
        assert strip_legacy_prefix('{"ok":true}') == '{"ok":true}'

    def test_real_error_envelope_shape(self):
        """The live-captured 1675012 envelope (docs/15 §4) parses cleanly."""
        raw = json.dumps({
            "errors": [{"message": "A server error noncoercible_variable_value "
                                   "occured. Check server logs for details.",
                        "severity": "CRITICAL", "code": 1675012}],
            "extensions": {"is_final": True},
        })
        (docs := parse_incremental(raw))
        assert docs[0]["errors"][0]["code"] == 1675012

    def test_merge_deep(self):
        docs = [{"data": {"a": {"x": 1}}}, {"data": {"a": {"y": 2}, "b": 3}}]
        assert merge_docs(docs) == {"data": {"a": {"x": 1, "y": 2}, "b": 3}}
