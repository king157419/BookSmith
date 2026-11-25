# BookSmith

替教材"值班"的编辑助理：老师或维护者在网页里提需求，智能体在独立分支上改书稿、出习题、做课件，自己编译自检，然后交给人审，审过才合并。

产品说明见 `docs/01-产品形态与方案.md`，架构与接口约定见 `docs/04-架构与接口.md`。本 README 只讲网页部分（`app/`）。

## 本地运行（Windows / Linux 都一样）

```bash
# 1. 建虚拟环境并装依赖（国内可加 -i https://pypi.tuna.tsinghua.edu.cn/simple）
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt     # Linux: .venv/bin/python

# 2. 配置：复制 .env.example 为 .env，至少填 DATA_DIR 和 BOOK_REPO_DIR
#    BOOK_REPO_DIR 指向一个有 master 分支的书稿 git 仓库。
#    开发时请用一个副本，不要直接指向正在写的书稿：审核"通过"会真的合并进 master。
cp .env.example .env

# 3. 启动（读取 .env；单进程，内置任务队列线程）
.venv/Scripts/python -m app
```

首次启动时日志里会打印一次性 **setup token**，打开 `http://127.0.0.1:8100/booksmith/setup`，输入 token，设置你自己的管理员账号和密码。之后在「管理」页生成邀请码发给老师。

> Windows 的 Git Bash 会把 `BASE_PATH=/booksmith` 这类环境变量改写成 `D:/Git/booksmith`。在 Git Bash 里手动设变量时，先 `export MSYS_NO_PATHCONV=1`，或者把它写进 `.env`（`.env` 不受影响）。

也可以 `uvicorn app.main:app --port 8100`，但**只能开 1 个 worker 进程**（任务队列线程跑在应用进程里）。

### 测试

```bash
.venv/Scripts/python -m pytest app/tests -q
```

测试会在临时目录里建一个假的书稿 git 仓库，覆盖完整流程：setup、邀请码注册、登录与 CSRF、额度、建任务 → FakePipeline 跑完 → 审核通过真的合并进 master、打回生成子任务、newchapter 大纲闸门、BASE_PATH 两种请求形式、产物鉴权等。

### 防止提交密钥

```bash
cp scripts/check_secrets.sh .git/hooks/pre-commit   # 或 ln -sf ../../scripts/check_secrets.sh .git/hooks/pre-commit
```

它会扫描暂存区新增的行（`sk-…`、`tvly-…`、`ghp_…`、私钥头、`*_API_KEY=…` 等），并拒绝提交 `.env`。

## 部署在 nginx 子路径后面（BASE_PATH）

入口服务器 nginx（完整配置见 deploy/nginx-booksmith.conf）：

```nginx
location /booksmith/ {
    proxy_pass http://127.0.0.1:8100/;      # 末尾斜杠：转发时会剥掉 /booksmith 前缀
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;                     # SSE 实时进度
    proxy_read_timeout 1h;
    client_max_body_size 60m;                # 附件上限 50MB
}
```

**方案**：应用最外层有一个纯 ASGI 中间件（`PrefixMiddleware`），在路由之前检查请求路径：以 `BASE_PATH` 开头就剥掉，否则原样交给路由。所以下面两种请求都能处理：

| 请求到达应用时的路径 | 场景 |
|---|---|
| `/tasks/3`（前缀已被剥掉） | 上面这种 `proxy_pass …/;` 写法 |
| `/booksmith/tasks/3`（带前缀） | `proxy_pass http://127.0.0.1:8100;`（不带斜杠）或本机直连 |

应用生成的所有链接、表单 action、重定向、静态资源 URL 都带 `BASE_PATH`；cookie 的 `Path` 也是 `BASE_PATH`。浏览器始终看到的是外部路径 `/booksmith/...`，所以 cookie 能正常带上。`BASE_PATH=/` 表示挂在根路径。

应用返回 `X-Accel-Buffering: no`，nginx 默认也不会缓冲 SSE；如果中间还有别的代理（如 SSH 隧道前的另一层），同样要关缓冲。

## 结构

```
app/
├── main.py          FastAPI 应用工厂 create_app()；页面、表单、SSE、产物下载
├── config.py        环境变量 → Settings
├── db.py            SQLite（标准库），表结构同 docs/04
├── auth.py          scrypt 密码哈希、签名会话 cookie、CSRF、登录限速、邀请码
├── worker.py        单线程任务队列
├── bookrepo.py      书稿仓库：分支/worktree、diff、--no-ff 合并、推送、清理
├── render.py        安全 Markdown（markdown-it，禁用原始 HTML）、diff 解析
├── pipelines/
│   ├── base.py      Ctx / Result / Pipeline 接口 + 注册表
│   ├── fake.py      FakePipeline（开发/测试）
│   └── __init__.py  按 BOOKSMITH_PIPELINES=fake|real 注册
├── templates/       Jinja2 模板
├── static/          style.css、app.js（无构建、无外部 CDN）
└── tests/
```

