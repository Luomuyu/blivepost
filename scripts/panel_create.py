# -*- coding: utf-8 -*-
"""
QQ机器人指令面板创建脚本（仅需运行一次）

创建两个指令面板：
  - 单聊面板（scope=c2c）：管理员私聊指令
  - 群聊面板（scope=group）：群聊指令

创建成功后把返回的 panel_id 与面板定义写入 scripts/panel_ids.json，
供 scripts/panel_manage.py（修改/删除/查询）使用。

凭证来源（二选一）：
  - 环境变量 BLIVEPOST_QQBOT_APP_ID / BLIVEPOST_QQBOT_APP_SECRET
  - 项目根目录 config.yaml 的 QQBot 段

用法：
  python scripts/panel_create.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import requests  # noqa: E402

TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
PANEL_URL = "https://api.sgroup.qq.com/connect/panel/create"
IDS_FILE = Path(__file__).resolve().parent / "panel_ids.json"

# ---------------------------------------------------------------- 面板定义

GROUP_ITEMS = [
    {"type": "command", "name": "/帮助", "desc": "查看全部指令"},
    {"type": "command", "name": "/绑定 ", "desc": "绑定本群与直播间，补房间号"},
    {"type": "command", "name": "/监控 ", "desc": "开始监控推送，补房间号"},
    {"type": "command", "name": "/取消监控 ", "desc": "取消监控推送，补房间号"},
    {"type": "command", "name": "/解绑 ", "desc": "解除绑定，补房间号"},
    {"type": "command", "name": "/状态", "desc": "查看本群监控状态"},
    {"type": "command", "name": "/我的绑定", "desc": "查看本群绑定列表"},
    {"type": "command", "name": "/收入 ", "desc": "查询收入汇总，补房间号"},
    {"type": "command", "name": "/SC榜 ", "desc": "查询SC排行，补房间号"},
    {"type": "command", "name": "/汇总", "desc": "全部房间合计"},
]

C2C_ITEMS = [
    {"type": "command", "name": "/登录", "desc": "扫码登录B站账号"},
    {"type": "command", "name": "/账号", "desc": "查看账号池状态"},
    {"type": "command", "name": "/移除账号 ", "desc": "从账号池删除账号，补UID"},
    {"type": "command", "name": "/恢复账号 ", "desc": "恢复失效账号，补UID"},
    {"type": "command", "name": "/导出 ", "desc": "导出明细数据，补房间号"},
    {"type": "command", "name": "/我", "desc": "查看你的openid"},
]

PANELS = [
    {"scope": "c2c", "target_type": "all", "items": C2C_ITEMS,
     "remark": "BLivePost单聊指令面板"},
    {"scope": "group", "target_type": "all", "items": GROUP_ITEMS,
     "remark": "BLivePost群聊指令面板"},
]


def load_credentials():
    """从环境变量或config.yaml读取appid/secret"""
    app_id = os.environ.get("BLIVEPOST_QQBOT_APP_ID", "")
    secret = os.environ.get("BLIVEPOST_QQBOT_APP_SECRET", "")
    if app_id and secret:
        return app_id, secret

    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            qq = data.get("QQBot") or {}
            app_id = app_id or str(qq.get("app_id", ""))
            secret = secret or str(qq.get("app_secret", ""))
        except Exception as e:
            print(f"读取config.yaml失败: {e}")
    return app_id, secret


def get_access_token(app_id: str, secret: str) -> str:
    """获取访问凭证（对齐官方access-token文档）"""
    resp = requests.post(
        TOKEN_URL,
        json={"appId": app_id, "clientSecret": secret},
        timeout=15,
    )
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"获取access_token失败: {data}")
    print(f"已获取access_token（有效期{data.get('expires_in')}秒）")
    return data["access_token"]


def create_panel(token: str, panel_spec: dict) -> str:
    """创建单个指令面板，返回panel_id"""
    body = {
        "scope": panel_spec["scope"],
        "target_type": panel_spec.get("target_type", "all"),
        "panel": {
            "items": panel_spec["items"],
            "remark": panel_spec.get("remark", ""),
            "version": 1,
        },
    }
    resp = requests.post(
        PANEL_URL,
        json=body,
        headers={
            "Authorization": f"QQBot {token}",
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    data = resp.json()
    panel_id = data.get("panel_id")
    if not panel_id:
        raise RuntimeError(f"创建面板失败（scope={panel_spec['scope']}）: {data}")
    print(f"已创建{panel_spec['scope']}面板 panel_id={panel_id}")
    return panel_id


def main():
    app_id, secret = load_credentials()
    if not app_id or not secret:
        print("错误：未配置凭证。请设置环境变量 BLIVEPOST_QQBOT_APP_ID / "
              "BLIVEPOST_QQBOT_APP_SECRET，或在config.yaml的QQBot段填写。")
        sys.exit(1)

    token = get_access_token(app_id, secret)

    saved = {}
    if IDS_FILE.exists():
        saved = json.loads(IDS_FILE.read_text(encoding="utf-8"))

    for spec in PANELS:
        scope = spec["scope"]
        if scope in saved and saved[scope].get("panel_id"):
            print(f"{scope}面板已存在（panel_id={saved[scope]['panel_id']}），跳过创建；"
                  f"如需重建请先运行 panel_manage.py delete")
            continue
        panel_id = create_panel(token, spec)
        saved[scope] = {"panel_id": panel_id, "items": spec["items"],
                        "remark": spec.get("remark", "")}

    IDS_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"面板ID已保存到 {IDS_FILE}")


if __name__ == "__main__":
    main()
