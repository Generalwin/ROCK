# E2B `sandbox.commands.run()` 协议调研与 ROCK 最小兼容方案

> 调研快照：2026-08-21
> 范围：E2B Python/JS SDK 调用 `sandbox.commands.run()` 时实际使用的数据面协议，以及 ROCK 的最小兼容方案。
> 不在范围：本文件不设计完整 E2B filesystem、PTY、snapshot、pause/resume 等兼容面，也不修改产品代码。

## 1. 结论先行

> **2026-08-28 本期实现决策**：前置网关负责 API-key 鉴权与集群路由；ROCK admin 将收到的原始 `X-API-Key` 作为 `envdAccessToken` 返回，E2B SDK随后以 `X-Access-Token` 携带同一值访问数据面。ROCK proxy 只读取执行所需的 `E2b-Sandbox-Id`，不校验 `E2b-Sandbox-Port`、`X-Access-Token`、`Content-Type` 或 `Connect-Protocol-Version`。由于验收调用会显式传 `stdin=False`，本期统一声明 `envdVersion=0.3.0`；`stdin=True` 仍返回 `unimplemented`。受 Rocklet 固定请求 deadline 限制，本期只接受 `1..60000ms` 的 command timeout，`0`、缺失或更长 timeout 明确返回 `unimplemented`。本文后续关于 per-sandbox token 的内容保留为生产加固建议，不属于本期范围。

