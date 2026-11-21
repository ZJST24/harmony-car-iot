"""
自然语言控车链路（可插拔模块，由 app.py 的 /voice_command 调用）
====================================================================
三段式设计，出错时能立刻定位是哪一段的问题：

  1. ASR   音频 → 文本     dashscope SDK + MaaS WebSocket（qwen-audio-3.1-asr-flash-message）
  2. LLM   文本 → JSON     DeepSeek OpenAI 兼容接口（deepseek-flash，JSON Mode）
  3. 裁决  JSON → 规范指令  纯代码：命令白名单 + 参数范围 + 置信度

安全约束：
  - 大模型的输出绝不直接执行，必须过 validate() 变成 {"cmd","speed"} 才交给 app.py
  - 急停等安全动作不经过本模块，保留物理/按钮通道
  - 凭证只从 config.json 注入，本文件不硬编码任何密钥
  - 本模块不碰华为云，只产出规范指令，下发仍复用 /command 的既有逻辑

依赖：requests、dashscope（pip install dashscope）
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from http import HTTPStatus

import requests

ALLOWED_CMDS = ("FORWARD", "BACK", "LEFT", "RIGHT", "STOP")
SPEED_CMDS = ("FORWARD", "BACK")

SYSTEM_PROMPT = """你是寻检车的指令解析器，把用户的自然语言翻译为一条结构化控制指令。

只允许五个动作：FORWARD(前进) BACK(后退) LEFT(左转) RIGHT(右转) STOP(停车/停下/急停)。
只输出一个 JSON 对象，不要输出解释文字，不要输出 Markdown 代码块：
{"cmd":"FORWARD|BACK|LEFT|RIGHT|STOP|null","speed":0-100或null,"confident":true或false}

