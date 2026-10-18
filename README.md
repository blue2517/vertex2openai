---
title: Vertex2OpenAI Express Adapter
emoji: 🔄
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
---

# Vertex2OpenAI Express Adapter

Vertex2OpenAI 是一个 **OpenAI API 兼容代理**。它对外提供 OpenAI 风格的 `/v1/chat/completions` 与 `/v1/models` 接口，对内支持两条上游通道调用 **Google Agent Platform（原 Vertex AI）的 Gemini 模型**：

- **Express API Key（标准模式）**：用官方 `google-genai` SDK + `VERTEX_EXPRESS_API_KEY` 调用。
- **Cookie 直连反代模式**：用 Google Cloud 控制台 Cookie + Project ID 直连控制台私有 `batchGraphql` 接口（无需浏览器）。

> 管理控制台 UI 名为 **agentplatform2api**；仓库/镜像名仍沿用 `Vertex2OpenAI`。
>
> 📌 **说明**：Google 已将 Vertex AI 更名为 **Agent Platform**，文档表述已同步（历史标识符如环境变量 `VERTEX_EXPRESS_API_KEY`、SDK 参数 `vertexai=True`、文件名 `vertexModels.json` 为兼容性保留）。**无头浏览器（Playwright）模式已移除**，由更轻量的 Cookie 直连模式完全替代。

## 功能特性

- **双上游通道，一键切换**
  - Express API Key：多 Key 随机或轮询调用官方 SDK。
  - Cookie 直连反代：Cookie + SAPISIDHASH 直连 `batchGraphql`，走网页端配额（含最新预览模型），真流式、防 60s 超时。**注意：走的是私有接口，见下方风险提示。**
- **OpenAI 兼容接口**：`GET /v1/models`、`POST /v1/chat/completions`。
- **管理控制台（浅色风格，单文件，免构建）**
  - **仅密码登录**：打开根路径 `/`，输入密码（即 `API_KEY`）即可，无需账号。
  - 标准模式 / Cookie 直连模式在线一键切换。
  - 在线热更新并保存 Google Cookie 与 Project ID；智能解析 `Cookie-Editor` 导出的 JSON / Header String；自动从整条控制台 URL 提取 Project ID。
  - **出网代理管理**：在「通道与凭证 → 出网代理」选择跟随环境、自定义 HTTP/HTTPS/SOCKS5/SOCKS5H 或直接连接；支持保存前测试，保存后新请求立即生效，认证信息只回显掩码。
  - **命名代理池**：保存多个代理，支持增删改、逐个测试、一键手动使用；既有自定义代理自动兼容，无需重填认证信息。
  - **模型参数面板**：按所选模型显示其支持能力，并在线调整思考强度、生图分辨率与比例、采样默认值、输入图压缩、重试、假流式/轮询/安全分显示（详见下文）。
  - **实时监控**：运行日志推流、健康度图表（成功/错误/拥堵重试）、Express 上游及 Cookie 上游/本地分词 Token 用量统计（本地计数包含文本与工具，不含媒体 token）。
- **Gemini 能力与适配**
  - 文本对话、流式（SSE）与非流式。
  - OpenAI tools / function calling ↔ Gemini function calling 适配（含 Gemini 3.x 多轮所需的 thought signature 编解码）；Express 与 Cookie 直连通道均支持自定义函数声明、调用与结果回传。
  - **安全分类对齐**：两条通道下发同一套安全设置（`HARM_CATEGORY_HATE_SPEECH`、`DANGEROUS_CONTENT`、`SEXUALLY_EXPLICIT`、`HARASSMENT`、`JAILBREAK`），阈值最宽松，避免通道间行为不一致。
    > 📌 **更正**：早期版本曾把"有思考、正文为空"归因于 Cookie 通道缺少 `HARM_CATEGORY_JAILBREAK`。这个解释是错的——按官方文档，**越狱分类器默认就是关闭的**，要打开必须显式把该分类的阈值设成具体的拦截值；不下发它不会启用任何过滤，下发 `OFF` 也只是空操作。该现象的真实成因见下方"3.6-flash 只返回思考"一节（前端恒发 `reasoning_effort=xhigh` 导致原生思考在 HIGH 档跑飞/被截断）。
  - **上游错误如实透传**：模型在当前项目/区域不可用（404）、权限不足（403）、参数非法（400）等，会以对应 HTTP 状态码 + OpenAI 错误格式返回，而非笼统的 500。
  - Google Search 增强：**文本模型**在模型名后加 `-search` 后缀按需开启。
  - 自动保留 Gemini 思考过程（Thinking），以 `reasoning_content` 返回。
  - 生图模型：输入图压缩、按模型的比例白名单校验、4K 等分辨率、图片输出转 Markdown data URL；生图强制"假流式"整块输出，避免超大 base64 卡死前端。
  - **按模型能力自动裁剪参数**：不同模型支持的参数不同，代理会自动移除目标模型不支持的参数以避免 400（详见"控制台与模型参数"）。
  - **自动退避重试**：两条通道内置 429/拥堵退避，次数与间隔由控制台配置。401/403 属于终止错误，不重试、不切换模型路由。流式首段有效输出前不提交 HTTP 200，首段错误直接返回实际 HTTP 状态；进入流式后才使用心跳与独立 SSE 错误。
  - **断连即停**：客户端断开后立即停止上游调用与重试。
