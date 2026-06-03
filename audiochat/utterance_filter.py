"""
发言过滤器 — 过滤会议中的无效发言

过滤三类噪声：
  1. 纯废话（"你好""早上好""嗯嗯"）—— 停用句匹配
  2. 太短没信息量（<5 字）—— 长度过滤
  3. 纯语气词/重复词 —— 正则检测

在 ASR 转录后、入 RAG 库前调用。
"""
from __future__ import annotations

import re

# ====== 停用句列表（精确匹配，全部小写比较）======
STOPWORD_UTTERANCES: set[str] = {
    # 问候/告别
    "你好", "您好", "早上好", "下午好", "晚上好",
    "大家好", "嗨", "hello", "hi",
    "再见", "拜拜", "拜", "辛苦了", "谢谢",
    "好的谢谢", "谢谢大家", "辛苦大家了",
    # 纯应答
    "好", "好的", "行", "行的", "可以", "没问题",
    "ok", "okay", "嗯", "嗯嗯", "啊", "哦", "哦哦",
    "对", "对对", "对对对", "是的", "是", "了解",
    "明白", "收到", "知道了", "懂了",
    # 开场/过渡废话
    "那我们开始吧", "咱们开始", "我们继续",
    "下一个", "接下来", "然后呢",
    "稍等", "等一下", "等等", "我想想",
    "这个怎么说呢", "就是说", "怎么说",
}

# ====== 纯语气词/重复词正则 ======
# 匹配全是语气词的句子：嗯嗯啊啊哦哦呃呃
_FILLER_PATTERN = re.compile(
    r"^[嗯啊哦呃呀吧了的嘛呢哈额唉诶噢哇嘿\s，。、！？,.!?\-]+$"
)

# 匹配连续重复：同一个字/词重复 3 次以上（如"对对对对对"）
_REPEAT_PATTERN = re.compile(r"^(.{1,4})\1{2,}[。！？\s]*$")


def is_noise(text: str, min_length: int = 5) -> bool:
    """判断一条发言是否为噪声，应被过滤。"""
    s = text.strip()
    if not s:
        return True

    # 长度过滤
    if len(s) < min_length:
        return True

    # 停用句精确匹配
    normalized = re.sub(r"[，。！？、\s]", "", s).lower()
    if normalized in STOPWORD_UTTERANCES:
        return True

    # 纯语气词
    if _FILLER_PATTERN.match(s):
        return True

    # 连续重复（"对对对对对""好好好好"）
    if _REPEAT_PATTERN.match(normalized):
        return True

    return False


def filter_utterances(utterances: list[dict]) -> list[dict]:
    """
    过滤发言列表，返回有效发言。

    每条 utterance 格式: {"speaker": "张三", "text": "...", "timestamp": ...}
    """
    return [u for u in utterances if not is_noise(u.get("text", ""))]


def filter_texts(texts: list[str]) -> list[str]:
    """简单版：过滤纯文本列表。"""
    return [t for t in texts if not is_noise(t)]
