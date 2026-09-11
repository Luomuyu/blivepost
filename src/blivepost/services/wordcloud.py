# -*- coding: utf-8 -*-
"""
弹幕词云（jieba分词统计；jieba缺失时退回二元组统计）

输入弹幕文本列表，输出高频词列表（过滤停用词/单字/纯数字/URL等噪音），
供Web面板词云页渲染。
"""

import logging
import re
from collections import Counter
from typing import Iterable, List

from ..utils.logger import get_logger


logger = get_logger("wordcloud")

# 常用停用词与弹幕噪音（单字已被长度过滤，这里收双字及以上虚词/套话）
STOPWORDS = frozenset({
    "什么", "这个", "那个", "这么", "那么", "怎么", "这样", "那样", "还是", "就是",
    "但是", "可是", "然而", "因为", "所以", "如果", "虽然", "而且", "或者", "以及",
    "我们", "你们", "他们", "她们", "它们", "自己", "大家", "别人", "没有", "不是",
    "可以", "已经", "现在", "时候", "一下", "一样", "一直", "一定", "一样", "上次",
    "感觉", "觉得", "知道", "看到", "看见", "出来", "过来", "起来", "下去", "出去",
    "开始", "最后", "然后", "不过", "有些", "一点", "有点", "真的", "直接", "应该",
    "可能", "出现", "发现", "哈哈", "哈哈哈哈", "啊啊", "hhh", "hhhh",
    "直播", "直播间", "主播", "老铁", "兄弟", "哥们", "老板", "大哥", "大佬",
    "111", "222", "333", "666", "999", "233", "698", "520",
})

# URL/表情/纯符号过滤
_URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_NON_WORD_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2190-\u21FF\u2B00-\u2BFF]+")

try:  # jieba为可选优化依赖：缺失时退回二元组切分
    import jieba  # type: ignore
    _HAS_JIEBA = True
except ImportError:  # pragma: no cover
    jieba = None
    _HAS_JIEBA = False
# 静默jieba词典构建日志（"Building prefix dict..."等刷屏噪音）；
# 应用debug开关为Y时由Logger.apply_debug放开
logging.getLogger("jieba").setLevel(logging.WARNING)


def _extract_tokens(text: str, use_jieba: bool) -> Iterable[str]:
    """单条弹幕→词元序列（jieba切词或二元组；剔除URL/表情/纯符号）"""
    text = _URL_RE.sub(" ", _EMOJI_RE.sub(" ", text or ""))
    if use_jieba:
        yield from jieba.cut(text)
        return
    cleaned = re.sub(r"\s+", " ", text).strip()
    chars = [c for c in cleaned if not _NON_WORD_RE.match(c)]
    for i in range(len(chars) - 1):
        yield chars[i] + chars[i + 1]


def _is_noise(token: str) -> bool:
    """无意义词元：过短/纯数字/纯符号/停用词"""
    t = token.strip()
    if len(t) < 2 or len(t) > 20:
        return True
    if _NON_WORD_RE.match(t) or t.isdigit():
        return True
    return t.lower() in STOPWORDS


def build_wordcloud(texts: Iterable[str], top_n: int = 100) -> List[dict]:
    """
    弹幕文本→高频词列表

    Args:
        texts: 弹幕内容列表
        top_n: 返回前N个高频词

    Returns:
        list[dict]: [{"word": 词, "count": 次数}]（按次数降序）
    """
    if _HAS_JIEBA:
        try:
            counter: Counter = Counter()
            for text in texts:
                counter.update(t for t in _extract_tokens(text, True)
                               if not _is_noise(t))
            return [{"word": w, "count": c}
                    for w, c in counter.most_common(top_n)]
        except Exception as e:
            logger.error(f"jieba分词失败，退回二元组统计: {e}")
    counter2: Counter = Counter()
    for text in texts:
        counter2.update(t for t in _extract_tokens(text, False) if not _is_noise(t))
    return [{"word": w, "count": c} for w, c in counter2.most_common(top_n)]
