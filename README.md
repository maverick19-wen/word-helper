# Word Helper

一个本地优先的英语单词查询工具：输入英文单词，即可查看中文释义、音标、词形变化、WordNet 派生词，以及一条由 LLM 生成的英文例句和中文翻译。

前端是无框架的单页 HTML，后端使用 FastAPI。词典释义与词形来自本地 ECDICT，派生词来自 NLTK WordNet，查询结果缓存在本地 SQLite 中。

## 功能

- 中文释义来自 **ECDICT**，包含词性标注与音标。
- 词形变化来自 **ECDICT** 的 `exchange` 字段，例如：
  - `happy` → `happier`、`happiest`
  - `go` → `went`、`gone`、`going`、`goes`
  - `child` → `children`
- 其他词性 / 派生词使用 **NLTK WordNet** 获取，并再次确认词条存在于 ECDICT 中，例如：
  - `happy` → `happiness`、`happily`
  - `beautiful` → `beautifully`
- 例句与中文翻译由 OpenAI 兼容的 LLM API 生成：
  - 校验例句中确实出现所查单词；
  - 校验中文翻译不为空；
  - 不符合要求时自动重试一次。
- 输入自动 `strip()`，查询不区分大小写。
- 查不到的单词显示 **“未找到”**，不会调用 LLM，也不会编造内容。
- 完整查询结果缓存到 `data/cache.sqlite3`；重复查询不会再次调用 LLM。
- 不提供用户账号系统；公网部署可选共享访问口令。不做生词本，不含发音播放和原生手机 App。

## 技术栈

- Python 3.10+
- FastAPI + Uvicorn
- SQLite（词典查询与结果缓存）
- ECDICT（英汉词典数据）
- NLTK + WordNet（派生词）
- OpenAI Python SDK（调用任意 OpenAI 兼容的 Chat Completions API）
- 原生 HTML / CSS / JavaScript

## 目录结构

```text
word-helper/
├── app.py                 # FastAPI 后端、词典查询、WordNet、LLM、缓存
├── convert_ecdict.py      # 唯一的转换脚本：ECDICT CSV → SQLite
├── requirements.txt
├── static/
│   └── index.html         # 前端页面
├── .env.example           # 环境变量示例，不含真实 Key
├── .gitignore
├── Dockerfile             # 云端部署镜像：构建时下载词典与 WordNet
├── .dockerignore
├── README.md
├── LICENSE
└── data/                  # 本地生成，已加入 .gitignore
    ├── ecdict.csv
    ├── ecdict.sqlite3
    ├── cache.sqlite3
    └── nltk_data/
        └── corpora/wordnet/
```

> 词典数据文件较大，不进入仓库。请按下面的步骤自行下载并转换。

## 安装与运行

### 1. 安装 Python 依赖

