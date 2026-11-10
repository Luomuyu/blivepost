# -*- coding: utf-8 -*-
"""
QQ机器人指令面板管理脚本（修改/删除/查询详情）

依赖 scripts/panel_create.py 创建面板时保存的 panel_ids.json
（内含两个面板的panel_id与PanelItem指令定义）。

用法：
  python scripts/panel_manage.py query               查询两个面板详情
  python scripts/panel_manage.py modify c2c          按本文件定义修改单聊面板
  python scripts/panel_manage.py modify group        按本文件定义修改群聊面板
  python scripts/panel_manage.py delete c2c          删除单聊面板
  python scripts/panel_manage.py delete group        删除群聊面板
  python scripts/panel_manage.py delete all          删除全部面板

凭证来源同panel_create.py（环境变量或config.yaml）。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import requests  # noqa: E402

from panel_create import load_credentials, get_access_token  # noqa: E402

API_BASE = "https://api.sgroup.qq.com"
IDS_FILE = Path(__file__).resolve().parent / "panel_ids.json"

# ---------- 面板定义（修改此处的items后运行 modify 即可更新面板） ----------
# 与 panel_create.py 的定义保持一致（同步修改两处）；指令全集以
# blivepost/services/commands.py 与 admin_commands.py 为准；
# 平台上限：每面板最多 10 条（实测校准，勿按文档20条放行）；
# /绑定列表 指令保留但不上面板（与 /状态 职责重叠，为腾条目移出）。

GROUP_ITEMS = [
    {"type": "command", "name": "/帮助", "desc": "查看全部指令"},
    {"type": "command", "name": "/监控 ", "desc": "开始监控推送，补房间号"},
    {"type": "command", "name": "/取消监控 ", "desc": "取消监控推送，补房间号"},
    {"type": "command", "name": "/状态", "desc": "查看本群监控状态与绑定"},
    {"type": "command", "name": "/推送设置 ", "desc": "查看/修改本群推送开关，补房间号"},
    {"type": "command", "name": "/绑定动态 ", "desc": "订阅UP主动态，补UP主UID"},
    {"type": "command", "name": "/取消动态 ", "desc": "取消订阅UP主动态，补UID"},
    {"type": "command", "name": "/收入 ", "desc": "查询收入汇总，补房间号"},
    {"type": "command", "name": "/SC榜 ", "desc": "查询SC排行，补房间号"},
    {"type": "command", "name": "/汇总", "desc": "全部房间合计，可带周/月"},
]

C2C_ITEMS = [
    {"type": "command", "name": "/登录", "desc": "扫码登录B站账号"},
    {"type": "command", "name": "/账号", "desc": "查看账号池状态"},
    {"type": "command", "name": "/移除账号 ", "desc": "从账号池删除账号，补UID"},
    {"type": "command", "name": "/恢复账号 ", "desc": "恢复失效账号，补UID"},
    {"type": "command", "name": "/导出 ", "desc": "导出明细数据，补房间号"},
    {"type": "command", "name": "/体积", "desc": "查询数据库体积"},
    {"type": "command", "name": "/我", "desc": "查看你的openid"},
]

PANEL_ITEMS = {"c2c": C2C_ITEMS, "group": GROUP_ITEMS}
PANEL_REMARK = {"c2c": "BLivePost单聊指令面板", "group": "BLivePost群聊指令面板"}


def load_ids() -> dict:
    if not IDS_FILE.exists():
        print(f"错误：未找到 {IDS_FILE}，请先运行 panel_create.py 创建面板。")
        sys.exit(1)
    return json.loads(IDS_FILE.read_text(encoding="utf-8"))


def save_ids(ids: dict) -> None:
    IDS_FILE.write_text(json.dumps(ids, ensure_ascii=False, indent=2),
                        encoding="utf-8")


def auth_headers(token: str) -> dict:
    return {"Authorization": f"QQBot {token}",
            "Content-Type": "application/json"}


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    action = sys.argv[1].lower()
    scope = sys.argv[2].lower() if len(sys.argv) > 2 else None

    app_id, secret = load_credentials()
    if not app_id or not secret:
        print("错误：未配置凭证。")
        sys.exit(1)
    token = get_access_token(app_id, secret)
    headers = auth_headers(token)

    ids = load_ids()

    if action == "query":
        targets = [scope] if scope in ("c2c", "group") else ["c2c", "group"]
        for sc in targets:
            info = ids.get(sc) or {}
            pid = info.get("panel_id")
            if not pid:
                print(f"[{sc}] 未创建")
                continue
            resp = requests.get(f"{API_BASE}/v2/panels/{pid}",
                                headers=headers, timeout=15)
            print(f"[{sc}] panel_id={pid}")
            print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
        return

    if action == "modify":
        if scope not in ("c2c", "group"):
            print("用法: panel_manage.py modify c2c|group")
            sys.exit(1)
        info = ids.get(scope) or {}
        pid = info.get("panel_id")
        if not pid:
            print(f"错误：{scope}面板尚未创建（panel_ids.json无记录）")
            sys.exit(1)
        body = {
            "panel": {
                "items": PANEL_ITEMS[scope],
                "remark": PANEL_REMARK[scope],
                "version": int(info.get("version", 1)) + 1,
            }
        }
        resp = requests.put(f"{API_BASE}/v2/panels/{pid}",
                            json=body, headers=headers, timeout=15)
        print(f"修改{scope}面板响应: {resp.status_code} {resp.text}")
        return

    if action == "delete":
        if scope not in ("c2c", "group"):
            print("用法: panel_manage.py delete c2c|group|all")
            sys.exit(1)
        targets = ["c2c", "group"] if scope == "all" else [scope]
        for sc in targets:
            info = ids.get(sc) or {}
            pid = info.get("panel_id")
            if not pid:
                print(f"[{sc}] 未创建，跳过删除")
                continue
            resp = requests.delete(f"{API_BASE}/v2/panels/{pid}",
                                   headers=headers, timeout=15)
            print(f"删除{sc}面板响应: {resp.status_code} {resp.text}")
            if resp.status_code in (200, 204):
                ids.pop(sc, None)
        save_ids(ids)
        return

    print(f"未知操作: {action}（支持 query/modify/delete）")
    sys.exit(1)


if __name__ == "__main__":
    main()
