import json

import pytest

from ledgercore import cli
from ledgercore.config import get_settings


def test_unreachable_database_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LEDGER_DATABASE_URL", "postgresql+asyncpg://x:x@127.0.0.1:1/none")
    monkeypatch.setattr("sys.argv", ["ledgercore-drift-check"])
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc:
            cli.main()
    finally:
        get_settings.cache_clear()
    assert exc.value.code == 2
    assert "error" in json.loads(capsys.readouterr().err)
