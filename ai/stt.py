"""
语音转文字(STT):用 Groq 的 Whisper。异步版(await),不阻塞事件循环。
回合制:一次进来一段录音,转成一段文字返回(不是实时流)。
"""
import os

_client = None


def _client_lazy():
    # 懒加载:第一次真正要转写时才创建 Groq 异步客户端。
    # 好处:没配 GROQ_API_KEY 时,只要不调用 transcribe(),整个服务照样能启动/测试。
    global _client
    if _client is None:
        from groq import AsyncGroq
        _client = AsyncGroq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


async def transcribe(audio_bytes, filename="audio.webm", language=None):
    """
    把一段录音转成文字(异步)。

    参数:
      audio_bytes: 录音原始字节(webm/ogg/mp3/wav/m4a 都行)
      filename:    文件名(Whisper 靠扩展名判断格式)
      language:    可选,语言代码('en'/'zh'/'es');【多语言关键】不传=自动识别语种

    例:用户按住麦克风说"我想做2000个录音麦" → 前端录成 webm 传上来 →
        这里 await 出字符串 "我想做2000个录音麦"(中文说就出中文,英文说就出英文)。
        静音/听不清则返回 ""。
    """
    resp = await _client_lazy().audio.transcriptions.create(
        file=(filename, audio_bytes),
        model=os.getenv("STT_MODEL", "whisper-large-v3"),
        language=language,       # None = 让 Whisper 自动识别语种(多语言核心)
        temperature=0,           # 0 = 最忠实转写,不自由发挥
    )
    return (resp.text or "").strip()
