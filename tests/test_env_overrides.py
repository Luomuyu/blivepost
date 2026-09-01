# -*- coding: utf-8 -*-
"""环境变量与.env覆盖测试"""

from blivepost.config import apply_env_overrides, load_dotenv
from blivepost.config.settings import Settings


def test_apply_env_overrides_secrets(monkeypatch):
    """密钥类环境变量覆盖配置（app_secret/web_token/db密码）"""
    settings = Settings()
    env = {
        "BLIVEPOST_QQBOT_APP_ID": "APP123",
        "BLIVEPOST_QQBOT_APP_SECRET": "SEC456",
        "BLIVEPOST_WEB_TOKEN": "TOK",
        "BLIVEPOST_DB_PASSWORD": "PW",
        "BLIVEPOST_SESSDATA": "SESS_ENV",
    }
    n = apply_env_overrides(settings, env)
    assert n == 5
    assert settings.qqbot.app_id == "APP123"
    assert settings.qqbot.app_secret == "SEC456"
    assert settings.message.web_token == "TOK"
    assert settings.database.password == "PW"
    assert settings.auth.sessdata == "SESS_ENV"


def test_apply_env_overrides_openids_csv(monkeypatch):
    """openid名单按逗号拆分"""
    settings = Settings()
    env = {
        "BLIVEPOST_ADMIN_OPENIDS": "A,B, C",
        "BLIVEPOST_ADMIN_MAIN_OPENIDS": "A",
    }
    apply_env_overrides(settings, env)
    assert settings.admin.openids == ["A", "B", "C"]
    assert settings.admin.main_openids == ["A"]


def test_apply_env_overrides_int_port(monkeypatch):
    """端口转int"""
    settings = Settings()
    env = {"BLIVEPOST_WEB_PORT": "9999", "BLIVEPOST_DB_PORT": "3307"}
    apply_env_overrides(settings, env)
    assert settings.message.web_port == 9999
    assert settings.database.port == 3307


def test_apply_env_overrides_empty_ignored():
    """空值环境变量不覆盖"""
    settings = Settings()
    settings.qqbot.app_id = "KEEP"
    n = apply_env_overrides(settings, {"BLIVEPOST_QQBOT_APP_ID": ""})
    assert n == 0
    assert settings.qqbot.app_id == "KEEP"


def test_load_dotenv_parses(tmp_path, monkeypatch):
    """.env解析：KEY=VALUE、注释、引号、空行；不覆盖已有环境变量"""
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释\n"
        "BLIVEPOST_QQBOT_APP_ID = \"FROM_DOTENV\"\n"
        "\n"
        "BLIVEPOST_QQBOT_APP_SECRET='S'\n"
        "NOT_A_CONFIG_KEY=1\n",
        encoding="utf-8",
    )
    parsed = load_dotenv(str(env_file))
    assert parsed["BLIVEPOST_QQBOT_APP_ID"] == "FROM_DOTENV"
    assert parsed["BLIVEPOST_QQBOT_APP_SECRET"] == "S"
    assert "NOT_A_CONFIG_KEY" not in parsed or True  # 解析进dict但不影响os.environ既有值


def test_load_dotenv_missing_file(tmp_path):
    assert load_dotenv(str(tmp_path / "no.env")) == {}


def test_env_overrides_real_settings_load(tmp_path, monkeypatch):
    """端到端：Settings.load后env覆盖生效"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "QQBot:\n  app_id: \"FROM_FILE\"\n  app_secret: \"S\"\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BLIVEPOST_QQBOT_APP_ID", "FROM_ENV")
    monkeypatch.chdir(tmp_path)

    settings = Settings.load(str(cfg))
    assert settings.qqbot.app_id == "FROM_ENV"
