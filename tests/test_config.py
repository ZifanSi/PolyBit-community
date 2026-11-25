import pytest

from polybit.config import ConfigError, Settings
from polybit.money import pb


def test_defaults_are_valid():
    Settings().validate()


def test_token_required_only_for_run():
    Settings().validate(require_token=False)
    with pytest.raises(ConfigError, match="PB_DISCORD_TOKEN"):
        Settings().validate(require_token=True)


@pytest.mark.parametrize(
    "overrides, needle",
    [
        ({"transfer_fee_bps": -1}, "PB_TRANSFER_FEE_BPS"),
        ({"transfer_fee_bps": 10_001}, "PB_TRANSFER_FEE_BPS"),
        ({"signup_grant": -pb(1)}, "PB_SIGNUP_GRANT"),
        ({"daily_cooldown_hours": 0}, "PB_DAILY_COOLDOWN_HOURS"),
        ({"daily_cooldown_hours": 50, "daily_streak_window_hours": 48}, "STREAK_WINDOW"),
        ({"pay_confirm_threshold": 0}, "PB_PAY_CONFIRM_THRESHOLD"),
        ({"admin_ids": frozenset({-5})}, "PB_ADMIN_IDS"),
    ],
)
def test_out_of_range_settings_rejected(overrides, needle):
    with pytest.raises(ConfigError, match=needle):
        Settings(**overrides).validate()


def test_all_problems_reported_together():
    with pytest.raises(ConfigError) as exc:
        Settings(transfer_fee_bps=-1, signup_grant=-1).validate()
    assert "PB_TRANSFER_FEE_BPS" in str(exc.value) and "PB_SIGNUP_GRANT" in str(exc.value)


def test_unparsable_env_names_the_variable(monkeypatch):
    monkeypatch.setenv("PB_TRANSFER_FEE_BPS", "lots")
    monkeypatch.setenv("PB_DAILY_AMOUNT", "abc")
    with pytest.raises(ConfigError) as exc:
        Settings.from_env()
    assert "PB_TRANSFER_FEE_BPS" in str(exc.value) and "PB_DAILY_AMOUNT" in str(exc.value)


@pytest.mark.parametrize("name", ["bad name", "1abc", "pkg..mod", "pkg;rm"])
def test_bad_extension_names_rejected(name):
    with pytest.raises(ConfigError, match="PB_EXTENSIONS"):
        Settings(extensions=(name,)).validate()


def test_extensions_env(monkeypatch):
    monkeypatch.setenv("PB_EXTENSIONS", "acme.polybit_ext, other_ext ,")
    s = Settings.from_env()
    assert s.extensions == ("acme.polybit_ext", "other_ext")
    s.validate()


def test_env_values_parsed(monkeypatch):
    monkeypatch.setenv("PB_ADMIN_IDS", "1, 2,3")
    monkeypatch.setenv("PB_DAILY_AMOUNT", "12.5")
    monkeypatch.setenv("PB_DEV_GUILD_ID", "")
    s = Settings.from_env()
    assert s.admin_ids == {1, 2, 3}
    assert s.daily_amount == pb("12.5")
    assert s.dev_guild_id is None