```bash
cd word-helper

python -m venv .venv
# Windows PowerShell:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 下载 ECDICT 并转换为 SQLite

从 [ECDICT 仓库](https://github.com/skywind3000/ECDICT) 下载 `ecdict.csv`（约 66 MB），放到项目的 `data/` 目录：

```powershell
# Windows PowerShell
mkdir data
curl.exe -L -o data\ecdict.csv https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv
```

```bash
# macOS / Linux
mkdir -p data
curl -L -o data/ecdict.csv https://raw.githubusercontent.com/skywind3000/ECDICT/master/ecdict.csv
```

然后运行唯一的转换脚本：

```bash
python convert_ecdict.py data/ecdict.csv -o data/ecdict.sqlite3
```

脚本只保留查询需要的字段，并把单词转为小写主键，因此查询天然不区分大小写。成功后会输出类似：

```text
完成: 770,238 条词条写入 data/ecdict.sqlite3 (...)
```

如果 GitHub 直链较慢，也可以在浏览器中打开 ECDICT 仓库，手动下载 `ecdict.csv`。

### 3. 下载 WordNet 数据

```bash
python -m nltk.downloader -d data/nltk_data -x wordnet
```

项目启动时会把 `data/nltk_data` 加入 NLTK 数据路径，不需要再配置系统级路径。若未安装 WordNet，词典查询仍可用，但“其他词性 / 派生词”一栏会为空。

### 4. 配置 LLM API

复制环境变量示例文件：

```powershell
# Windows
copy .env.example .env
```

```bash
# macOS / Linux
cp .env.example .env
```

编辑 `.env`，填入你自己的 API Key：

```dotenv
LLM_API_KEY=your-api-key-here
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
```

| 环境变量 | 必需 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `LLM_API_KEY` | 生成例句时必需 | 无 | LLM 提供方 API Key，只由服务端读取 |
| `LLM_BASE_URL` | 否 | OpenAI 官方地址 | 任意 OpenAI 兼容 API 的 Base URL |
| `LLM_MODEL` | 否 | `gpt-4o-mini` | 提供方支持的模型名 |
| `ACCESS_PASSWORD` | 否 | 无 | 可选的共享访问口令；设置后启用 HTTP Basic 认证，公网部署时建议开启 |
| `ACCESS_USERNAME` | 否 | `wordhelper` | 配合 `ACCESS_PASSWORD` 使用的用户名 |

`.env` 已加入 `.gitignore`，不要把真实 Key 提交到仓库。未配置 Key 时，仍可正常查询释义和词形，页面会提示例句生成服务未配置。

### 5. 启动

```bash
python -m uvicorn app:app --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>。FastAPI 接口文档位于 <http://127.0.0.1:8000/docs>。

本地运行时默认只监听 `127.0.0.1`。如果让服务监听 `0.0.0.0` 或部署到公网，请务必设置 `ACCESS_PASSWORD`，并使用 HTTPS。

### 6.（可选）云端部署

项目附带 `Dockerfile`，镜像构建时会自动下载 ECDICT 并转换为 SQLite，同时下载 WordNet；**词典数据不会提交到源码仓库**。

在一台已安装 Docker 的云服务器上：

```bash
cd word-helper
cp .env.example .env
# 编辑 .env：至少填写 LLM_API_KEY，公网部署务必填写 ACCESS_PASSWORD

docker build -t word-helper .
docker run -d --name word-helper --restart unless-stopped \
  -p 8000:8000 \
  --env-file .env \
  -v word-helper-cache:/app/cache \
  word-helper
```

然后手机通过 `http://服务器公网IP:8000` 访问。**这是使用 HTTP Basic 认证和 API Key 的服务，公网长期使用时应提供 HTTPS**，例如在容器前使用 Caddy/Nginx 反向代理，或部署到自带 HTTPS 域名的托管平台。

也可以把项目目录部署到支持 Docker 的托管平台（如 Render、Railway 等），在平台的 Environment/Secrets 中配置 `LLM_API_KEY`、`LLM_BASE_URL`、`LLM_MODEL` 和 `ACCESS_PASSWORD`。平台会提供 HTTPS 地址。

健康检查接口为 `GET /health`，不需要认证，可供云平台探活。缓存路径可以用 `CACHE_DB_PATH` 指向持久卷；上述 Docker 命令已将 `/app/cache` 挂载为命名卷。

> 部署本身需要你自己的云服务器或托管平台账号。本仓库只提供可部署的应用和容器配置，不会替用户注册或开通云服务。

## 输入输出示例

### 示例 1：形容词，大小写混合与首尾空格

输入：`  HaPpY  `（实际会按 `happy` 查询）

- **音标**：`/'hæpi/`
- **词性**：`a. 形容词`
- **中文释义**：`a. 快乐的, 幸福的, 愉快的, 恰当的`
- **词形变化**：比较级 `happier`，最高级 `happiest`
- **其他词性 / 派生词**：`happiness`（名词）、`happily`（副词）
- **LLM 例句**（示例，实际生成内容会变化）：
  - She felt happy when she saw the flowers in the garden.
  - 看到花园里的花，她感到很开心。