- **中文运行日志**：密钥轮询、上游调用、重试退避、权限报错、Token 统计等均为中文实时说明。

## 快速开始（本地 Docker）

编辑 `docker-compose.yml`，设置初始环境变量：

```yaml
environment:
  - API_KEY=your_adapter_api_key
  - VERTEX_EXPRESS_API_KEY=your_vertex_express_api_key
```

启动：

```bash
docker compose up -d
```

默认将宿主机 `8050` 映射到容器 `7860`。浏览器打开控制台并用密码（`API_KEY`）登录：

```text
http://localhost:8050
```

## 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---:|---|---|
| `API_KEY` | 是 | `123456` | 保护本代理的 Key，同时也是**控制台登录密码**。客户端请求用 `Authorization: Bearer <API_KEY>`。 |
| `VERTEX_EXPRESS_API_KEY` | 否 | 空 | Gemini Express Mode API Key，多个用英文逗号分隔。标准模式使用。 |
| `ROUNDROBIN` | 否 | `false` | 多 Express Key 轮询(`true`)或随机(`false`)。可在控制台热改。 |
| `FAKE_STREAMING` | 否 | `false` | 先非流式取回再模拟流式；生图模型始终强制启用。可在控制台热改。 |
| `FAKE_STREAMING_INTERVAL` | 否 | `1.0` | 假流式等待期间 keep-alive 间隔秒数。可在控制台热改。 |
| `MODELS_CONFIG_URL` | 否 | 仓库 `vertexModels.json` | 远程模型列表地址；改远程文件即可刷新，无需重部署。 |
| `SAFETY_SCORE` | 否 | `false` | 是否把 Gemini safety ratings 附加到输出。可在控制台热改。 |
| `PROXY_URL` | 否 | 空 | 出网 HTTP/HTTPS/SOCKS5/SOCKS5H 代理；控制台可覆盖或显式选择直连，无需修改此变量。 |
| `SSL_CERT_FILE` | 否 | 空 | 自定义证书路径。 |
| `VERTEX_BASE_URL` | 否 | 空 | **高级**：覆盖标准模式的上游 `base_url`，正常无需设置。要钉定区域请用控制台的「标准模式 location」（详见下方「标准模式的 location」）。 |
| `GOOGLE_COOKIE` | 否 | 空 | Cookie 直连模式的 Google Cookie（初始值，后续可在控制台更新）。 |
| `GOOGLE_PROJECT_ID` | 否 | 空 | Cookie 直连模式的 Project ID（初始值，后续可在控制台更新）。 |
| `EXPERIMENT_FLAGS` | 否 | 空 | 可选：batchGraphql 的 `experimentFlagsBinary`，一般无需设置。 |
| `STATE_DIR` | 否 | `.` | `web_state.json` 的存放目录。用 Docker 时请指向挂载卷，否则重建容器会丢失全部设置与 Cookie。 |
| `ALLOW_DEFAULT_KEY` | 否 | 空 | 仅用于在公开托管环境（如 HF Space）临时放行默认 `API_KEY`，正常部署不要设置。 |

> 提示：`ROUNDROBIN`、`FAKE_STREAMING(_INTERVAL)`、`SAFETY_SCORE` 等环境变量仅作为**初始值**，运行时以控制台设置为准。

### 控制台出网代理

在 **通道与凭证 → 出网代理** 中配置：

- **跟随环境变量**：沿用 `PROXY_URL`；未配置时保持 HTTP 客户端的环境代理行为。
- **自定义代理**：填写 `http://HOST:PORT`、`https://HOST:PORT`、`socks5://USER:PASSWORD@HOST:PORT` 或 `socks5h://HOST:PORT`。认证信息中的 `@`、`#` 等字符应使用百分号编码。
- **直接连接**：显式忽略 `PROXY_URL`、`HTTP_PROXY`、`HTTPS_PROXY` 等环境代理。
- **代理池（手动选择）**：保存多个带名称的代理，直接点列表里的「使用」切换，或从下拉框选择后保存。测试某个条目不会启用它。

「已保存的代理」支持添加、改名、更新地址、单条测试和删除。旧版自定义地址会显示为「原自定义代理」；也可将环境变量中的当前代理加入列表，认证信息由服务端沿用。编辑时地址留空保留密码；删除正在使用的池条目前需先切换。代理池最多保存 100 条，仅手动选择，不自动轮换，现有上游重试逻辑保持不变。

点击「测试连接」仅测试当前表单，通过所选连接访问固定的 Google 公共探测端点，不保存配置，也不发送 API Key 或 Cookie。「保存代理」将配置写入 `STATE_DIR/web_state.json`，新请求立即生效；正在进行的请求继续使用原连接。两条聊天通道、模型清单加载、项目探测和远程图片获取共用此设置。

已保存地址只回显掩码；输入框留空会保留自定义代理，首次设置时也可沿用 `PROXY_URL`。切到直连不会抹掉已保存地址；切回「自定义代理」并保存即可再次使用。状态文件含认证信息，需保护访问权限并定期备份；Docker 部署时保留 `data` 卷。

开发验证：安装 `app/requirements.txt` 和 `pytest` 后执行 `python -m pytest -q`。测试使用临时状态目录和模拟网络，不连接生产服务；安装 Node.js 后还会执行内联前端表单的交互回归测试。

