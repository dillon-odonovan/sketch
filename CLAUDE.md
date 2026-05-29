# CLAUDE.md

Project-level guidance for Claude Code sessions on this repository.

## Testing

### Use `unittest.mock` for test doubles — no hand-rolled fakes

Prefer `unittest.mock.MagicMock` and `AsyncMock` over hand-rolled dataclass stubs
or fake classes. They eliminate boilerplate and make assertions explicit:

```python
from unittest.mock import AsyncMock, MagicMock

# Discord interaction
interaction = MagicMock()
interaction.user.id = 111
interaction.guild_id = 222
interaction.followup.send = AsyncMock()
interaction.response.defer = AsyncMock()

# Google Sheets API chain (values read)
svc = MagicMock()
request = svc.spreadsheets.return_value.values.return_value.get.return_value
request.execute.return_value = {"values": [...]}

# Metadata chain (tab names)
svc.spreadsheets.return_value.get.return_value.execute.return_value = {...}

# Service class with spec (async methods auto-become AsyncMock)
sheets = AsyncMock(spec=SheetsClient)
sheets.delete_by_url.return_value = some_row
sheets.delete_by_url.side_effect = TeamNotFoundError(...)
```

Assert with mock introspection rather than inspecting hand-rolled call lists:

```python
sheets.delete_by_url.assert_called_once_with("Sheet Name", url)
sheets.invalidate_snapshot.assert_not_called()
interaction.followup.send.assert_called_once()
(content,), kwargs = interaction.followup.send.call_args
assert "expected text" in content
assert kwargs["ephemeral"] is True
```

**When to keep a custom stub:** Complex routing stubs with dict-keyed
dispatch logic (e.g. `_RoutingService` in `test_sheets_client.py`) are
fine to keep when a `MagicMock` equivalent would be harder to read.
