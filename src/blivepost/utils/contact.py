# -*- coding: utf-8 -*-
"""
平台消息发送者身份提取

QQ开放平台三种openid的区别：
  - union_openid: 用户在UnionID体系的唯一标识（跨群/场景一致，新消息可能携带）
  - member_openid: 群成员openid（同一用户在不同群不同）
  - user_openid: 单聊场景的用户openid

优先级：union_openid > member_openid/user_openid > id
"""


def extract_sender(author) -> str:
    """
    从消息author对象提取发送者标识

    优先union_openid（跨场景一致），回退member_openid/user_openid/id。

    Args:
        author: botpy消息的author对象（或原始dict）

    Returns:
        str: 发送者标识，无法提取返回空串
    """
    if author is None:
        return ""
    for attr in ("union_openid", "member_openid", "user_openid", "id"):
        value = getattr(author, attr, None)
        if value is None and isinstance(author, dict):
            value = author.get(attr)
        if value:
            return str(value)
    return ""