---

## 行为变更说明（整改后）

按 `REFACTOR_PLAN.md` 完成整改后，以下行为与旧版本不同，升级时请注意：

| 项目 | 旧行为 | 新行为 |
|---|---|---|
| **思考档位兜底** | 非法档位一律抬到 `high`（Pro 上选 minimal 反而变 high） | 就近**向下**夹取：Pro 选 minimal → `low` |
| **`retry_max` 语义** | Express 通道当作总次数，设 0 时一次请求都不发 | 统一为「重试次数」，总请求数 = `retry_max + 1`，设 0 仍请求一次；取值钳到 0–50 |
| **并行函数调用（流式）** | 只发第一个，`index` 恒为 0 | 全部下发，`index` 跨 chunk 稳定递增 |
| **思考签名** | base64 拼进 `tool_call_id`（上千字符，易被前端截断） | 服务端通过 `tool_call_id` 自动缓存与映射，响应体保持纯净 OpenAI 规范（不注入非标字段）；缺失签名时使用官方 `skip_thought_signature_validator` 哨兵兜底 |
| **Cookie 通道 + 函数调用** | 静默把 `role=tool` 折成 model，发出错乱历史 | 原生下发函数声明，按 `functionCall` / `functionResponse` 回放历史并保留 thought signature |
| **Cookie 通道输入图** | 不压缩、不支持 http(s) 图片、不解析正文内联图 | 与标准通道一致（压缩开关对两条通道都生效） |
| **`stop` 字段** | 只接受数组，传字符串 422 | 字符串/数组都接受 |
| **`logprobs` 字段** | 按 Gemini 语义当整数 | 兼容 OpenAI 的 `logprobs: bool` + `top_logprobs: int` |
| **Express 流式 usage** | 从不下发，客户端显示 0 | 支持 `stream_options.include_usage` |
| **控制台 Cookie 回显** | 明文返回完整 Cookie | 仅返回“已配置/字段数/总长度”，不返回任何值的前后缀；输入框留空＝保持原 Cookie 不变 |
| **登录** | 无限速、会话 token 为确定值 | 失败 3 次后指数退避；随机会话 token，可单独失效 |
| **状态文件** | 每次读设置都同步读盘、非原子写 | 内存优先 + 原子写 + 权限 0600 + 支持 `STATE_DIR` |
| **文本保真** | 所有消息的多空格/缩进被压平 | 仅在确实抽走内联图片时才压平 |
| **2.5 Flash-Lite 思考预算** | 下限按 0 处理 | 下限 512（`0` 仍表示关闭） |
| **生图比例白名单** | 所有生图模型共用同一套比例 | 按模型分别校验；Flash Image 支持含 `9:21` 在内的扩展比例，Pro Image 使用较小白名单 |

思考签名由反代服务内部通过 `tool_call_id` 与短期缓存（`SignatureStore`）自动管理，下游响应保持标准 OpenAI 格式；缺失显式签名时由跳过校验哨兵兼容处理。

---

## 控制台与模型参数

控制台"模型参数"面板可在线调整全局默认值，并按所选模型显示其支持情况。可调项：思考强度（3.x 档位 / 2.5 预算）、生图分辨率与默认比例、采样默认值（temperature/top_p/max_tokens）、输入图压缩（开关/边长/质量）、Studio 读取超时、重试次数与退避、假流式/轮询/安全分显示、Cookie 调试日志。

**Studio 读取超时**：在“模型参数 → ② 全局设置 → 超时、重试与退避”设置 `studio_read_timeout_seconds`，默认 **180 秒**，范围 **1–3600 秒**（整数）。点击“保存全局设置”后持久化到 `web_state.json`，新请求立即生效，重启后保留。仅作用于 Studio/Cookie 通道，文本流式、非流式与生图共用；限制的是等待首段或下一段响应数据的时长，而非整个请求总时长。非流式超时返回简短的 **504 / timeout** 原因并结束请求，日志仅记一行；其它限流重试规则保持原样。非流式续传缓冲的等待上限同步留出余量；客户端及外层反向代理的超时请相应调整。

**Studio 断连重试与耗时日志**：

- `RemoteProtocolError`（包括响应头前断连、`incomplete chunked read`）及连接/读写网络错误按异常类型识别，沿用控制台的 `retry_max` 和退避设置；`retry_max=0` 只请求一次。证书校验、请求格式、代理认证错误不因这项改动反复重发。
- 文本真流式只在尚未输出思考、正文、图片或工具调用时重试；心跳、角色块、UI 元数据和独立签名不算首段有效输出。已有有效输出后断连，保留先前输出，再发送顶层 SSE `error` 与 `[DONE]`，不补成功结束块，也不重新拼接另一轮生成。
- 真流式仅增加**首包、首字**两项计时：首包是首个非空响应体数据块到达（在解压、文本解码和 JSON 解析前计时，UI 元数据也算，但不要求存在 UI 事件）；首字是首个有效输出到达（思考、正文、图片或完整工具调用，日志标注类型，心跳/角色/UI 不算首字）。不再单列响应头等待、正文首字或工具调用独立计时，也不增加定时等待提示。日志携带请求短 ID；结束或传输异常时保留阶段、当前尝试接收的解码后 UTF-8 字节数/JSON 数及**包含失败尝试和退避等待的总耗时**。用单调时钟计时，首包、首字和总耗时都不随重试清零；字节数不是 token 计量，新增传输诊断不输出认证头、Cookie 或异常内的私密 URL。
- 非流式及生图假流式仍先收齐响应再返回；传输断连可按现有预算重试，不把缓冲收齐后的解析时间标为上游首字时间。非流式 `TimeoutException` 仍维持单次 504 结束的既有行为。
- 重新发送 POST 属于另一次生成尝试，不是恢复同一次上游生成。即使尚未收到输出，上游也可能已处理请求；重试可能额外消耗额度，应按需求设置重试次数。

