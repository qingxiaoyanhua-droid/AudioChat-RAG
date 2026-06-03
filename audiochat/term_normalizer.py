"""
企业术语表 — ASR 输出纠错

ASR 常见错误：专业术语、英文缩写被识别成同音/近音中文。
在文本进入 RAG 检索或 LLM 总结之前，先做一轮规则替换。

用法：
    from audiochat.term_normalizer import normalize_terms
    clean_text = normalize_terms("开皮爱指标没达标")
    # -> "KPI指标没达标"
"""
from __future__ import annotations

import re
from typing import Optional

# ====== 术语映射表 ======
# 格式: ASR 常见错误写法 -> 正确术语
# 按长度降序排列，优先匹配长词避免误替换
TERM_MAP: dict[str, str] = {
    # 英文缩写类
    "开皮爱": "KPI",
    "阿皮爱": "API",
    "欧凯啊": "OKR",
    "西啊了": "CI/CD",
    "诶皮爱": "API",
    "屁皮踢": "PPT",
    "皮地爱夫": "PDF",
    "优啊了": "URL",
    "埃斯低开": "SDK",
    "杰森": "JSON",
    "诶奇踢踢皮": "HTTP",
    "艾奇踢踢皮": "HTTP",
    "埃斯克优尔": "SQL",
    "西诶欧": "CEO",
    "西踢欧": "CTO",
    "屁爱姆": "PM",
    "啊而迪": "RD",
    "丘诶": "QA",

    # 技术术语类
    "拉个": "RAG",
    "拉哥": "RAG",
    "瑞格": "RAG",
    "洛拉": "LoRA",
    "劳拉": "LoRA",
    "皮皮欧": "PPO",
    "低皮欧": "DPO",
    "基皮欧": "GRPO",
    "大模型": "大模型",  # 保持不变，占位防误替换
    "千问": "千问",
    "吉皮踢": "GPT",
    "伯特": "BERT",
    "变压器": "Transformer",
    "注意力机制": "注意力机制",

    # 业务术语类（根据公司实际情况扩展）
    "迭代": "迭代",
    "需求评审": "需求评审",
    "代码审查": "Code Review",
    "上线": "上线",
    "灰度": "灰度发布",
    "回滚": "回滚",
    "容器": "容器",
    "码头工人": "Docker",
    "库伯奈踢斯": "Kubernetes",
    "K八S": "K8s",
}

# 预编译正则：按 key 长度降序，优先匹配长词
_PATTERN: Optional[re.Pattern] = None


def _build_pattern() -> re.Pattern:
    global _PATTERN
    if _PATTERN is None:
        sorted_keys = sorted(TERM_MAP.keys(), key=len, reverse=True)
        escaped = [re.escape(k) for k in sorted_keys]
        _PATTERN = re.compile("|".join(escaped))
    return _PATTERN


def normalize_terms(text: str) -> str:
    """对文本做术语替换，返回纠正后的文本。"""
    pattern = _build_pattern()
    return pattern.sub(lambda m: TERM_MAP[m.group()], text)


def add_terms(new_terms: dict[str, str]) -> None:
    """运行时动态添加术语映射（热更新）。"""
    global _PATTERN
    TERM_MAP.update(new_terms)
    _PATTERN = None  # 下次调用时重新编译


def normalize_batch(texts: list[str]) -> list[str]:
    """批量替换。"""
    return [normalize_terms(t) for t in texts]
