# -*- coding: utf-8 -*-
"""WBI签名测试"""

from blivepost.services.wbi import WbiSigner, wbi_sign, derive_wbi_key, MIXIN_KEY_ENC_TAB


def test_mixin_table_covers_all_indices():
    """混淆表应为0-63的双射排列"""
    assert sorted(MIXIN_KEY_ENC_TAB) == list(range(64))


def test_derive_wbi_key():
    """密钥派生：img_key + sub_key 按混淆表重排取前32位"""
    img_key = "7cd084941338484aae1ad9425b84077c"
    sub_key = "4932caff0ff746eab6f01bf08b70ac45"
    mixed = img_key + sub_key
    expected = "".join(mixed[i] for i in MIXIN_KEY_ENC_TAB)[:32]

    assert derive_wbi_key(f"https://i0.hdslb.com/bfs/wbi/{img_key}.png",
                          f"https://i0.hdslb.com/bfs/wbi/{sub_key}.png") == expected
    assert len(expected) == 32


def test_wbi_sign_deterministic():
    """签名应确定且为32位hex"""
    key = "abcdef1234567890abcdef1234567890"
    s1 = wbi_sign("mid=1&platform=web", key, wts="1700000000")
    s2 = wbi_sign("mid=1&platform=web", key, wts="1700000000")
    s3 = wbi_sign("mid=2&platform=web", key, wts="1700000000")

    assert s1 == s2
    assert s1 != s3
    assert len(s1) == 32
    int(s1, 16)  # 合法hex


def test_wbi_sign_empty_key():
    """密钥为空时签名返回空串"""
    assert wbi_sign("a=1", "") == ""


def test_wbi_signer_cached_key(monkeypatch):
    """签名器应缓存密钥，密钥有效期内不重复发HTTP请求"""
    import time
    from blivepost.services import wbi as wbi_mod

    calls = []

    class FakeResponse:
        def json(self):
            return {"data": {"wbi_img": {
                "img_url": "https://i0.hdslb.com/bfs/wbi/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png",
                "sub_url": "https://i0.hdslb.com/bfs/wbi/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.png",
            }}}

    def fake_get(*args, **kwargs):
        calls.append(1)
        return FakeResponse()

    monkeypatch.setattr(wbi_mod.requests, "get", fake_get)

    signer = WbiSigner()
    k1 = signer.get_w_key()
    k2 = signer.get_w_key()
    assert k1 == k2 and len(k1) == 32
    assert len(calls) == 1

    # 强制刷新会重新请求
    signer.get_w_key(force_refresh=True)
    assert len(calls) == 2

    # 过期后重新请求
    signer._w_key_expire = time.time() - 1
    signer.get_w_key()
    assert len(calls) == 3
