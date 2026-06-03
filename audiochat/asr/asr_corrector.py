"""
ASR 后校正模块 — 基于 Qwen 的轻量级纠错

核心思想：
  ASR 输出的低置信度词（如口语化表达、专有名词）可能出错，
  用小模型（如 Qwen2.5-0.5B）做 prompt 纠错，不依赖 token 级别对齐语料。

工作流程：
  1. FunASR 原始转写
  2. 对低置信度句子做 Prompt 纠错（Qwen 0.5B，本地推理，< 2s）
  3. 输出更准确的文本

使用示例：
    from audiochat.asr.asr_corrector import ASRPostCorrector
    
    corrector = ASRPostCorrector(model_path="/data/models/Qwen2.5-0.5B")
    corrected = corrector.correct("张三说的 APMI 已完成")
    # → "张三说的 API 已完成"
"""

from __future__ import annotations

from typing import Optional


class ASRPostCorrector:
    """
    基于 Prompt 的 ASR 后校正器

    特点：
      - 无需 token 级别对齐语料
      - 用小模型（0.5B），推理延迟 < 2s
      - 可选启用，关闭时零额外开销
    """

    def __init__(
        self,
        model_path: str = "Qwen/Qwen2.5-0.5B-Instruct",
        device: str = "cuda",
        enabled: bool = False,
    ):
        """
        Args:
            model_path: 校正模型路径
            device: 运行设备
            enabled: 是否启用校正（关闭时 correct() 直接返回原文本）
        """
        self.enabled = enabled
        self._model = None
        self._tokenizer = None
        self._model_path = model_path
        self._device = device

    def _lazy_load(self):
        """懒加载模型（首次调用时才加载）"""
        if self._model is not None:
            return

        try:
            from transformers import AutoModelForCausalLM, AutoTokenizer
            import torch

            self._tokenizer = AutoTokenizer.from_pretrained(
                self._model_path,
                trust_remote_code=True,
            )
            self._model = AutoModelForCausalLM.from_pretrained(
                self._model_path,
                torch_dtype=torch.float16,
                device_map=self._device,
                trust_remote_code=True,
            )
            self._model.eval()
            print(f"[ASR 校正] 模型加载完成：{self._model_path}")
        except Exception as e:
            print(f"[ASR 校正] 模型加载失败：{e}，校正功能已关闭")
            self.enabled = False

    def correct(self, text: str, verbose: bool = False) -> str:
        """
        校正单条 ASR 文本

        Args:
            text: ASR 原始输出
            verbose: 是否打印校正前后对比

        Returns:
            校正后的文本
        """
        if not self.enabled:
            return text

        self._lazy_load()

        prompt = self._build_correction_prompt(text)
        response = self._generate(prompt)

        if verbose:
            print(f"[ASR 校正] 原文：{text}")
            print(f"[ASR 校正] 纠后：{response}")

        return response

    def correct_batch(self, texts: list[str], verbose: bool = False) -> list[str]:
        """
        批量校正多条 ASR 文本

        Args:
            texts: ASR 原始输出列表
            verbose: 是否打印校正前后对比

        Returns:
            校正后的文本列表
        """
        if not self.enabled:
            return texts

        return [self.correct(t, verbose=verbose) for t in texts]

    def _build_correction_prompt(self, text: str) -> str:
        """
        构建校正 Prompt

        核心策略：明确告诉模型 ASR 常见错误模式，让它做最小修改
        """
        return f"""以下是一段语音识别(ASR)转写的文本，可能存在少量错别字、口语化表达
或不准确的专有名词。请在不改变原意的前提下，只修正明显的错别字和口语化表达，
保持原句的结构和长度，不要过度改写。

ASR原文：{text}

纠错后："""

    def _generate(self, prompt: str) -> str:
        """调用模型生成校正结果"""
        if self._model is None or self._tokenizer is None:
            return prompt.split("纠错后：")[-1].strip()

        from transformers import AutoTokenizer
        import torch

        messages = [{"role": "user", "content": prompt}]
        text = self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
        inputs = self._tokenizer([text], return_tensors="pt").to(self._device)

        with torch.no_grad():
            outputs = self._model.generate(
                **inputs,
                max_new_tokens=128,
                temperature=0.3,
                do_sample=False,
                pad_token_id=self._tokenizer.eos_token_id,
            )

        response = self._tokenizer.decode(
            outputs[0][inputs["input_ids"].shape[1]:],
            skip_special_tokens=True,
        )
        return response.strip()

    def enable(self):
        """启用校正器"""
        self.enabled = True

    def disable(self):
        """禁用校正器（零额外开销）"""
        self.enabled = False

    @property
    def is_enabled(self) -> bool:
        return self.enabled