### API 错误协议

- 非流式失败返回对应的 HTTP 4xx/5xx 与顶层 `{"error":{"message":"...","type":"...","code":...,"param":null}}`，不返回 `choices.message.content`，不把排错说明当作模型回复。
- Studio 上游即使以 HTTP 200 包装 GraphQL 错误，也会解析实际错误，映射为 400/401/403/429/5xx。空回复、只有思考而无正文/工具调用返回 502；上游明确拦截返回独立错误。
- 流式首段有效输出前不发送 200、心跳或空内容块；首段 401/403 会作为 HTTP JSON 错误直接返回，服务内不重试，SDK 也不切换模型路径。此阶段调用方需给首字节等待留足时间；只有出现有效文本、思考或工具输出才正式建立流。
- 流式响应头发出后 HTTP 状态码已固定；后续失败发送独立 `data: {"error":{...}}`，随后 `[DONE]`。错误不写入 `delta.content`，也不补成功的 `finish_reason=stop`；客户端需要检查 SSE 的顶层 `error`。
- 续传缓冲会把错误流记录为 `error`，而非 `done`；详细诊断留在服务端日志，客户端错误消息限长并隐藏 API Key。
- `error.code` 使用字符串语义码（例如 403 的 `permission_denied`、429 的 `rate_limit_exceeded`），HTTP 状态保留真实数字。最终 HTTP 错误带 `x-should-retry: false` 与 `Cache-Control: no-store`；支持该提示的 OpenAI SDK 不再叠加一轮自动重试。该头属于 SDK 兼容提示，不是 HTTP 标准的强制停止指令；忽略该提示的中转仍由其自身重试策略决定，不将真实 429/403 伪装成 400。
- Studio 非流式的 HTTP 错误、HTTP 200 中封装的 GraphQL 错误和超时都输出带相同请求 ID 的终态日志；经任务路由调用时同时标注响应头中的任务 ID。控制台能看到每次调用对应的 HTTP 状态和结束记录，不再仅有开始记录。此改动不增加失败缓存、去重或冷却窗口。

### Cookie 通道的 Token 用量

- 正常非流式响应总是包含 `usage.prompt_tokens`、`completion_tokens`、`total_tokens`。流式请求设置 `"stream_options":{"include_usage":true}` 时，在结束前收到 `choices: []` 的用量尾块；未请求尾块时仍统计到控制台。
- 上游有效 `usageMetadata` 优先，跨事件合并字段；缺失、无效或全为 proto 默认零值时，用本地分词补计。部分字段由上游给出时保留这些字段，再补齐缺项。
- 输入按**实际发出的最终请求体**计数：系统指令、消息历史、启用的工具名称/描述/参数 Schema、历史函数调用参数、工具执行结果均包含；`tool_choice=none` 或原生工具协议降级后未发出的声明不计入。
- 输出包含正文、实际收到的思考文本、函数名称及参数键和值。流式文本片段先按连续语义段合并再分词，不把每个网络块单独分词相加。思考计入 `completion_tokens`，并可在 `completion_tokens_details.reasoning_tokens` 查看其子项。
- 顶层 `usage_metadata` 标明来源，例如 `{"source":"local_tokenizer","estimated":true,"tokenizer":"gemma3","scope":"text_and_tools","media_tokens_included":false,"contains_media":false}`；完整上游数据标为 `source=upstream`，混合补计标为 `source=mixed`。标准 `usage` 字段保持 OpenAI 结构。
- 使用随程序打包、SHA-256 校验的 Google Gemma 3 SentencePiece 词表（约 4.7 MB）；启动时本地加载，分词在线程中执行，没有运行时词表下载、计数 API 请求或向外发送文本。升级需安装新增的 `sentencepiece` 依赖，Docker 构建会自动安装并包含词表。
- 本地统计保持实际消息字符串的换行，不改写文本。用户给定的 LF 样本验证为 **1065 tokens**；CRLF 文件回放应与实际消息保持同一口径。
- 本地计数不是账单审计：未观测到的内部思考、私有协议开销、失败尝试及图片/音视频 token 不虚构计量。含媒体时标注 `contains_media=true`、`media_tokens_included=false`，base64、签名、UI 元数据及代理附加的安全评分 HTML 均不当作正文 token；不同模型的词表兼容性仍应以各模型实测为准。

### 断连、重连宽限与主动停止