## 关键设计

- **任务队列**：应用启动时（lifespan）起一个守护线程，按 id 顺序取 `queued` 任务执行，同一时间只跑一个。新任务提交时立即唤醒，否则 2 秒轮询一次。服务重启时，上次停在 `running` 的任务会标成失败。
- **会话**：`bs_session` cookie = base64(JSON{uid, iat}) + HMAC-SHA256 签名，HttpOnly、SameSite=Lax、14 天有效，HTTPS 下（含 `X-Forwarded-Proto: https`）加 Secure。服务端无状态：要强制所有人下线，换 `SECRET_KEY`。
- **CSRF**：双提交 cookie。随机的 `bs_csrf` cookie 和表单隐藏字段 `csrf`（或请求头 `X-CSRF-Token`）必须一致，所有 POST 都校验；登录时会换一个新值。
- **登录限速**：同一用户名或同一 IP 10 分钟内失败 5 次就锁定（内存里记，重启清零）。setup token 和邀请码输错也限速。
- **额度**：按北京时间每天的 0 点重新计数，统计归在该用户名下、当天新建的任务，打回产生的返工任务也算在内。成员自己打回时，先检查额度；管理员打回时不检查，所以返工任务即使超额也会建出来。
- **审核**："通过"仅管理员可用：`git merge --no-ff` 进 master；遇到冲突就 `merge --abort`，master 保持原状，任务仍是待审核。主克隆没检出 master 时，用临时 worktree 合并。配置了 `BOOK_GIT_REMOTE` 时，合并后推送。合并完删掉 worktree 和分支。"打回"时，原任务标为已打回，同时新建子任务（`parent_id` 指向原任务，沿用同一分支和大纲，`feedback` 是意见），子任务排队继续改。
- **安全**：所有页面带 CSP（`script-src 'self'`，没有内联脚本）、`X-Frame-Options: SAMEORIGIN`（允许本站 iframe 预览 PDF）、`nosniff`。产物和附件只能由任务的提交人或管理员访问，路径必须落在 `DATA_DIR/tasks/<id>/` 或 `DATA_DIR/uploads/` 之内。

## 接入真实流水线

在 `.env` 设置 `BOOKSMITH_PIPELINES=real`，并配置书稿仓库、OpenCode 及对应任务的编译工具。四种真实流水线都使用同一套模型配置。

| 接入方式 | `BOOKSMITH_MODEL` | `BOOKSMITH_MODEL_API_KEY` | `BOOKSMITH_MODEL_BASE_URL` |
|---|---|---|---|
| 默认 DeepSeek 服务 | 留空，默认 `deepseek/deepseek-flash` | 填 DeepSeek 的密钥 | 留空 |
| OpenCode 内置服务商 | 例如 `anthropic/claude-sonnet-4-6`，使用当前 OpenCode 版本识别且账号有权调用的模型 | 填该服务商的密钥 | 通常留空 |
| OpenAI 兼容的 Chat Completions 接口 | `custom/模型ID`，斜杠后的内容原样发给接口 | 填该接口的密钥，免鉴权本地服务可留空 | 填服务商提供的完整基础地址 |

例如，使用阿里云百炼北京地域的按量付费千问接口时，填写以下配置，并将占位文字替换为自己的密钥。

```dotenv
BOOKSMITH_PIPELINES=real
BOOKSMITH_MODEL=custom/qwen-plus
BOOKSMITH_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
BOOKSMITH_MODEL_API_KEY=你的百炼API密钥
```

模型需要支持工具调用，以便 OpenCode 读写书稿。若需图片输入，请选择支持图片的模型；自定义接口可在 `BOOKSMITH_OPENCODE_CONFIG` 对应的 OpenCode 配置中声明模型的输入模态和上下文长度。修改 `.env` 后重启应用，任务日志会显示本次使用的模型。Docker Compose 部署需重建应用容器以重新载入环境变量。

worker 负责的事：建分支和 worktree、在运行结束后自动提交没提交的改动、生成相对 master 的 `changes.diff`、把 `out_dir` 外的产物复制进来、处理异常和取消。流水线负责的事：在 `ctx.workdir` 里改稿并提交、把产物写进 `ctx.out_dir`、用 `ctx.log(step, msg)` 报告进度、在循环里检查 `ctx.cancelled()`。`newchapter` 第一轮返回 `awaiting_outline` 时，`summary` 就是大纲草稿；用户确认后，`ctx.task["outline"]` 非空，任务再次进入 `run`。
