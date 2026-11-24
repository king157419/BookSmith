"""python -m app：读取 .env / 环境变量，用 uvicorn 启动（单进程，内置任务队列线程）。"""
import logging
import os

import uvicorn

from .config import load_dotenv
from .main import create_app


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("PORT", "8100"))
    host = os.environ.get("HOST", "0.0.0.0")
    # workers 必须为 1：任务队列线程跑在应用进程里
    uvicorn.run(create_app(), host=host, port=port, proxy_headers=True,
                forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"))


if __name__ == "__main__":
    main()
