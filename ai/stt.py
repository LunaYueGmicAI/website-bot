"""
语音转文字(STT):用 OpenAI 的 Whisper。异步版(await),不阻塞事件循环。
回合制:一次进来一段录音,转成一段文字返回(不是实时流)。

为什么用 OpenAI 而不是 Groq:团队统一用同一把 OPENAI_API_KEY(LLM + STT 共用一把 key,
少一套凭据要管)。OpenAI 的转写端点和 Groq 接口几乎一致,换供应商只改客户端 + 默认模型名。
"""
import os

_client = None


def _client_lazy():
    # 懒加载:第一次真正要转写时才创建 OpenAI 异步客户端。
    # 好处:没配 OPENAI_API_KEY 时,只要不调用 transcribe(),整个服务照样能启动/测试。
    global _client
    if _client is None:
        from openai import AsyncOpenAI
        # timeout=30:转写比对话稍慢(尤其接近 60s 上限的录音),给足 30s;超时抛错由 /voice 兜底成""。
        # max_retries=2:429/5xx 自动退避重试。
        _client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=30, max_retries=2)
    return _client


async def transcribe(audio_bytes, filename="audio.webm", language=None):
    """
    把一段录音转成文字(异步)。

    参数:
      audio_bytes: 录音原始字节(webm/ogg/mp3/wav/m4a 都行)
      filename:    文件名(Whisper 靠扩展名判断格式;OpenAI 接受 tuple=(名, 字节))
      language:    可选,语言代码('en'/'zh'/'es');【多语言关键】不传=自动识别语种

    例:用户按住麦克风说"我想做2000个录音麦" → 前端录成 webm 传上来 →
        这里 await 出字符串 "我想做2000个录音麦"(中文说就出中文,英文说就出英文)。
        静音/听不清则返回 ""。
    """
    resp = await _client_lazy().audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=os.getenv("STT_MODEL", "whisper-1"),   # OpenAI 的转写模型(经典 whisper-1;可换 gpt-4o-transcribe)
        language=language,       # None = 让 Whisper 自动识别语种(多语言核心)
        temperature=0,           # 0 = 最忠实转写,不自由发挥
    )
    return (resp.text or "").strip()
