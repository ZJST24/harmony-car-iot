"""
语音链路体检（诊断脚本，不参与业务运行）
========================================
按项目惯例，密钥只从同目录 config.json 读取，本文件不硬编码任何凭证。

检查三件事：
  1. DeepSeek 文本接口能否连通、能否稳定输出 JSON
  2. MaaS 平台 OpenAI 兼容端点是否可用、模型列表里有没有目标 ASR 模型
  3. MaaS 的 /audio/transcriptions 能否接收 16kHz WAV（用合成音验证协议层）

用法：
  D:\\miniconda\\python.exe test_voice_api.py

注意：合成的正弦波不是人声，第 3 项返回空文本属正常，
      只要 HTTP 200 就说明端点/鉴权/音频格式这层是通的。
      要验证真实识别率，请换成自己录的一句 "向前开慢一点"。
"""

import io
import json
import math
import os
import struct
import wave

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

LLM = CFG.get("llm", {})
ASR = CFG.get("asr", {})


def mask(key: str) -> str:
    return (key[:6] + "..." + key[-4:]) if len(key) > 12 else "***"


def make_wav(path: str, seconds: float = 1.0, rate: int = 16000) -> str:
    """生成 16kHz / 单声道 / 16bit 的 WAV，用于验证 ASR 接口协议层"""
    frames = bytearray()
    for i in range(int(rate * seconds)):
        v = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
        frames += struct.pack("<h", v)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def probe_deepseek():
    print("\n[1] DeepSeek 文本接口")
    url = LLM["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": LLM["model"],
        "messages": [
            {"role": "system", "content": "你是车控指令解析器。只输出 JSON：{\"cmd\":\"FORWARD|BACK|LEFT|RIGHT|STOP|null\",\"speed\":0-100|null,\"confident\":bool}。无法判断时 cmd 为 null，禁止输出其它文字。"},
            {"role": "user", "content": "往前开慢一点"},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "max_tokens": 120,
    }
    try:
        r = requests.post(
            url,
            headers={"Authorization": "Bearer " + LLM["api_key"],
                     "Content-Type": "application/json"},
            json=payload, timeout=40,
        )
        print("  HTTP", r.status_code, "| key", mask(LLM["api_key"]), "| model", LLM["model"])
        if r.status_code == 200:
            content = r.json()["choices"][0]["message"]["content"]
            print("  输出:", content.strip())
            try:
                print("  JSON 解析:", json.loads(content))
            except json.JSONDecodeError as e:
                print("  !! 不是合法 JSON:", e)
        else:
            print("  !! 响应:", r.text[:400])
    except Exception as e:
        print("  !! 调用失败:", type(e).__name__, str(e)[:200])


def probe_models():
    print("\n[2] MaaS OpenAI 兼容端点 / 模型列表")
    url = ASR["base_url"].rstrip("/") + "/models"
    try:
        r = requests.get(url, headers={"Authorization": "Bearer " + ASR["api_key"]}, timeout=20)
        print("  HTTP", r.status_code, "| key", mask(ASR["api_key"]))
        if r.status_code == 200:
            ids = [m.get("id") for m in r.json().get("data", [])]
            print("  模型数:", len(ids))
            hit = [i for i in ids if "asr" in str(i).lower() or "audio" in str(i).lower()]
            print("  音频相关:", hit[:12] or "（列表里没有，但可能仍可直接调用）")
            print("  目标模型在列表里:", ASR["model"] in ids)
        else:
            print("  !! 响应:", r.text[:300])
    except Exception as e:
        print("  !! 调用失败:", type(e).__name__, str(e)[:200])


def probe_asr():
    print("\n[3] MaaS 语音识别 /audio/transcriptions（OpenAI 兼容格式）")
    wav_path = os.path.join(BASE_DIR, "_probe_tone.wav")
    make_wav(wav_path)
    url = ASR["base_url"].rstrip("/") + "/audio/transcriptions"
    try:
        with open(wav_path, "rb") as f:
            r = requests.post(
                url,
                headers={"Authorization": "Bearer " + ASR["api_key"]},
                files={"file": ("probe.wav", f, "audio/wav")},
                data={"model": ASR["model"]},
                timeout=60,
            )
        print("  HTTP", r.status_code, "| model", ASR["model"])
        print("  响应:", r.text[:400])
    except Exception as e:
        print("  !! 调用失败:", type(e).__name__, str(e)[:200])
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


def probe_dashscope_asr():
    """按参考代码走 dashscope SDK + WebSocket 通道（该 MaaS 平台不提供 /audio/transcriptions）"""
    print("\n[4] MaaS 语音识别（dashscope SDK / WebSocket 通道）")
    try:
        import dashscope
        from dashscope.audio.asr import Recognition
        from http import HTTPStatus
    except ImportError as e:
        print("  !! 缺少 dashscope SDK:", e)
        print("     安装：D:\\miniconda\\python.exe -m pip install dashscope")
        return

    wav_path = os.path.join(BASE_DIR, "_probe_tone.wav")
    make_wav(wav_path)
    dashscope.api_key = ASR["api_key"]
    dashscope.base_websocket_api_url = ASR["ws_url"]
    print("  ws 端点:", ASR["ws_url"])
    # 平台 /models 里没有 3.1-asr-flash-message，后两个是列表里确实存在的 ASR 模型，作为备选
    candidates = [ASR["model"], "qwen-audio-3.0-asr-flash", "fun-asr-flash-2026-06-15"]
    try:
        for m in candidates:
            try:
                rec = Recognition(model=m, format="wav", sample_rate=16000, callback=None)
                result = rec.call(wav_path)
                if result.status_code == HTTPStatus.OK:
                    print("  OK   |", m, "| 识别文本:", repr(result.get_sentence()))
                    break
                print("  FAIL |", m, "| code", result.status_code, "|", str(result.message)[:160])
            except Exception as e:
                print("  FAIL |", m, "|", type(e).__name__, str(e)[:160])
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)


if __name__ == "__main__":
    print("=" * 56)
    print(" 语音链路体检（密钥来自 config.json，不打印完整值）")
    print("=" * 56)
    if not LLM.get("api_key") or not ASR.get("api_key"):
        raise SystemExit("config.json 缺少 llm.api_key 或 asr.api_key")
    probe_deepseek()
    probe_models()
    probe_asr()
    probe_dashscope_asr()
    print("\n完成。合成正弦波不是人声，识别文本为空属正常；看 HTTP / status_code 判断链路是否通。")