规则：
- FORWARD / BACK 必须给 speed，取 0~100 的整数；"慢一点"约 30，"快一点"约 80，默认 60。
- LEFT / RIGHT / STOP 的 speed 一律为 null。
- 无法明确判断、与开车无关、或一句话里包含多个动作时，cmd 为 null、confident 为 false，禁止猜测。
- 语音识别可能有同音错字，按最合理的车控意图理解；但不得据此臆造指令。
"""


class VoiceChainError(Exception):
    """带阶段标记的错误，便于前端显示失败发生在哪一段。"""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message


class VoiceChain:
    def __init__(self, cfg: dict, log=None):
        self.llm = cfg.get("llm") or {}
        self.asr = cfg.get("asr") or {}
        self.voice = cfg.get("voice") or {}
        self.log = log or logging.getLogger("voice")
        # dashscope 用模块级全局配置，并发调用会互相串扰，这里串行化
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 1. ASR
    def transcribe(self, wav_path: str) -> str:
        """16kHz / 单声道 / 16bit WAV → 中文文本"""
        if not self.asr.get("api_key"):
            raise VoiceChainError("asr", "config.json 未配置 asr.api_key")
        try:
            import dashscope
            from dashscope.audio.asr import Recognition
        except ImportError as e:
            raise VoiceChainError("asr", "缺少 dashscope SDK，请执行 pip install dashscope") from e

        with self._lock:
            dashscope.api_key = self.asr["api_key"]
            dashscope.base_websocket_api_url = self.asr.get(
                "ws_url", "wss://dashscope.aliyuncs.com/api-ws/v1/inference")
            recognition = Recognition(
                model=self.asr.get("model", "qwen-audio-3.1-asr-flash-message"),
                format="wav", sample_rate=16000, callback=None)
            result = recognition.call(wav_path)

        if result.status_code != HTTPStatus.OK:
            raise VoiceChainError("asr", f"ASR 返回 {result.status_code}：{result.message}")
        return self._extract_text(result.get_sentence())

    @staticmethod
    def _extract_text(sentence) -> str:
        """SDK 不同版本返回形态不同：str / dict / list[dict]，统一压成文本"""
        if isinstance(sentence, str):
            return sentence.strip()
        if isinstance(sentence, dict):
            return str(sentence.get("text", "")).strip()
        if isinstance(sentence, list):
            return "".join(str(x.get("text", "")) for x in sentence if isinstance(x, dict)).strip()
        return ""

    # ------------------------------------------------------------ 2. LLM
    def parse(self, text: str) -> dict:
        """自然语言文本 → 大模型输出的 JSON 对象（此处还不代表可执行）"""
        if not self.llm.get("api_key"):
            raise VoiceChainError("llm", "config.json 未配置 llm.api_key")
        url = self.llm.get("base_url", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        payload = {
            "model": self.llm.get("model", "deepseek-flash"),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 150,
        }
        try:
            resp = requests.post(
                url,
                headers={"Authorization": "Bearer " + self.llm["api_key"],
                         "Content-Type": "application/json"},
                json=payload, timeout=40,
            )
        except requests.RequestException as e:
            raise VoiceChainError("llm", f"调用大模型失败：{e}") from e
        if resp.status_code != 200:
            raise VoiceChainError("llm", f"大模型返回 {resp.status_code}：{resp.text[:180]}")
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as e:
            raise VoiceChainError("llm", f"大模型响应格式异常：{resp.text[:180]}") from e
        return self._load_json(content)

    @staticmethod
    def _load_json(content: str) -> dict:
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip()).strip()
        try:
            obj = json.loads(s)
        except json.JSONDecodeError as e:
            raise VoiceChainError("llm", f"大模型未输出合法 JSON：{s[:120]}") from e
        if not isinstance(obj, dict):
            raise VoiceChainError("llm", "大模型输出不是 JSON 对象")
        return obj

    # ------------------------------------------------------------ 3. 裁决
    @staticmethod
    def validate(parsed: dict):
        """纯代码裁决：返回 (规范指令, 错误信息)。AI 输出在这里才会变成可执行命令"""
        cmd = parsed.get("cmd")
        if not isinstance(cmd, str) or cmd.upper() not in ALLOWED_CMDS:
            return None, "意图不明确或不属于允许的动作，请重说"
        cmd = cmd.upper()
        if parsed.get("confident") is False:
            return None, "大模型标记为不确定，拒绝执行"
        speed = parsed.get("speed")
        if cmd in SPEED_CMDS:
            if isinstance(speed, bool) or not isinstance(speed, int) or not 0 <= speed <= 100:
                return None, "前进/后退的速度必须是 0~100 的整数"
            return {"cmd": cmd, "speed": speed}, None
        return {"cmd": cmd}, None

    # ------------------------------------------------------------ 编排
    def run(self, text: str = None, wav_path: str = None) -> dict:
        """跑完整链路。返回结构化结果，绝不抛异常到 Flask 层。"""
        out = {
            "ok": False, "text": "", "parsed": None, "command": None,
            "stage": "", "error": None, "timings": {},
        }
        try:
            if wav_path:
                out["stage"] = "asr"
                t0 = time.time()
                text = self.transcribe(wav_path)
                out["timings"]["asr_ms"] = int((time.time() - t0) * 1000)
                if not text:
                    raise VoiceChainError("asr", "没有识别到语音内容，请靠近麦克风再说一次")

            out["text"] = (text or "").strip()
            if not out["text"]:
                raise VoiceChainError("input", "没有可解析的文本")

            out["stage"] = "llm"
            t0 = time.time()
            parsed = self.parse(out["text"])
            out["timings"]["llm_ms"] = int((time.time() - t0) * 1000)
            out["parsed"] = parsed

            out["stage"] = "validate"
            command, err = self.validate(parsed)
            if err:
                raise VoiceChainError("validate", err)

            out["command"] = command
            out["ok"] = True
            out["stage"] = ""
            self.log.info("语音指令 -> %s | %s -> %s", out["text"], parsed, command)
        except VoiceChainError as e:
            out["error"] = e.message
            out["stage"] = e.stage
            self.log.info("语音指令在 %s 阶段被拒：%s（原文 %r）", e.stage, e.message, out["text"])
        return out