- **默认断连即停**：两条通道共用 ASGI 断连监听，覆盖请求准备、等待首字节、正文读取及退避等待。最后一个调用方离开后，取消上游任务并等待清理，关闭 HTTP/SDK 连接，停止后续重试。取消不计作上游故障，运行日志只记一条取消原因。
- **缓存与保活独立**：开启“弱网续传”本身只启用响应缓存；默认 `continue_on_disconnect=false`。如需跨断线继续生成，在“模型参数 → ② 全局设置”开启“断连后等待重连”，设置 `disconnect_grace_seconds`（默认 **5 秒**，范围 **0–300 秒**，整数）。宽限内用原请求体和原幂等头重新连接即可接续；到期仍无调用方则取消。此选项仅对续传模式生效，保存后对新任务生效。
- **共享任务**：同一个续传任务有多个调用方时，单个断开只释放该连接的租约，最后一个离开才触发取消或宽限。轮询请求也参与租约管理；启用宽限后，轮询间隔应短于宽限时长。
- **显式停止**：响应头 `X-Request-Id` 标识任务；续传模式额外返回 `X-Resume-Id`。携带同一 API Key 调用 `DELETE /v1/requests/{id}`（亦支持 `DELETE /v1/resumable/{id}`）可跳过宽限立即停止，并返回 `status=cancelled`。重复取消保持幂等；未知或非该 API Key 的任务返回 404。非续传模式的终态记录最多保留 5 分钟、1024 条，过期后返回 404。
- **非流式尚未收到响应头**：续传模式支持 `POST /v1/chat/completions/cancel`，发送与原请求相同的 JSON 请求体及原幂等/请求 ID 头来定位并取消任务。非续传模式下，直接断开该非流式请求即可停止。
- **终态**：缓冲保留 `cancelled` 状态与简短原因；显式取消时，仍连接着的非流式调用方收到 **499 / request_cancelled**，流式调用方收到错误帧与 `[DONE]`。同一请求再次生成时重新建任务。

取消保证本服务终止任务并关闭上游连接；上游是否立即停止计算由其服务端决定。若中间还有反向代理，需让它传递调用方断连；主动停止可直接调用取消接口。

任务登记与续传缓冲位于当前进程内；多 worker / 多副本部署需使用请求粘滞路由，确保重连、轮询和取消到达原任务所在进程。

### 参数优先级

**单次请求 > 该模型专属 > 控制台全局默认 > 内置默认。**

- 有请求级写法的参数：请求体字段优先。标准字段 `temperature`/`top_p`/`max_tokens` 等，扩展字段 `reasoning_effort`、`thinking_budget`、`image_size`、`aspect_ratio`/`ar`。客户端不传时依次回退到"该模型专属 → 控制台全局 → 模型默认"。
- 两个例外：
  1. **"模型不支持"优先级最高**（最后一步裁剪）：目标模型不支持的参数，无论来自请求、专属还是全局都会被移除（避免 400）。
  2. **全局项无请求级/专属写法**：图压缩、重试、假流式、轮询、安全分显示、Cookie 调试仅由控制台全局决定。

### 按模型单独保存参数（per-model overrides）

模型参数面板顶部选择模型后，可为**当前所选模型**单独保存专属值——支持覆盖的三类：**思考（档位/预算）、生图（分辨率/比例）、采样默认（temperature/top_p/max_tokens）**。

- **保存为该模型专属**：只把上述三类字段存成该模型的专属配置；下拉框中该模型名后会显示 `★`，并出现"已有专属参数"徽章。
- **清除该模型专属**：删除该模型专属配置，回退到全局默认。
- 底部"保存设置"按钮保存**全局默认 + 基础设施项**；当所选模型已有专属配置时，它**不会**用面板里显示的专属值覆盖全局（避免误操作），只保存基础设施项。
- 基础设施项（图压缩/重试/假流式/安全分/Cookie 调试）不支持按模型覆盖，始终全局唯一。

### 按模型能力自动裁剪（`app/model_capabilities.py`，依据官方文档）