### 示例 2：不规则动词

输入：`go`

- **音标**：`/gou/`
- **中文释义**：包含 `vi.`、`vt.`、`n.` 多个义项，直接来自 ECDICT
- **词形变化**：过去式 `went`，过去分词 `gone`，现在分词 `going`，第三人称单数 `goes`
- **例句**：由 LLM 生成，保证原句包含完整单词 `go`（不会只出现 `going` 来代替）

### 示例 3：名词

输入：`child`

- **词形变化**：复数 `children`
- **派生词**：例如 `childhood`；仅展示 ECDICT 中真实存在的词条

### 示例 4：不存在的词

输入：`nonexistentzzzz`

- 页面显示 **未找到**，并提示检查拼写。
- 不会调用 LLM API。

接口形式：

```http
GET /api/word?q=happy
```

成功时返回释义、词性、词形、派生词和例句；不存在的词返回 HTTP `404`，响应体为 `{"detail": "未找到该单词。"}`。

## 缓存

- 位置：`data/cache.sqlite3`
- 缓存键：去除首尾空白并转为小写的单词。
- 只有成功生成例句的完整结果才会写入缓存；后续查询直接读取 SQLite，不重复请求 LLM。
- 如需强制刷新缓存，先停止服务，再删除 `data/cache.sqlite3*` 文件并重启。

## 实现说明与边界

- 中文释义、音标和词形变化直接取自 ECDICT；词性标签由 ECDICT 释义中的词性代码映射展示。缺失时不会补充或猜测。
- 派生词先在 WordNet 中查找，再通过 ECDICT 确认词条真实存在。WordNet 数据缺失时自动跳过这一部分。
- 例句的“包含所查单词”校验使用英文单词边界匹配，避免 `go` 命中 `going`、`ego` 等。
- 只接受单个英文单词，可包含连字符和撇号，例如 `long-term`、`can't`；不接受词组、数字或中文。
- 例句与翻译属于生成式内容，可能存在错误，请仅作学习参考；它不是 ECDICT 的词典释义。
- 使用 LLM 时，所查单词和词性会被发送到你配置的 API 提供方。除此之外，本工具不收集用户数据。

## 第三方数据、软件与许可证

| 名称 | 用途 | 许可证 | 来源 |
| --- | --- | --- | --- |
| ECDICT | 英汉词典释义、音标、词性、词形数据 | MIT（Copyright © 2025 Linwei） | <https://github.com/skywind3000/ECDICT> |
| WordNet 3.0 | 英语词义与派生关系 | WordNet License（Princeton University） | <https://wordnet.princeton.edu/> |
| NLTK | WordNet 访问接口 | Apache License 2.0 | <https://www.nltk.org/> |
| FastAPI | Web 框架 | MIT | <https://fastapi.tiangolo.com/> |
| Uvicorn | ASGI 服务器 | BSD-3-Clause | <https://www.uvicorn.org/> |
| OpenAI Python SDK | LLM API 客户端 | Apache License 2.0 | <https://github.com/openai/openai-python> |
| python-dotenv | 读取 `.env` | BSD-3-Clause | <https://github.com/theskumar/python-dotenv> |
| SQLite | 本地数据库 | Public Domain | <https://www.sqlite.org/> |

说明：

- ECDICT 与 WordNet 的原始数据**不随本项目分发**，需要用户自行下载；本项目只提供转换与查询代码。使用和再分发数据时请遵守各自的上游许可证。
- ECDICT 仓库采用 MIT 许可证；其词条由多个上游来源整理而来，详细来源说明见 [ECDICT README](https://github.com/skywind3000/ECDICT#readme)。
- WordNet 3.0 的完整许可文本见 [Princeton WordNet License](https://wordnet.princeton.edu/license-and-commercial-use)。
- LLM 生成的例句不属于第三方词典数据，其准确性和可用性取决于所配置的模型与服务条款。

## 本项目许可证

本项目代码采用 [MIT License](LICENSE)。