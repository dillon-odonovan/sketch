import json

import pytest

from guild_config import (
    GuildConfig,
    StaticGuildConfigStore,
    parse_guild_config_json,
)


def _dump(obj: dict) -> str:
    return json.dumps(obj)


class TestParseGuildConfigJson:
    def test_single_guild_happy_path(self):
        raw = _dump({"123456789012345678": {"spreadsheet_id": "abc_DEF-123"}})
        result = parse_guild_config_json(raw)
        assert result == {
            123456789012345678: GuildConfig(spreadsheet_id="abc_DEF-123"),
        }

    def test_many_guilds(self):
        raw = _dump(
            {
                "111": {"spreadsheet_id": "sheet-A"},
                "222": {"spreadsheet_id": "sheet-B"},
                "333": {"spreadsheet_id": "sheet-C"},
            }
        )
        result = parse_guild_config_json(raw)
        assert set(result.keys()) == {111, 222, 333}
        assert result[111].spreadsheet_id == "sheet-A"

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            parse_guild_config_json("{not json")

    def test_non_object_top_level_raises(self):
        with pytest.raises(ValueError, match="must be a JSON object"):
            parse_guild_config_json("[]")

    def test_empty_object_raises(self):
        with pytest.raises(ValueError, match="empty"):
            parse_guild_config_json("{}")

    def test_non_numeric_guild_key_raises(self):
        raw = _dump({"not-a-snowflake": {"spreadsheet_id": "abc"}})
        with pytest.raises(ValueError, match="numeric string"):
            parse_guild_config_json(raw)

    def test_missing_spreadsheet_id_raises(self):
        raw = _dump({"123": {}})
        with pytest.raises(ValueError, match="missing a non-empty string"):
            parse_guild_config_json(raw)

    def test_empty_spreadsheet_id_raises(self):
        raw = _dump({"123": {"spreadsheet_id": ""}})
        with pytest.raises(ValueError, match="missing a non-empty string"):
            parse_guild_config_json(raw)

    def test_non_string_spreadsheet_id_raises(self):
        raw = _dump({"123": {"spreadsheet_id": 12345}})
        with pytest.raises(ValueError, match="missing a non-empty string"):
            parse_guild_config_json(raw)

    def test_non_object_guild_value_raises(self):
        raw = _dump({"123": "abc"})
        with pytest.raises(ValueError, match="must be an object"):
            parse_guild_config_json(raw)

    def test_unknown_per_guild_key_raises(self):
        raw = _dump({"123": {"spreadsheet_id": "abc", "typo_field": "x"}})
        with pytest.raises(ValueError, match="unknown key"):
            parse_guild_config_json(raw)

    def test_spreadsheet_id_disallowed_chars_raises(self):
        raw = _dump({"123": {"spreadsheet_id": "abc/../etc/passwd"}})
        with pytest.raises(ValueError, match="disallowed characters"):
            parse_guild_config_json(raw)

    def test_broadcast_channel_id_present(self):
        raw = _dump({
            "123": {
                "spreadsheet_id": "abc",
                "broadcast_channel_id": "987654321098765432",
            },
        })
        result = parse_guild_config_json(raw)
        assert result[123].broadcast_channel_id == 987654321098765432

    def test_broadcast_channel_id_absent_defaults_to_none(self):
        raw = _dump({"123": {"spreadsheet_id": "abc"}})
        result = parse_guild_config_json(raw)
        assert result[123].broadcast_channel_id is None

    def test_broadcast_channel_id_non_numeric_raises(self):
        raw = _dump({
            "123": {"spreadsheet_id": "abc", "broadcast_channel_id": "not-a-snowflake"},
        })
        with pytest.raises(ValueError, match="broadcast_channel_id"):
            parse_guild_config_json(raw)

    def test_broadcast_channel_id_non_string_raises(self):
        raw = _dump({
            "123": {"spreadsheet_id": "abc", "broadcast_channel_id": 987654321},
        })
        with pytest.raises(ValueError, match="broadcast_channel_id"):
            parse_guild_config_json(raw)

    def test_broadcast_channel_id_empty_string_raises(self):
        raw = _dump({
            "123": {"spreadsheet_id": "abc", "broadcast_channel_id": ""},
        })
        with pytest.raises(ValueError, match="broadcast_channel_id"):
            parse_guild_config_json(raw)


class TestStaticGuildConfigStore:
    def test_get_hit(self):
        store = StaticGuildConfigStore(
            {
                42: GuildConfig(spreadsheet_id="sheet-A"),
            }
        )
        assert store.get(42) == GuildConfig(spreadsheet_id="sheet-A")

    def test_get_miss(self):
        store = StaticGuildConfigStore(
            {
                42: GuildConfig(spreadsheet_id="sheet-A"),
            }
        )
        assert store.get(99) is None

    def test_configured_guild_ids(self):
        store = StaticGuildConfigStore(
            {
                1: GuildConfig(spreadsheet_id="A"),
                2: GuildConfig(spreadsheet_id="B"),
            }
        )
        assert sorted(store.configured_guild_ids()) == [1, 2]

    def test_empty_store(self):
        store = StaticGuildConfigStore({})
        assert store.get(1) is None
        assert store.configured_guild_ids() == []