- **采样参数弃用**：自 **Gemini 3.6 Flash / 3.5 Flash-Lite 起（及所有更新/未来模型）**，`temperature`/`top_p`/`top_k` 已废弃（现被忽略、未来返回 400），代理会**自动移除**；更早的 3.x（如 3.0–3.5 非 lite）仍可用，但官方建议保持默认。
- **`candidate_count`**：所有 Gemini 3.x 不支持，自动移除。
- **思考**：Gemini 3.x 用 `thinking_level`（`minimal`/`low`/`medium`/`high`，不可完全关闭；各模型默认不同，如 3.6-flash=medium、pro=high、flash-lite=minimal）；
  - **`gemini-3.7-flash`（2026-08 新增）**：官方默认档位 **`medium`**（取代旧 3.x 的 `high`），档位表只提供 **`low`/`medium`/`high`——没有 `minimal`**（出处：`intro_gemini_3_7_flash.ipynb`）。因此在 3.7 上选「关闭原生思考」会压到 `low`（就近合法档），而不是发一个不支持的枚举去冒 400 风险。同理，**3.7 及更新/未知型号一律不提供 `minimal`**，等真机验证后再逐个放开；`thinking_level` 与旧的 `thinking_budget` **不可同时出现**（官方明确会 400），本代理只会二选一。Gemini 2.5 用 `thinking_budget`（`-1` 动态；2.5-flash 可设 `0` 关闭，2.5-pro 最低 128）。
  - **原生思考控制（`native_thinking_mode`）**——控制台"思考强度"卡片的下拉，支持"保存为该模型专属"：
    - **跟随请求（默认）**：用前端发来的 `reasoning_effort`。⚠️ SillyTavern 等前端常在**每次请求都发 `reasoning_effort`（如 `xhigh`）**，会覆盖你在控制台设的档位。
    - **关闭原生思考（角色扮演推荐）**：忽略前端 effort，把档位压到该模型最低（3.x=`minimal`、2.5-flash 预算 `0`、2.5-pro `128`），并**不返回思考**。
    - **强制用上方档位**：忽略前端 effort，用你在卡片里选的档位（返回思考）。
  - 🎭 **酒馆预设“卡原生思维链”一键配置**：许多预设把思维链写在 **system 提示**里（不是预填充），并恒发 `reasoning_effort=xhigh`。把"原生思考控制"选 **“关闭原生思考”** 即可（可用"保存为该模型专属"只对 3.6-flash 生效），让预设自己的思维链接管。
  - 🩺 **3.6-flash 在 Studio 只返回思考、无正文（`FINISH_REASON_UNSPECIFIED`）怎么办**：真机定案——这是**原生思考在 HIGH 档跑飞/被截断**（SillyTavern 恒发 `reasoning_effort=xhigh` 覆盖了控制台档位），**不是**安全策略/`HARM_CATEGORY_JAILBREAK` 的问题（已用含/不含 jailbreak 的多组对照验证）。**解决：把"原生思考控制"设为"关闭原生思考"**（对 3.6-flash 用"保存为该模型专属"），即忽略前端 `xhigh` 并压到 `minimal` 档位。
- **生图**：剥离全部采样参数；`response_modalities=["TEXT","IMAGE"]`；**两个生图模型比例白名单不同**（pro-image 10 种；flash-image 15 种，含 `1:4/4:1/1:8/8:1/9:21`），控制台按所选模型过滤，后端也会校验，选到不支持的比例会**自动回退为"由模型决定"（不报错）**。
- **预填充由调用端负责**：服务端不检测末尾 assistant 是否为预填充，不补占位 user、不注入 assistant 或续写指令、不改写末尾轮次，也不根据消息结尾压制原生思考。旧的预填充兼容/注入设置不再生效。上游若拒绝请求，按 API 错误协议返回实际错误，不自动改写消息后重试。控制台显式配置的附加 system 指令仍为独立的可选功能，留空则保持消息不变。

- **新增/未来模型**：按家族/版本模式自动归类；未知/未来型号按"最新代"前向安全处理（自动移除已废弃采样参数）。

---

## 标准模式的 location（能指定，且能修好"偶发 404 / 随机区域"）

**实测结论（2026-08-14，真机验证）**：留空时 location 由 Google 后端自选，**可能落到该模型不提供服务的区域直接 404**；显式钉定后同一个 Key、同一个模型即正常。

| 请求形态 | `gemini-2.5-pro` | 说明 |
|---|---|---|
| 裸模型名（旧行为）→ express 端点 `https://aiplatform.googleapis.com/v1/publishers/google/models/{model}:generateContent` | **404** `projects/…/locations/asia-southeast1/…was not found` | 区域由后端路由，落到没有该模型的区域 |
| `projects/{project}/locations/global/publishers/google/models/{model}` | **200 正常出文** | 区域由我们钉定 ✅ |

**默认已经是 `global`**（多数 Gemini 模型只在 global 提供）。控制台「模型参数 → ② 全局设置 → 标准模式 location」下拉里除 `global` 外还列出了 Agent Platform 提供 Gemini 的各区域（美国 / 欧洲 / 亚太 / 其它，共 30 项）可按需切换；选「默认（后端自选）」＝回到旧行为。

**项目 ID 不需要单独配**：自动用「通道与凭证」页填的那个（或环境变量 `GOOGLE_PROJECT_ID`）——一个人通常只有一个 Express 项目。两者都没有时自动退回裸模型名（不会拼出半截路径）。

**路径错误兜底**：模型路径不存在或参数不匹配时，可退回裸模型名重试一次；401/403 认证或权限错误直接终止，不再更换路径重试。

几个必须知道的边界（都是实测）：

- **项目必须是该 API Key 有权、且已开启计费的项目**。换成别的项目会 `403 requires billing to be enabled`。
- **区域端点主机不通**：`https://{location}-aiplatform.googleapis.com/...` 配 Express Key 会 404（该模型在那个区域没有）。因此钉定走的是**全局主机 + 路径里带 location**，不是换主机。
- **有的模型只在 `global` 提供**（如 `gemini-2.5-pro`），所以推荐 `global`。
- SDK 层面**不能**给 `Client` 传 `location`：`api_key` 与 `project/location` 互斥，硬传会 `ValueError`。钉定是靠**模型资源路径**实现的（`google-genai` 的 `t_model()` 对 `projects/` 开头的 model 原样透传）。
- 启动后第一次调用会打印一行端点解析结果，便于核对；钉定生效时另有一行 `🌐 [上游端点] 已钉定 location: projects/...`。

> 📌 **勘误**：本文件上一版写的"Express 模式无法指定 location、报错里的区域无从干预"**是错的**——那个结论只验证了"不能给 Client 传 location"，漏了"可以用完整资源路径钉定"这条路。已按真机结果更正。

