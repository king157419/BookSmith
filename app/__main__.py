"""python -m app：读取 .env / 环境变量，用 uvicorn 启动（单进程，内置任务队列线程）。"""
import logging
import os

import uvicorn

from .config import Settings, load_dotenv
from .main import create_app


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    host = os.environ.get("HOST") or "0.0.0.0"
    # workers 必须为 1：任务队列线程跑在应用进程里
    uvicorn.run(create_app(settings), host=host, port=settings.port, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS") or "127.0.0.1")


if __name__ == "__main__":
    main()
