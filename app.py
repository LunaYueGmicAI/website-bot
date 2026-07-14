"""
应用入口:创建 FastAPI 应用、配跨域、挂路由、启动后台清理任务。本文件保持"瘦"。

为什么用 FastAPI(异步):本 bot 每个请求大部分时间在等外部 API(STT/LLM/Slack),
属于 I/O 密集型。异步能让单个进程在"等 A 返回"时去处理 B、C,同样资源扛更多并发,
而且单进程 → 我们那个"内存里的会话字典"天然成立(不用担心多进程各存一份)。

本地开发:  uvicorn app:app --reload --port 8090
生产运行:  uvicorn app:app --host 0.0.0.0 --port 8090   (单进程即可,async 单进程就扛得住)
"""
import os
import asyncio
from contextlib import asynccontextmanager

# ⚠️ 必须最先加载 .env:因为 core/api 在"导入时"就会读环境变量,晚了就读到默认值
#    (踩过的坑,见记忆 feedback_python-dotenv-import-order)。
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core import sessions
from api.routes import router


@asynccontextmanager
async def lifespan(app):
    """
    应用的启动/关闭钩子。
    启动:拉起后台 TTL 清理协程(每 5 分钟扫一次过期会话)。
      例:某会话 40 分钟没人说话 > TTL 30 分钟 → 下次扫到就从内存删掉(Slack 归档不动)。
    关闭:取消这个协程。
    """
    task = asyncio.create_task(sessions.run_sweeper(interval=300))
    yield
    task.cancel()


app = FastAPI(title="GMIC Website Bot", lifespan=lifespan)

# 只允许 .env 里列出的来源(嵌 widget 的官网)跨域调用本 API;没配就先放开(仅本地方便)。
# 例:ALLOWED_ORIGINS="https://gmic.ai,https://www.gmic.ai"
_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂上 api/routes.py 里那组端点
app.include_router(router)


if __name__ == "__main__":
    # 方便本地直接 `python app.py` 起服务;生产建议直接用 uvicorn 命令(见文件顶部)。
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8090")), reload=False)