---

## Studio(Cookie) 通道：免登录 与 非 Express 项目

两种设想都做了真机验证（2026-08-14）：

| 场景 | 结果 |
|---|---|
| **不带 Cookie（免登录）** + Express 项目 | ❌ `Permission 'aiplatform.endpoints.predict' denied` |
| **不带 Cookie（免登录）** + 非 Express 项目 | ❌ `requires billing to be enabled` |
| 不带 Cookie + 空项目 | ❌ `Request contains an invalid argument` |
| 带 Cookie + Express 项目 | ✅ 正常出文 |
| 带 Cookie + 非 Express 项目（未开计费） | ❌ `requires billing to be enabled` |

结论与代码现状：

- **本通道用的 `batchGraphql` 控制台接口不接受匿名调用**，必须有登录态（SAPISIDHASH）。浏览器里"不登录也能用 Studio"走的是**另一条**面向未登录用户的接口/额度，本项目当前不支持。若你能抓到那条请求（F12 → Network → 复制 URL、请求头与 payload），加一条上游并不难。
- **代理里并没有"检测是不是 Express 项目就拒绝"的逻辑**——能不能用只取决于：① 有登录态 Cookie；② Project ID 填的项目**该账号有权限且已开启计费**。是否 Express 项目本身不是门槛。
- 认证/权限失败直接返回上游的 401/403，不据此猜测 Cookie、项目或计费状态，不追加排查长提示。每条请求遇到这类错误立即结束；调用方再次发送 POST 是另一条独立请求。

---

## Cookie(Studio) 通道的原生函数调用

Cookie 直连通道会把 OpenAI `tools` 转成 batchGraphql 的原生 `functionDeclarations`。`tool_choice=none` 会直接省略全部上游工具；`auto` / `required` / 指定自定义函数分别使用 Studio 的 `AUTO` / `ANY` 模式。嵌套对象与数组参数会递归转换成 Studio 私有 UI Schema。内建 `googleSearch` 没有可放进 `allowedFunctionNames` 的声明名，因此搜索请求不附加自定义函数的 `toolConfig`。

| 情况 | 行为 |
|---|---|
| 自定义函数声明 | 原生下发；流式与非流式响应都返回 OpenAI `tool_calls` |
| 声明里含搜索类工具（`google_search` / `web_search` 等） | 映射为 Studio 内建 `googleSearch`。实机确认 batchGraphql 不允许它与自定义函数混用：混合 AUTO/required 请求优先自定义函数并忽略搜索；显式强制搜索时只发送 `googleSearch` |
| 历史里的 assistant `tool_calls` | 回放为 role=`model` 的 `functionCall` Parts，并从短期缓存或上下文恢复 thought signature |
| 连续的 role=`tool` 结果 | 按 `tool_call_id` 关联函数名，合并为 role=`user` 的 `functionResponse` Parts；并行顺序为 FC1, FC2, FR1, FR2 |
| 模型或协议明确拒绝原生函数 Schema | 仅本次固定降级为文本工具观测并重试一次；没有用户可见策略开关，也不会恢复旧版严格拒绝路径 |

---

## Cookie 直连模式配置指引（支持手机与电脑）

> ⚠️ **使用前请先读这段风险提示**
>
> Cookie 直连模式的原理是：用硬编码的网页客户端 key 和固定的 `querySignature`，
> 冒充 Google Cloud 控制台前端去调用其**私有** `batchGraphql` 接口。因此：
>
> - **无兼容性承诺**：这是内部接口，Google 改一次 `querySignature` 或参数结构就会全线失效，且不会有任何公告。
> - **条款风险**：以自动化方式访问非公开接口，可能与 Google Cloud 的使用条款相冲突，存在账号被限制或处置的风险。请仅用自有账号、自担风险。
> - **凭证敏感度极高**：配置的 Cookie 含 `__Secure-1PSID` 等完整会话凭证，等价于该 Google 账号的完整访问权。请勿把本服务部署到公开可访问的地方，务必设置强 `API_KEY`。
>
> 如果你需要的是稳定、可长期依赖的方案，请使用标准模式（Express API Key）。

在控制台切换到 **Agent Platform Studio (Cookie 直连反代)**，需配置 **Cookie** 与 **Project ID**：