1. **当前 E2B SDK 的 `commands.run()` 不是 REST JSON、WebSocket 或 SSE。**它调用的是 envd 的 **Connect RPC over HTTP**：`POST /process.Process/Start`，请求和响应均为 `application/connect+json`，方法类型是“一条请求、服务端流式响应”。官方 proto 把 `Start` 定义为 server-streaming RPC；Python 生成客户端也固定了这个 path 和方法类型。[Process proto](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/spec/envd/process/process.proto#L5-L20) [Python generated client](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/envd/process/process_connect.py#L160-L219)
2. **Python sync、Python async、JS/TS 三种调用共用同一 wire contract。**差别只在 SDK 如何等待首帧、消费流和调用 callback；服务端无需为语言或同步/异步分别提供接口。[Python sync start](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_sync/commands/command.py#L244-L339) [Python async start](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_async/commands/command.py#L247-L343) [JS start](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/commands/index.ts#L417-L486)
3. **最小可用数据面是 `POST /process.Process/Start`，建议同时实现失败探测用的 `GET /health`。**正常 `run()` 只请求 `Start`；底层连接异常时 SDK 可能再探测 `/health`，用于区分 sandbox 已死亡和瞬时网络错误。[Python health probe](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/envd/api.py#L33-L62) [JS health probe](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/envd/api.ts#L34-L61)
4. **`on_stdout`/`on_stderr` 不存在独立的服务端能力或参数。**它们只是在客户端收到 stdout/stderr data event 后执行的 callback。ROCK 可以继续使用现有缓冲式 `execute()`，命令结束后各发一个 stdout/stderr event；这样不是实时输出，但 `CommandResult.stdout/stderr` 正确，callback 也会在末尾各触发一次。[E2B streaming docs](https://e2b.dev/docs/commands/streaming) [JS event consumer](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/commands/commandHandle.ts#L270-L374)
5. **不能把输出只塞进最终 `end` event。**`end` 只有 `exit_code/exited/status/error`，stdout/stderr 只能通过此前的 data event 返回；否则 SDK 得到的 stdout/stderr 必然为空。[Process event schema](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/spec/envd/process/process.proto#L69-L105)
6. **`stdin` 最小实现仍必须接受请求中的 `stdin:false`。**当前 Python/JS SDK 即使调用方未传 stdin，也会把默认值 `false` 放进 StartRequest。可以对 `stdin:true` 返回 Connect `unimplemented`/`invalid_argument`，并不实现 `/SendInput`、`/CloseStdin`。[Python request construction](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_sync/commands/command.py#L257-L315) [JS request construction](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/commands/index.ts#L426-L465)
7. **命令非零退出不是 RPC 错误。**服务端仍应正常发送 stdout/stderr、再发送 `end.exitCode != 0`；SDK 随后在本地抛 `CommandExitException`（Python）或 `CommandExitError`（JS），异常对象携带 exit code 和完整输出。[Python wait](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_sync/commands/command_handle.py#L160-L206) [JS wait](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/commands/commandHandle.ts#L160-L182)
8. **ROCK 当前 master 已有 E2B 控制面，但没有这个数据面。**历史分支 `feat/e2b-control-plane-api-1293` 曾实现并用官方 Python `e2b==2.34.0` 跑通过；它未合入 master，且之后在同一历史分支又经历多轮安全/并发修正，不能直接照搬或整提交 cherry-pick。详见第 10 节。

完整调用链和最小/后续接口边界见 [可编辑 draw.io 图](../research/e2b-commands-run-flow.drawio) 与 [PNG 预览](../research/e2b-commands-run-flow.png)：

![E2B commands.run 调用流与 ROCK 最小兼容边界](../research/e2b-commands-run-flow.png)

## 2. 版本与一手资料基线

本次以两个官方仓库的不可变 commit 为基线：

- SDK monorepo：[`e2b-dev/E2B@5995e0ad`](https://github.com/e2b-dev/E2B/tree/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5)，2026-08-20 的 release commit；该快照的 Python manifest 为 `2.44.0`，JS manifest 为 `2.44.1`。[Python manifest](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/pyproject.toml#L1-L20) [JS manifest](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/package.json#L1-L18)
- envd 服务端：[`e2b-dev/infra@78ab8bb5`](https://github.com/e2b-dev/infra/tree/78ab8bb5576217abcc83c224026540c3ed982e13)，2026-08-21 快照。

本仓 [uv.lock](../../uv.lock#L1280) 仍锁定 Python `e2b==2.34.0`。官方 tag 对比结果如下：

| 基线 | Python transport 实现 | `commands.run()` wire 是否变化 |
|---|---|---|
| 2.34.0 | E2B 仓库内 vendored `e2b_connect` | `POST /process.Process/Start`、Connect JSON、5-byte envelope |
| 2.37.0 | 已迁移到 `connectrpc` + JSON codec；SDK 内部 transport 大改 | **未变** |
| 2.44.0 / JS 2.44.1 | Python 使用 `connectrpc`/`pyqwest`，JS 使用 `@connectrpc/connect-web` | **未变** |

2.34 的官方 vendored client 明确给出了 `>BI`（1 byte flags + 4 byte big-endian length）的 framing、`application/connect+json`、`Connect-Timeout-Ms` 和 stream parser；当前 Python 则明确声明自定义 JSON codec 是为了继续匹配 JS 的 `useBinaryFormat:false`，不是切到 protobuf binary。[2.34 framing/client](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b_connect/client.py#L19-L84) [2.34 server-stream request](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b_connect/client.py#L331-L388) [current Python JSON codec](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/envd/client_shared.py#L29-L60) [current JS JSON transport](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/index.ts#L161-L200)

因此，ROCK 应以 Connect+JSON wire 为兼容边界，而不是依赖某一版 Python SDK 的内部 transport 类。

本轮还用真实 `e2b==2.44.0` 对 loopback stub 做了额外交叉验证：5-byte Connect+JSON framing、stdout、exit code、非零退出的 `CommandExitException` 均与上述源码结论一致。该次 HTTP/1.1 请求使用 chunked transfer、没有 `Content-Length`；这是实现测试证据，不替代上面的官方源码依据。

## 3. SDK 实际调用链

### 3.1 Python sync

```text
Sandbox.commands
  -> Commands.run(cmd, ...)
  -> Commands._start(...)
  -> ProcessClientSync.start(StartRequest, timeout_ms=...)
  -> POST {envd_api_url}/process.Process/Start
  -> 先取 start event 得到 pid
  -> CommandHandle.wait() 消费 data/end events
  -> CommandResult 或 CommandExitException
```

`run(background=False/None)` 调 `wait()`；`background=True` 返回同一个 `CommandHandle`。`background` 不进入请求体。[Python sync run](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_sync/commands/command.py#L182-L284)

### 3.2 Python async

调用链和 wire 相同；差别是 async SDK 用 `first_event()` 单独约束等待首个 start event 的时间，随后由 `AsyncCommandHandle` 后台 task 消费流并执行同步或异步 callback。[Python async first event](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/envd/client_async/__init__.py#L101-L143) [AsyncCommandHandle](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_async/commands/command_handle.py#L81-L120)

### 3.3 JS/TS

```text
Sandbox.commands
  -> Commands.run(cmd, opts)
  -> Commands.start(...)
  -> Connect client Process.start(...)
  -> POST {envdApiUrl}/process.Process/Start
  -> 首帧必须是 start event
  -> CommandHandle.wait() 消费 data/end events
  -> CommandResult 或 CommandExitError
```

JS 同样只在客户端根据 `opts.background` 决定返回 handle 还是等待结果。[JS run/start](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/commands/index.ts#L374-L486)

## 4. 涉及的接口矩阵

| 场景 | 接口 | Method / 类型 | 是否属于本期最小集 |
|---|---|---|---|
| `commands.run()`，前台或后台初始调用 | `/process.Process/Start` | `POST`，Connect server stream | **必须** |
| RPC 连接异常后的健康判断 | `/health` | `GET`，普通 HTTP | **建议必须** |
| `CommandHandle.kill()` / `commands.kill()` | `/process.Process/SendSignal` | `POST`，Connect unary | 后续 |
| `CommandHandle.send_stdin()` / `commands.send_stdin()` | `/process.Process/SendInput` | `POST`，Connect unary | 本期不做 |
| `CommandHandle.close_stdin()` | `/process.Process/CloseStdin` | `POST`，Connect unary | 本期不做 |
| `commands.connect(pid)` | `/process.Process/Connect` | `POST`，Connect server stream | 后续 |
| `commands.list()` | `/process.Process/List` | `POST`，Connect unary | 后续 |

这些方法及其类型全部来自同一个官方 Process service 定义；生成代码将 path 固定为 `/process.Process/<Method>`。[Proto service](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/spec/envd/process/process.proto#L5-L20) [Generated paths](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/envd/process/process_connect.py#L67-L147)

没有任何 WebSocket upgrade，也没有 `text/event-stream`。长连接依赖普通 HTTP streaming；E2B Python transport 在 TLS 上通过 ALPN 使用 HTTP/2，但允许 HTTP/1.1 fallback，JS 的 fetch/connect-web 也不要求 WebSocket。[Python transport HTTP version](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/api/client_sync/__init__.py#L66-L116) [JS Connect transport](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/index.ts#L168-L200)

## 5. URL、routing headers 与鉴权

### 5.1 Base URL

SDK 按以下优先级计算 envd base URL：

1. 显式 `E2B_SANDBOX_URL` / `sandbox_url`：直接作为 base URL；
2. debug：`http://localhost:49983`；
3. E2B 支持的托管 domain：当前服务端 runtime 使用 `https://sandbox.<domain>`；
4. 其他 domain / browser fallback：`https://49983-<sandbox-id>.<domain>`。

Python 与 JS 的实现分别见 [Python URL calculation](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/connection_config.py#L284-L320) 和 [JS URL calculation](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/connectionConfig.ts#L493-L535)。

对 ROCK 最简单的部署配置是：

```bash
E2B_API_URL=https://<rock-admin-or-control-plane>
E2B_SANDBOX_URL=https://<rock-proxy-data-plane>
```

这样无需 wildcard DNS/TLS；proxy 用 header 选择 sandbox。

### 5.2 `Start` 请求的有效 headers

| Header | 来源 / 语义 | 最小服务端处理 |
|---|---|---|
| `Content-Type: application/connect+json` | Connect JSON server stream | SDK会发送；ROCK 手动解析 envelope，不校验该 header |
| `Connect-Protocol-Version: 1` | Connect protocol | SDK会发送；ROCK 不使用、不校验 |
| `Connect-Timeout-Ms: <ms>` | command/stream 总时限；E2B 中 `0`/缺失为无限 | 本期仅接受 `1..60000` 并映射为 ROCK command timeout；其他值明确拒绝 |
| `Keepalive-Ping-Interval: 50` | SDK 要求无输出期间约每 50 秒发 keepalive | 长命令应支持 |
| `E2b-Sandbox-Id: <id>` | 固定 gateway 下的 sandbox 路由 | 必须校验并路由 |
| `E2b-Sandbox-Port: 49983` | envd 固定逻辑端口 | 由网关处理；ROCK 不使用、不校验 |
| `X-Access-Token: <token>` | 控制面 create/connect 返回的数据面凭据 | 由网关鉴权；ROCK 不使用、不校验 |
| `Authorization: Basic base64("<user>:")` | sandbox 内运行用户，不是平台 API 鉴权 | 最小只接受默认用户，或映射到 ROCK remote user |
| `User-Agent` | `e2b-python-sdk/x` / `e2b-js-sdk/x` | 观测用，不能当鉴权 |

SDK 在创建 `Sandbox` 对象时把 sandbox id、port 和 envd token放入 envd/RPC headers；`user` 则单独编码为 Basic auth。[Python sandbox headers](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_sync/main.py#L1120-L1164) [Python user header](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/envd/utils.py#L44-L57) [JS sandbox headers/token](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/index.ts#L161-L200) [JS user header](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/envd/rpc.ts#L147-L180)

### 5.3 一个必须避免的安全误区

`E2B_API_KEY`/`X-API-Key` 只用于控制面；当前 SDK **不会把它自动转发到 envd RPC**。JS 官方测试还专门断言自定义 `Authorization`/`X-Custom` 控制面 headers 不会泄露到 RPC，只留下 User-Agent、routing headers 和 `X-Access-Token`。[Control-plane API key](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/api/index.ts#L87-L127) [RPC header isolation test](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/tests/sandbox/rpcHeaders.test.ts#L20-L48)

因此如果 ROCK create response 不签发 `envdAccessToken`，`/process.Process/Start` 不能假设还会收到控制面凭据。生产方案应：

1. create/connect 时生成高熵 per-sandbox token；
2. response 返回明文 `envdAccessToken`；
3. ROCK 只持久化摘要；
4. 数据面用常量时间比较校验 `X-Access-Token`；
5. sandbox delete/owner 变化时使 token 失效。

E2B 控制面 schema把 `envdAccessToken` 定义为可选字段，SDK会在构造 sandbox handle 时读取并转发它。[Python response model](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/api/client/models/sandbox.py#L12-L33) [Python response parsing](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/api/client/models/sandbox.py#L82-L129)

这不是假设中的缺口：官方 Python 2.34 至当前版本的 `Sandbox.create()` 都默认 `secure=True`，语义就是 envd 必须用 access token 保护；而当前 ROCK master 的 create request 虽接收 `secure`，`E2BCreateSandboxResponse` 却没有 `envdAccessToken` 字段，入口也没有生成 token。[Official secure default](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_sync/main.py#L164-L191) [ROCK request model](../../rock/admin/proto/request.py#L19-L31) [ROCK response model](../../rock/admin/proto/response.py#L12-L18) [ROCK create response](../../rock/admin/entrypoints/e2b_api.py#L68-L92) 因此若只加 Start route 而不补 token，功能可以在可信内网跑通，但不能称为 secure E2B 数据面兼容。

## 6. `POST /process.Process/Start` 的精确 wire contract

### 6.1 Request framing

请求 HTTP body 不是裸 JSON，而是一条 Connect envelope：

```text
+-----------+----------------------+--------------------------+
| flags 1B  | payload length 4B BE | JSON payload N bytes     |
+-----------+----------------------+--------------------------+
| 0x00      | N                    | StartRequest JSON        |
+-----------+----------------------+--------------------------+
```

官方 2.34 client 和当前官方 frame-level tests 均固定这个结构；当前测试还用 `0x02` 表示 end-stream envelope。[2.34 encoder](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b_connect/client.py#L70-L84) [Current frame test harness](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/tests/envd_frame_server.py#L43-L57)

**不能依赖 `Content-Length`。**当前 Python 2.44 在 HTTP/1.1 loopback 下可用 `Transfer-Encoding: chunked` 发送这条请求，HTTP/2 本身也没有 chunked/`Transfer-Encoding` 语义。FastAPI/Starlette handler 应通过 `async for chunk in request.stream()` 有界累计，边读边执行总大小限制；`Content-Length` 只可作为存在时的提前拒绝优化。

`commands.run("echo hi", envs={"X":"a"}, cwd="/tmp")` 的 JSON 语义是：

```json
{
  "process": {
    "cmd": "/bin/bash",
    "args": ["-l", "-c", "echo hi"],
    "envs": {"X": "a"},
    "cwd": "/tmp"
  },
  "stdin": false
}
```

JS 输出 compact JSON；Python codec 当前可能包含空格。服务端必须按 JSON 解析，不能做 raw byte 比较。字段 schema 来自 `ProcessConfig`/`StartRequest`；`run()` 始终把用户命令放进 `/bin/bash -l -c`。[Proto request schema](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/spec/envd/process/process.proto#L32-L59) [JS request construction](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/commands/index.ts#L447-L465)

参数到 wire 的映射：

| SDK 参数 | Wire 位置 |
|---|---|
| `cmd` | `process.args[2]`，同时固定 `process.cmd=/bin/bash`、前缀 args `[-l,-c]` |
| `envs` | `process.envs` |
| `cwd` | `process.cwd` |
| `stdin` | 顶层 `stdin` |
| `user` | HTTP `Authorization: Basic ...` |
| Python `timeout` / JS `timeoutMs` | HTTP `Connect-Timeout-Ms` |
| `background` | **不在 wire 中；仅客户端分支** |
| `on_stdout` / `on_stderr` | **不在 wire 中；仅客户端 callback** |
| Python `request_timeout` / JS `requestTimeoutMs` | 客户端等待/abort 控制，不是 StartRequest 字段 |

### 6.2 Response framing 与事件顺序

成功响应：

```http
HTTP/1.1 200 OK
Content-Type: application/connect+json
Transfer-Encoding: chunked
```

HTTP/2 下没有 `Transfer-Encoding`，但 body 仍是相同 Connect envelope 流。每个 message 都是 `flags=0x00 + 4-byte length + JSON`；最终再发一条 `flags=0x02`、payload `{}` 的 Connect end-stream envelope。官方 frame test 以同样结构模拟真实 envd 响应。[Official frame response](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/tests/envd_frame_server.py#L123-L138)

事件顺序必须是：

1. **首条必须是 start**，否则 SDK直接报错：

   ```json
   {"event":{"start":{"pid":42}}}
   ```

   [Python first-event validation](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/envd/utils.py#L27-L41) [JS first-event validation](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/envd/api.ts#L143-L166)
2. 零到多条 stdout/stderr data；bytes 使用 base64：

   ```json
   {"event":{"data":{"stdout":"aGkK"}}}
   {"event":{"data":{"stderr":"d2FybmluZwo="}}}
   ```

3. 长时间无输出时可发 keepalive：

   ```json
   {"event":{"keepalive":{}}}
   ```

4. 正常进程结束发 end：

   ```json
   {
     "event": {
       "end": {
         "exitCode": 7,
         "exited": true,
         "status": "exit status 7",
         "error": "exit status 7"
       }
     }
   }
   ```

5. 最后发 Connect end-stream：`flags=0x02, payload={}`。

真实 envd 把 stdout/stderr 拆成 data events，并从 `ProcessState` 生成 `exitCode/exited/status/error`。[envd output events](https://github.com/e2b-dev/infra/blob/78ab8bb5576217abcc83c224026540c3ed982e13/packages/envd/internal/services/process/handler/handler.go#L341-L420) [envd end event](https://github.com/e2b-dev/infra/blob/78ab8bb5576217abcc83c224026540c3ed982e13/packages/envd/internal/services/process/handler/handler.go#L569-L621)

`exitCode=0` 在 protobuf JSON 中可省略（默认值仍为 0）；ROCK 显式发送 `0` 也合法。`error` 是 optional，零退出时应省略，非零时建议填入 `exit status N`，否则 JS `CommandExitError.message` 会缺失。[Command result/error](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/commands/commandHandle.ts#L17-L77)

### 6.3 Connect stream error

如果响应已开始，不能再改 HTTP status。应发送最终 `flags=0x02` envelope：

```json
{
  "error": {
    "code": "deadline_exceeded",
    "message": "command timed out"
  }
}
```

官方 2.34 stream parser 在 `end_stream` envelope 中读取 `error` 并转成 SDK exception；这个 wire 语义在当前 Connect client 中保持不变。[Official stream parser](https://github.com/e2b-dev/E2B/blob/43db96a0ef2e555b96eee1a52856013fbf0dc644/packages/python-sdk/e2b_connect/client.py#L477-L533)

如果在发送 200/首帧前即可完成校验，也可以返回标准 Connect HTTP error（如 `400` + `{"code":"invalid_argument","message":"..."}`）。为简化 Starlette 实现，始终用 `200 application/connect+json` + error end envelope 也能被当前 SDK 正确识别，但必须保证 envelope 合法。

## 7. 同步、异步、流式、timeout 与退出语义

### 7.1 前台与后台

- 前台：SDK收到 start 后继续消费原流，直到 end，返回 `CommandResult` 或抛非零退出异常。
- 后台：SDK收到 start 后立即把 `CommandHandle(pid)` 返回给用户；原流仍保持并继续传输。官方文档明确 `background:true` 返回 handle，命令继续运行。[Background docs](https://e2b.dev/docs/commands/background)
- 因为 `background` 不上 wire，服务端不能判断调用方选了前台还是后台。因此 `Start` handler 应始终：快速发送 start、让命令生命周期独立于 HTTP request cancellation、随后继续流式发送结果。

真实 envd 特意用 `context.Background()` 派生命令 context，并注明“request context 被取消时不应杀死命令”；请求断开只结束订阅，command timeout 才控制进程生命周期。[envd Start context](https://github.com/e2b-dev/infra/blob/78ab8bb5576217abcc83c224026540c3ed982e13/packages/envd/internal/services/process/start.go#L23-L45)

### 7.2 Callback

- Python sync：`wait()` 在迭代 data event 时直接调用 callback。[Python sync callback](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_sync/commands/command_handle.py#L160-L183)
- Python async：后台 consumer 对返回 awaitable 的 callback 执行 await。[Python async callback](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_async/commands/command_handle.py#L219-L239)
- JS：同样在 event consumer 中 await callback。[JS callback](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/commands/commandHandle.ts#L344-L374)

所以本期可以不做 ROCK 实时输出 seam，只做“命令完成后输出一次”；不能省略 data event。

### 7.3 两类 timeout

| 概念 | Python | JS | wire/效果 |
|---|---|---|---|
| command/stream timeout | `timeout`，默认 60 秒，`0` 无限 | `timeoutMs`，默认 60,000 ms，`0` 无限 | `Connect-Timeout-Ms`；控制整个长请求，也被真实 envd用作进程 timeout |
| request/start timeout | `request_timeout`，默认连接配置 60 秒 | `requestTimeoutMs`，默认 60,000 ms | 客户端等待首个 start event/建立连接的本地上限；不是 StartRequest 字段 |

当前 Python sync 对 streaming call 不再单独应用 `request_timeout`，而是由 `timeout`/transport connect timeout 约束；Python async 和 JS 会在收到 start 后清除首帧 timer。[Python sync docs/source](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_sync/commands/command.py#L196-L210) [Python async first event](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_async/commands/command.py#L311-L323) [JS request controller](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/connectionConfig.ts#L126-L193)

真实 envd读取 `Connect-Timeout-Ms`，用它建立 process context deadline；0/缺失表示不设 deadline。[envd timeout parser](https://github.com/e2b-dev/infra/blob/78ab8bb5576217abcc83c224026540c3ed982e13/packages/envd/internal/services/process/start.go#L222-L234) [envd process deadline](https://github.com/e2b-dev/infra/blob/78ab8bb5576217abcc83c224026540c3ed982e13/packages/envd/internal/services/process/start.go#L34-L45)

完整实现应把 header 毫秒换成秒传给 `SandboxCommand.timeout`，并把缺失/0 映射为 `None`。但当前 Rocklet 无法兑现无限或超过 60 秒的 E2B deadline，因此本期只接受 `1..60000ms`；缺失、0 或更长值返回 `unimplemented`，避免静默落回 ROCK 默认值或被 85 秒 middleware 提前终止。

### 7.4 错误映射

核心映射应是：

| 情况 | Connect code / event | Python | JS |
|---|---|---|---|
| 请求/body/cwd非法 | `invalid_argument` | `InvalidArgumentException` | `InvalidArgumentError` |
| token 无效 | `unauthenticated` | `AuthenticationException` | `AuthenticationError` |
| sandbox/process 不存在 | `not_found` | `NotFoundException` | `NotFoundError`；首帧 unavailable 还有 sandbox-specific 处理 |
| 并发/限流 | `resource_exhausted` | `RateLimitException` | `RateLimitError` |
| sandbox 不可达 | `unavailable` | `TimeoutException` | `TimeoutError` |
| request 被取消 | `canceled` | async 外部取消恢复为 `CancelledError`；其他映射 timeout | `TimeoutError` |
| command deadline | `deadline_exceeded` | `TimeoutException` | `TimeoutError` |
| 正常进程退出码非 0 | 正常 `end.exitCode != 0`，不是 Connect error | `CommandExitException` | `CommandExitError` |

SDK 映射来自 [Python RPC error map](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/envd/rpc.py#L18-L32) 与 [JS RPC error map](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/envd/rpc.ts#L69-L86)。

如果 response stream 在没有 end event 的情况下直接 EOF，SDK会报“Command ended without an end event”/“Process exited without a result”；因此无论成功、非零退出还是兼容层错误，都必须以合法 end/end-stream 结束。[Python missing end](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/sandbox_async/commands/command_handle.py#L241-L263) [JS missing end](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/js-sdk/src/sandbox/commands/commandHandle.ts#L166-L181)

## 8. 推荐的最小兼容边界

### 8.1 本期承诺

`POST /process.Process/Start`：

- 接受 Connect JSON identity encoding；不要求 protobuf binary/gzip。
- 只从 routing headers 中读取 `E2b-Sandbox-Id`；端口、token、Content-Type 和 Connect 版本由网关/SDK协议层负责。继续解析会影响执行语义的 Basic user、timeout 和 keepalive。
- 支持 SDK 标准 command shape：`/bin/bash -l -c <cmd>`。
- 支持 `envs`、`cwd`、timeout。
- 接受 `stdin:false`；`stdin:true` 返回 `unimplemented`。
- 先返回 synthetic pid 的 start event。
- 调用现有 ROCK buffered `execute()`。
- 完成后各发至多一条 stdout/stderr data event，再发 end 和 Connect end-stream。
- 非零 exit code 原样放在 end，补 `error: "exit status N"`。
- ROCK timeout 转成 `deadline_exceeded` error envelope。
- command task 不因客户端断流而取消，保证 `background=True` 的基本语义。

`GET /health`：

- 只要求 `E2b-Sandbox-Id`；
- sandbox running 返回 `204` 空 body；
- sandbox 不存在/已停止返回 `502`（SDK会判断为 sandbox 已不运行）；
- 只读 meta store，不调用会刷新 TTL 的旧 status API。

官方 envd 的 health OpenAPI 成功状态就是 `204`，实际 handler 也返回无 body 204。[envd OpenAPI health](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/spec/envd/envd.yaml#L17-L23) [envd health handler](https://github.com/e2b-dev/infra/blob/78ab8bb5576217abcc83c224026540c3ed982e13/packages/envd/internal/api/store.go#L130-L139)

### 8.2 明确不承诺

- 实时 stdout/stderr；只在结束后一次性发送。
- `stdin:true`、send/close stdin。
- `CommandHandle.kill()`。
- `commands.connect/list()`。
- 跨 proxy worker 的 durable PID/process registry。
- PTY。

注意：`commands.run(..., background=True)` 的“立即返回 handle + 原连接上 `handle.wait()`”可工作；但 handle 的 kill/reconnect 不在本期范围。需要在用户文档里把这条边界写清楚。

### 8.3 `envdVersion` 决策

本期返回 `0.3.0`。原因是官方 SDK在调用方显式传 `stdin=False` 且版本 `<0.3.0` 时，会在客户端直接拒绝调用；ROCK adapter 已接受并丢弃 `stdin:false`，因此可以准确声明这项能力。`stdin:true`、SendInput、CloseStdin 和 PTY 仍不支持并返回 `unimplemented`。

不要继续提高到 `0.4.0+`：更高版本会启用默认用户、close-stdin、filesystem upload/metadata 等尚未实现的 feature gate。feature gate 常量见 [Python envd versions](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/envd/versions.py#L1-L12)。create、get、list 返回的版本必须保持一致。

## 9. ROCK 落地方案

### 9.1 路由位置

当前 master 已拆成：

- admin role：`rock/admin/entrypoints/e2b_api.py`，承载 `/sandboxes` create/get/delete；
- proxy role：`rock/admin/entrypoints/e2b_proxy_api.py`，当前只承载 `/v2/sandboxes` list；
- proxy 创建统一的 sandbox backend，并将其直接注入 `E2BProxyService`。

因此建议新增独立 `e2b_envd_api.py`（或在 `e2b_proxy_api.py` 中增加窄 router），只在 proxy role mount 根路径：

```text
POST /process.Process/Start
GET  /health
```

`E2BProxyService` 直接持有 composition root 传入的 `SandboxProxyService`（或其 `OpenSandboxProxyService` 子类），调用 `execute(..., propagate_rocklet_errors=True)`，不增加纯转发 Adapter 或 E2B 专用 execute 方法。不要把完整历史版大文件重新塞回当前 control-plane `e2b_api.py`。

### 9.2 执行序列

```text
request
  -> 从 header 读取必需的 sandbox id，忽略 ROCK 不使用的协议/端口/token headers
  -> 有界读取并解析单条 request envelope
  -> 校验 process shape / stdin / env / cwd / timeout
  -> meta store 确认 sandbox running
  -> 创建独立 asyncio task 执行 SandboxProxyService.execute()
  -> 分配 synthetic uint32 pid，发送 start
  -> 每隔 keepalive interval 等待 task；未完成则发 keepalive
  -> task 完成后发 stdout/stderr data
  -> 发 end(exitCode/exited/status/error)
  -> 发 Connect end-stream `{}`
```

映射到 ROCK：

```python
SandboxCommand(
    sandbox_id=<E2b-Sandbox-Id>,
    command=[process["cmd"], *process.get("args", [])],
    env=process.get("envs") or None,
    cwd=process.get("cwd") or None,
    timeout=connect_timeout_ms / 1000,  # 本期已先校验 1..60000ms
    shell=False,
    check=False,
)
```

不要把 `args[2]` 作为单个 string 且 `shell=False` 传给 rocklet；E2B SDK已经明确给出了 `/bin/bash -l -c` argv，按 argv 透传最安全，也最接近官方行为。

### 9.3 StreamingResponse 注意事项

- `StreamingResponse(..., media_type="application/connect+json")` 直接 yield raw bytes envelope；不是 SSE generator。
- 在返回 `StreamingResponse` 前完成 sandbox id/body 的可同步校验；token 由网关校验，开始流后所有错误都走 Connect end error envelope。
- 给 task 建立有界强引用 registry，client disconnect 时只关闭订阅，不取消 command task。
- ingress/LB 必须关闭 response buffering，并允许至少 60 秒以上的长响应；否则 SDK只会看到 transport error。
- keepalive interval 以请求 header 为准，做上下界限制；官方 envd默认 90 秒，但 SDK当前固定请求 50 秒，并在有 data 时重置 ticker。[SDK keepalive constants](https://github.com/e2b-dev/E2B/blob/5995e0ad1cb7b2fba9ce7c5ae2c0acb3c86d46a5/packages/python-sdk/e2b/connection_config.py#L22-L37) [envd keepalive](https://github.com/e2b-dev/infra/blob/78ab8bb5576217abcc83c224026540c3ed982e13/packages/envd/internal/permissions/keepalive.go#L10-L28)

### 9.4 后续完整进程语义

若要把 `background=True` 宣称为完整兼容，下一阶段至少增加：

- `/process.Process/SendSignal`：handle.kill；
- `/process.Process/Connect`：重新订阅；
- `/process.Process/List`：发现运行进程；
- actual PID 或跨 worker durable synthetic PID -> backend process handle 映射；
- 已退出结果的短期 retention。

真实 envd 的 process registry 与 late-connect exit retention 都是专门实现的，不是一个 request-local task 就能等价替代。[envd process registry/retention](https://github.com/e2b-dev/infra/blob/78ab8bb5576217abcc83c224026540c3ed982e13/packages/envd/internal/services/process/service.go#L21-L38) [envd Connect behavior](https://github.com/e2b-dev/infra/blob/78ab8bb5576217abcc83c224026540c3ed982e13/packages/envd/internal/services/process/connect.go#L19-L55)

### 9.5 当前 ROCK 执行链上还需同时修正的三个阻断点

1. **timeout/异常信息必须在 E2B 调用中保留。**Rocklet 用 HTTP 511 传输序列化异常、用 504 表示 middleware timeout；`execute()` 默认保留 ROCK 历史失败结果语义，E2B 调用显式传 `propagate_rocklet_errors=True` 启用结构化异常传播，由 `E2BProxyService` 映射为 `deadline_exceeded`、`invalid_argument`、`unavailable` 或 `internal`。[proxy error handling](../../rock/sandbox/service/sandbox_proxy_service.py) [CommandResponse](../../rock/actions/sandbox/response.py#L112-L116)
2. **ROCK 当前不能兑现任意 E2B command timeout。**Rocklet 对所有 HTTP 请求有固定 85 秒 middleware deadline；proxy 的 `rpc` httpx pool 默认总 timeout 为 180 秒。[Rocklet timeout middleware](../../rock/rocklet/server.py#L73-L80) [ROCK request timeout constant](../../rock/utils/__init__.py#L49) [proxy pool timeout](../../rock/config.py#L570-L575) 默认 E2B 60 秒能工作，但 `timeout>85` 和 `timeout=0` 不能按 E2B 语义工作。完整实现应让 `/execute` 依赖 `SandboxCommand.timeout`，并按 command timeout 给 proxy 请求设置相应 deadline；如果首期不改，必须显式把能力矩阵限定为 `timeout<=60s`，而不是静默提前终止。
3. **复用现有 `SandboxProxyService.execute()` 会刷新 ROCK TTL。**该方法执行前调用 `_update_expire_time()`，而 E2B command timeout 与 sandbox 生命周期 timeout 是两个概念。[execute](../../rock/sandbox/service/sandbox_proxy_service.py#L257-L265) [TTL refresh](../../rock/sandbox/service/sandbox_proxy_service.py#L897-L903) 是否保留 sliding TTL 必须作为兼容策略显式决定；不要把它误写成 E2B 原生语义。

当前部署决策允许 admin access log 原样记录完整 request headers，包括 `X-Access-Token`、`Authorization` 等字段；因此日志系统本身必须有相应的访问控制和保留策略。[access log](../../rock/admin/main.py#L310-L335)

## 10. 本仓历史实现：可复用证据与不能直接照搬的部分

### 10.1 证据位置

以下内容都在**未合入 master**的本地历史分支 `feat/e2b-control-plane-api-1293`：

```bash
git show feat/e2b-control-plane-api-1293:docs/research/e2b-api-protocol.md
git show b7871a9fd:rock/admin/entrypoints/e2b_api.py
git show b7871a9fd:tests/unit/admin/entrypoints/test_e2b_envd_compat.py
git show cd209fdcc:.superpowers/sdd/task-6-report.md
git show 4d64c55e5:tests/unit/admin/entrypoints/test_e2b_sdk_compat.py
```

`git merge-base --is-ancestor b7871a9fd HEAD` 和 `git merge-base --is-ancestor cd209fdcc HEAD` 当前都返回非零，确认它们不是 master 历史。

### 10.2 已验证的价值

提交 `b7871a9fd` 已有：

- 5-byte Connect envelope parser/encoder；
- `/process.Process/Start` + `/health`；
- `/bin/bash -l -c`、env、cwd、timeout 映射；
- synthetic pid、start/keepalive/data/end 顺序；
- client disconnect 后 task 继续；
- `stdin:true`/PTY 明确拒绝；
- buffered ROCK output 转 stdout/stderr event；
- Connect error end envelope。

提交 `cd209fdcc` 的报告记录：用真实 loopback TCP + 官方 `e2b==2.34.0`，同步和异步 Python SDK 的 create/run/files/health/get/list/connect/timeout/kill 两条 E2E 均通过，测试没有 mock SDK transport。这说明其基本 wire 判断是对的，不只是单元测试自证。

### 10.3 不能直接 cherry-pick 的原因

1. 原型把控制面与 envd 数据面合在一个超大 `e2b_api.py`，并只 mount 在当时的 admin role；当前 master 已拆 admin/proxy role。
2. 原型只验证 Python 2.34，没有验证 JS/TS，也没有对当前 2.44 source 做 E2E。
3. `b7871a9fd` 不是该历史分支的最终状态；其后还有 token rotation、owner fencing、并发、上传关闭、日志脱敏等多轮修正。仅 `b7871a9fd..feat/e2b-control-plane-api-1293` 对核心 route/test 就有数百行变化。
4. PID/task registry 是单进程内存结构，不支持多 proxy worker 的 kill/connect/list。
5. 原型的非零 end event没有填 `error`；Python 仍可从 exit code 抛异常，但当前 JS `CommandExitError.message` 会不完整。
6. 原型声明 `envdVersion=0.1.0` 并只接受默认 `user:`；这适合其当时最小集，但如果当前调用方显式传 `stdin=False` 或自定义 user，需要重新决定 feature gate/用户执行模型。
7. 当前 master 的 `E2BProxyService`/metadata 模型、鉴权头与历史分支已经不同，必须以当前 seam 实现窄兼容层，而不是恢复历史分支整体架构。

建议做法：把历史提交当作**测试向量和 framing 参考**，重新在当前 proxy seam 上实现；先移植/更新 protocol tests，再写新 route。

## 11. 验收矩阵

### 11.1 官方 SDK E2E（真实 TCP，不 mock transport）

至少覆盖：

| SDK | 版本 |
|---|---|
| Python sync / async | 本仓锁定 2.34.0 |
| Python sync / async | 2.37.0（transport 迁移基线） |
| Python sync / async | 2.44.0 当前官方 source tag |
| JS/TS | 2.44.1 当前官方 source tag，或当时 npm 可安装的最新稳定版 |

用 `api_url` 指向 admin，`sandbox_url` 指向 proxy；至少验证：

1. stdout+stderr、env、cwd、exit 0；
2. exit 23：SDK抛 CommandExit 异常，异常上 exit code/stdout/stderr/error 正确；
3. timeout：SDK抛 Timeout，不是缺 end event；
4. `background=True` 在命令完成前返回，`handle.wait()` 可取结果；断开 client 后后台 task 仍完成；
5. 调用方不传 stdin时成功；`stdin=True` 收到清晰 unimplemented；
6. 长时间无输出收到 keepalive；
7. sandbox id 缺失、sandbox stopped；确认 port/token 等 ROCK 不使用的 headers 不影响请求；
8. transport 故障时 `/health` 返回 204/502，SDK分类正确。

### 11.2 Raw wire tests

- flags、big-endian length、truncated/trailing envelope；
- 非 JSON、过大 body、未知字段；
- ROCK 不使用的 protocol/routing headers 被忽略；
- stdout/stderr base64；
- start 必须第一条、end 必须存在、最后 0x02 envelope；
- 200 已发出后的 invalid_argument/internal/deadline_exceeded error envelope；
- proxy response buffering 关闭后的长流测试。

### 11.3 ROCK 回归

- 现有 `/sandboxes`、`/v2/sandboxes` 与 ROCK 原生 `/apis/envs/sandbox/v1/*` 不变；
- admin role 不错误 mount 数据面，proxy role 不错误 mount create/delete；
- `SandboxProxyService.execute()` 的 rocklet/OpenSandbox backend 路由均有覆盖；
- timeout 刷新、metrics decorator、access log 按部署决策记录原始 headers 且不记录 Connect command body；
- 多请求并发下 synthetic pid 不冲突、task registry 有上限且完成后清理。

## 12. 推荐拆分

1. **PR 1：协议 codec + raw tests**
   `proto/request.py` 放 Start 请求模型，`proto/response.py` 放事件/错误响应模型，`proto/exceptions.py` 放 Connect 专用错误；`proto/e2b_connect.py` 只保留 envelope 编解码和请求到 ROCK command 的转换。参数错误复用 `BadRequestRockError`。
2. **PR 2：proxy `Start` + health**
   `e2b_envd_api.py`、proxy service execute seam、buffered output、timeout/keepalive/task registry。
3. **后续生产加固：sandbox-scoped envd token**
   用短期、可撤销 token 替换本期复用的原始 API key；补摘要持久化、proxy校验、rotation/owner/delete fencing。
4. **PR 4：SDK E2E**
   Python 2.34/2.37/current + JS current 的真实 TCP suite；更新 E2B 使用文档和能力矩阵。
5. **后续：完整 background process API**
   SendSignal/Connect/List、durable PID mapping、exit retention；stdin/PTY 另立需求。

这个拆分可以先交付用户要求的 `sandbox.commands.run()` 最小兼容，同时不把未承诺的 envd 全量能力绑进首个 PR。
