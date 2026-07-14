"""
应用入口:创建 Flask 应用、配好跨域、注册路由、按需启动后台清理线程。

本文件保持"瘦":具体逻辑都在 core / ai / integrations / api 里,方便日后扩展。

本地开发:  python app.py
生产运行:  gunicorn -w 1 -b 0.0.0.0:$PORT app:app   (单 worker —— 原因见 DESIGN.md)
"""
import os

# ⚠️ 必须最先加载 .env:因为 core.sessions / api.routes 在"导入时"就会读环境变量,
#    晚了就读到默认值了(踩过的坑,见记忆 feedback_python-dotenv-import-order)。
from dotenv import load_dotenv
load_dotenv()

from flask import Flask
from flask_cors import CORS

from core import sessions
from api.routes import bp


def create_app():
    app = Flask(__name__)
    # 只允许 .env 里列出的来源(嵌了 widget 的官网)跨域调用本 API;没配就先放开(仅本地方便)。
    origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    CORS(app, resources={r"/*": {"origins": origins or "*"}})
    app.register_blueprint(bp)   # 挂上 api/routes.py 里那组端点
    return app


app = create_app()


if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG") == "1"
    # 后台 TTL 清理线程:只在"真正运行"时启动;debug 重载模式下不启动,
    # 否则重载器会把它杀掉(见记忆 feedback_flask-debug-threads)。
    if not debug:
        sessions.start_sweeper()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8090")), debug=debug)