### 1. 获取完整 Google Cookie
关键会话凭证（如 `__Secure-1PSIDTS`、`__Secure-1PSID`）带 `HttpOnly`，无法用书签脚本提取，需按下述方式获取：
- **电脑端**：登录 [Google Cloud Console](https://console.cloud.google.com) → 按 **F12** → **Network** → 刷新页面 → 点任意成功请求 → 复制 **Request Headers** 里的 `Cookie:` 整段，粘贴到控制台。
- **手机端**：iOS(Safari) 或 Android(Kiwi) 安装 `Cookie-Editor` → 登录控制台 → 插件 **Export** 为 **Header String** 或 **JSON** → 整段粘贴，系统自动解析。

### 2. 获取 Project ID
- 从控制台顶部项目选择器复制，或直接把含 `?project=xxx` 的整条 URL 粘贴到输入框，系统自动提取。

> ⚠️ **关于 Cookie 有效期**：通常较为持久——只要不退出登录、不修改密码、Google 未主动失效会话，一般可维持**数周甚至更久**（实测可用一个月以上），**并非只有 1~2 小时**。仅当接口确实报 `Permission Denied` / `predict denied` 时，重新获取并到控制台保存激活即可。

---

## 调用示例

### 查询模型

```bash
curl http://localhost:8050/v1/models \
  -H "Authorization: Bearer your_adapter_api_key"
```

### 非流式对话

```bash
curl http://localhost:8050/v1/chat/completions \
  -H "Authorization: Bearer your_adapter_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-flash",
    "messages": [
      {"role": "user", "content": "用一句话介绍 Gemini。"}
    ],
    "stream": false
  }'
```

### 流式对话

```bash
curl http://localhost:8050/v1/chat/completions \
  -H "Authorization: Bearer your_adapter_api_key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-2.5-flash",
    "messages": [
      {"role": "user", "content": "写一首短诗。"}
    ],
    "stream": true
  }'
```

### Google Search 搜索增强（文本模型）

在模型名后加 `-search` 后缀：

```json
{
  "model": "gemini-2.5-flash-search",
  "messages": [
    {"role": "user", "content": "今天有哪些 Gemini API 相关更新？"}
  ]
}
```

### 单次请求覆盖参数（任意前端可用）

通过请求体额外字段按需覆盖控制台默认：

```json
{
  "model": "gemini-3-pro-image",
  "messages": [{"role": "user", "content": "生成一只赛博朋克猫"}],
  "image_size": "2K",
  "aspect_ratio": "16:9"
}
```

（文本模型可用 `reasoning_effort`：`low`/`medium`/`high`，或 2.5 用 `thinking_budget` 整数。）

> **注**：`temperature`/`top_p`/`max_tokens` 等标准字段、以及上述扩展字段，其优先级恒高于"该模型专属"与"控制台全局"设置。若要为某模型设持久默认值又不想每次请求都带，请用控制台的"保存为该模型专属"。

---

## 模型列表配置

> ⚠️ **先看这条**：模型列表**默认从远程拉取** —— `https://raw.githubusercontent.com/bad-woman/vertex2openai/main/vertexModels.json`，只有远程全部地址都不可用时才回退到镜像里的本地 `vertexModels.json`。
> 所以**只改本地文件（或只更新镜像）通常看不到新模型**，必须二选一：
> 1. 把更新后的 `vertexModels.json` 推到 GitHub `main`（推荐，改远程即可全部部署生效、无需重部署）；
> 2. 或设环境变量 `MODELS_CONFIG_URL` 指向你自己维护的地址。

默认模型列表在远程 `MODELS_CONFIG_URL` 或本地 `vertexModels.json`：

```json
{
  "models": [
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-image",
    "gemini-3-pro-image",
    "gemini-2.5-pro",
    "gemini-2.5-flash"
  ]
}
```

> ⏰ **停用时间线（核对于 2026-07-26，请自行复核官方 deprecations 页）**
> - `gemini-2.5-pro` / `gemini-2.5-flash` / `gemini-2.5-flash-lite`：**2026-10-16 停用**。届时 `thinking_budget` 分支将没有活模型，可整体删除。
> - `gemini-3-flash-preview`：官方推荐替代为 `gemini-3.5-flash`，但**该模型目前仍可正常调用**（2026-07-26 用 Express Key 实机验证通过），因此**保留在默认清单中**；待官方正式停用后再移除。
> - `gemini-3.1-flash-image` / `gemini-3-pro-image`：GA 版本（不带 `-preview`），对应的 `-preview` 版本已于 2026-06-25 停用。

`/v1/models` 会自动为**非生图**的 Gemini 模型生成带 `-search` 后缀的别名。新增模型只需把 ID 加进此列表即可（能力自动归类，无需改代码）。

---

## 关于 429 报错与并发控制

429（Resource Exhausted）常因上游限额不足或请求频率过高。项目已内置退避重试，另建议：
- 控制客户端并发频率。
- 适当减小最大输出 Token。
- 配置多个有效 Express Key 并开启轮询。
- 及时更新失效或权限受限的 Google Cookie。

---

## 后续升级与扩展

- **新增模型**：把模型 ID 加入 `vertexModels.json`（或远程 `MODELS_CONFIG_URL`）。`model_capabilities.py` 按**家族/版本模式**自动归类（思考方式、采样裁剪、生图比例/分辨率），**未知/未来型号按"最新代"前向安全处理**。基本即插即用。
- **迁移到 Interactions API**：代码按上游通道解耦（`app/upstreams/` 下各类实现 `BaseUpstream`；能力判定、消息转换、参数构建均可复用），新增一个 `InteractionsUpstream` 并在路由层接入即可。

  现状（核对于 2026-07-26）：
  - **Gemini Developer API 侧**：Interactions API 已于 2026-06 **GA**，官方推荐新项目使用；`generateContent` 被标为 legacy 但继续完整支持。
  - **Agent Platform 侧**：Interactions API 仍标注为 **experimental**。
  - **Express 模式**：REST 面只有 `countTokens` / `generateContent` / `streamGenerateContent` —— **这才是本项目暂不迁移的直接原因**（本项目走的正是 Express 通道）。

---

## 本地开发与检查

```bash
# 语法检查
python -m compileall app

# 本地启动
cd app
uvicorn main:app --host 0.0.0.0 --port 7860
```
