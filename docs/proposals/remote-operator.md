# Remote Operator 设计方案

## 1. 背景

ROCK Admin 通过 `OperatorFactory` 按 `runtime.operator_type` 创建对应的 Operator 实例，现有支持 `ray`、`k8s`、`opensandbox` 三种后端。这些 Operator 均与特定基础设施强绑定（Ray 集群、K8s CRD、OpenSandbox SDK），无法通用地接入任意远端 sandbox 平台。

本方案新增 **Remote Operator**，以 HTTP REST 形式接入远端 sandbox 平台，并通过 **Provider 抽象** 支持多平台适配。设计模式参照 K8s Operator 的 `K8sProvider` Protocol + `BatchSandboxProvider` 实现。

### 现有架构

```
SandboxManager
  └── AbstractOperator (submit / restart / get_status / stop / delete)
        ├── RayOperator        → Ray Actor
        ├── K8sOperator         → K8sProvider Protocol → BatchSandboxProvider (K8s CRD)
        └── OpenSandboxOperator → OpenSandboxClient (SDK)

ProxyService
  ├── SandboxProxyService        → Rocklet RPC (Ray / K8s)
  └── OpenSandboxProxyService    → OpenSandboxBackend (SDK)
```

K8s Operator 的关键设计：`K8sProvider` 是一个 `Protocol`，定义 `submit`/`get_status`/`stop` 三个核心方法；`K8sOperator` 作为薄封装层，将生命周期调用委托给 provider，自身只负责 Redis 信息合并。Remote Operator 复用这一模式。

## 2. 目标与非目标

**目标：**

- 新增 `remote` operator 类型，通过 `runtime.operator_type: "remote"` 启用
- 定义 `RemoteProvider` Protocol，支持不同远端平台适配
- 实现首个 provider：`SandboxNextProvider`（SandboxManager Control HTTP API）
- 不依赖 Ray，启动时跳过 Ray 初始化
- 命令/文件执行复用现有 `SandboxProxyService`（Rocklet RPC），远端平台运行 Rocklet

**非目标（Phase 1）：**

- 不实现 archive / restore
- 不实现 restart（远端平台语义各异）
- 不新增独立 ProxyService

## 3. 架构设计

### 3.1 整体架构

```
SandboxManager
  └── AbstractOperator
        └── RemoteOperator                    # 薄封装，委托给 provider
              └── RemoteProvider (Protocol)   # provider 抽象接口
                    └── SandboxNextProvider     # 首个实现：HTTP REST
                    └── (future providers)     # E2B, Modal, 自定义平台 ...

ProxyService (复用现有)
  └── SandboxProxyService → Rocklet RPC (与 Ray / K8s 一致)
```

### 3.2 RemoteProvider Protocol

定义文件：`rock/sandbox/operator/remote/provider.py`

**生命周期方法（必须实现）：**

| 方法 | 签名 | 说明 |
|------|------|------|
| `submit` | `(config: DockerDeploymentConfig, user_info: dict) → SandboxInfo` | 创建沙箱，返回含 `sandbox_id`、`state`、`extended_params`（含平台 ID）的 SandboxInfo |
| `get_status` | `(remote_sandbox_id: str) → SandboxInfo \| None` | 查询实时状态，映射为 Rock State；404 返回 None |
| `stop` | `(remote_sandbox_id: str) → bool` | 停止沙箱（暂停或终止，语义由 provider 定义） |
| `delete` | `(remote_sandbox_id: str) → bool` | 永久删除沙箱，已不存在返回 True |

`remote_id` 由 RemoteOperator 从 Redis 缓存的 `host_name` 字段中解析后传入，provider 不直接依赖 Redis。

**Template API（可选）：**

| 方法 | 签名 | 说明 |
|------|------|------|
| `create_template` | `(spec: Any) → dict` | 创建模板，返回含 `template_id` 和 `status` 的 dict |
| `get_template_status` | `(template_id: str) → dict \| None` | 查询模板状态，不存在返回 None |
| `delete_template` | `(template_id: str) → bool` | 删除模板，不存在返回 True |

Template 方法默认 raise `NotImplementedError`，RemoteOperator 捕获后转为 `BadRequestRockError`。`scale_template` 不在 Protocol 中（SandboxNext 无 scale 端点），保持 AbstractOperator 默认行为。

### 3.3 RemoteOperator

定义文件：`rock/sandbox/operator/remote/operator.py`

继承 `AbstractOperator`，`supports_running_delete = True`。通过 `_create_provider()` 工厂方法根据 `RemoteOperatorConfig.provider` 选择 provider 实现（当前仅 `"sandbox_next"`）。

| 方法 | 行为 |
|------|------|
| `submit` | 直接委托 `provider.submit()` |
| `get_status` | ① Redis 获取用户元数据 → ② 解析 `remote_sandbox_id`（`host_name`） → ③ 委托 `provider.get_status()` → ④ 返回 `{**provider_info, "sandbox_id": redis_info["sandbox_id"]}` |
| `stop` | 从 Redis 解析 `remote_sandbox_id` → 委托 `provider.stop()`（当前与 `delete` 同语义） |
| `delete` | 从 Redis 解析 `remote_sandbox_id` → 委托 `provider.delete()` |
| `restart` | 不支持，raise `BadRequestRockError` |
| `create_template` | 委托 provider，`NotImplementedError` → `BadRequestRockError` |
| `get_template_status` | 同上 |
| `delete_template` | 同上 |
| `scale_template` | 不委托，保持 AbstractOperator 默认 `BadRequestRockError` |

### 3.4 SandboxNextProvider

定义文件：`rock/sandbox/operator/remote/providers/sandbox_next_provider.py`

使用 `httpx.AsyncClient` 与 SandboxManager Control HTTP API 通信。认证通过 `X-Api-Key` 头（必填，`api_key`）——网关侧由 API key 解析出租户 profile（`X-Sandbox-Profile-ID` 是网关到 SandboxManager 的内部 header，客户端无需也不应携带）；沙箱形态通过 `X-Sandbox-Class` 头携带，在 `submit` 时由 `_derive_sandbox_class()` 从 `DockerDeploymentConfig` 推导（当前固定返回 `gui`，后续按 num_gpus/image_os 细化 gpu/gui/headless）。profile_id 和资源 ID 不得出现在请求体中。

#### API 端点摘要

| Method | Path | 说明 |
|--------|------|------|
| `POST` | `/v1/sandboxes` | 创建沙箱，返回 `Sandbox`（201，幂等：同 request_id 返回同一对象） |
| `GET` | `/v1/sandboxes/{id}` | 查询详情（200），404 表示不存在 |
| `DELETE` | `/v1/sandboxes/{id}` | 删除沙箱（202 异步受理，返回 deleting 状态的 Sandbox） |
| `POST` | `/v1/sandboxes/{id}/pause` | 暂停（202） |
| `POST` | `/v1/sandboxes/{id}/resume` | 恢复（200） |
| `POST` | `/v1/sandboxes/{id}/renew` | 续租（200） |
| `POST` | `/v1/templates` | 创建模板（202） |
| `GET` | `/v1/templates/{id}` | 查询模板（200），404 表示不存在 |
| `DELETE` | `/v1/templates/{id}` | 删除模板（202 / 204） |

#### Raw 模式（K8s 对象透传）

当 Control API 的字段（`resource_spec` 等）不足以表达需求时，`submit` 可走 raw 模式：把完整的 BatchSandbox CRD manifest 以 JSON 字符串形式放进 `raw` 字段透传给平台。

- **触发条件**：E2B 链路中 template 表没有找到 READY 模板时，`E2BService` 置空 `template_id` 并在 `extended_params` 中设置 `use_raw=true`（此时请求的 image 被当作镜像名）；provider 内 `template_id` 优先于 `use_raw`，原生 SDK 链路（不携带该信号）维持 resource_spec 路径不变
- **manifest 构造**：复用 K8s 链路的 `K8sTemplateLoader.build_manifest`（Jinja2 渲染 + drop-empty 规则），模板来自 `RemoteOperatorConfig.templates`（结构同 `K8sConfig.templates`，固定使用 `default`）；`memory` 经 `normalize_memory_to_k8s` 规范化后渲染
- **请求体**：`{"request_id": "<Rock sandbox_id>", "raw": "<manifest JSON 字符串>"}`；Rock sandbox_id 由 `build_manifest` 写入 `metadata.name` 与 `rock.sandbox/sandbox-id` label（CRD 级 + Pod 级）
- **port_mapping**：从模板的 `ports`（proxy/server/ssh）动态提取，不再写死；模板路径/resource_spec 路径仍使用固定映射（8000/8080/22）
- **数据面**：raw manifest 的容器需自行启动 rocklet（如 command 中下载并启动），否则仅控制面可用

#### 生命周期方法映射

| Provider 方法 | SandboxManager API | 关键映射 |
|---------------|--------------------|----------|
| `submit` | `POST /v1/sandboxes` | `request_id` = Rock `sandbox_id`（幂等键），`resource_spec`（`vcpu_count`/`memory_mb`/`disk_size_mb`）从 `DockerDeploymentConfig` 转换；响应中 `sandbox_id` → `host_name`（remote sandbox id），`endpoint` → `host_ip` + `extended_params.endpoint` |
| `get_status` | `GET /v1/sandboxes/{id}` | 404 → 返回 None；否则映射状态与 `endpoint` |
| `stop` | `DELETE /v1/sandboxes/{id}` | 与 `delete` 同语义 |
| `delete` | `DELETE /v1/sandboxes/{id}` | 404 → 返回 True |

#### Template 方法映射

当前 `SandboxNextProvider` 暂未实现 Template API，`create_template` / `get_template_status` / `delete_template` 均直接抛出 `NotImplementedError`，由 `RemoteOperator` 转换为 `BadRequestRockError`。

#### 状态映射

SandboxManager 返回 protobuf 风格的大写枚举（如 `SANDBOX_RUNNING`）：

| SandboxManager 状态 | Rock State | 说明 |
|---------------------|-----------|------|
| `SANDBOX_STATE_UNSPECIFIED` | `PENDING` | 未指定 |
| `SANDBOX_CREATING` | `PENDING` | 创建中 |
| `SANDBOX_ALLOCATED` | `PENDING` | 已分配，尚未就绪 |
| `SANDBOX_RUNNING` | `RUNNING` | 运行中 |
| `SANDBOX_PAUSING` | `STOPPED` | 暂停中（过渡态） |
| `SANDBOX_PAUSED` | `STOPPED` | 已暂停 |
| `SANDBOX_PAUSE_FAILED` | `STOPPED` | 暂停失败 |
| `SANDBOX_RESUMING` | `PENDING` | 恢复中（过渡态） |
| `SANDBOX_RESUME_FAILED` | `STOPPED` | 恢复失败 |
| `SANDBOX_DELETING` | `STOPPED` | 删除中（过渡态） |
| `SANDBOX_DELETED` | `DELETED` | 已删除 |
| `SANDBOX_FAILED` | `STOPPED` | 异常，不可用但未删除 |
| `SANDBOX_UNKNOWN` | `PENDING` | 控制面无法确认节点事实 |
| `SANDBOX_MIGRATING` | `PENDING` | 迁移中 |
| GET 404 | — | Provider 返回 None |
| 其他未知 | `PENDING` | 保守降级 |

可通过 `provider_options.state_mapping` 覆盖默认映射表。

#### 数据面连通

Provider 在 `submit()` 返回的 `SandboxInfo` 中填充：

| SandboxInfo 字段 | 来源 | 说明 |
|-------------------|------|------|
| `host_ip` | 响应 `endpoint` | 数据面地址（如 pod IP），原始字符串直接使用，不解析 |
| `port_mapping` | 写死 | `{Port.PROXY: 8000, Port.SERVER: 8080, Port.SSH: 22}`，与 K8s 一致 |
| `host_name` | 响应 `sandbox_id` | 平台分配的沙箱 ID（remote sandbox id） |
| `extended_params[endpoint]` | 响应 `endpoint` | 原始值，供后续使用 |
| `extended_params[backend]` | 固定 `"sandbox_next"` | 后端标识 |

注：Control HTTP API 响应不含访问凭证字段，`auth_token` 不再填充。

`SandboxProxyService` 通过 `host_ip` + `port_mapping` 构造 Rocklet RPC 连接，与 Ray / K8s 完全一致，无需改造。

#### 重试策略

对 5xx 错误进行指数退避重试（默认最多 3 次，退避基数 0.5s），4xx 错误直接返回。通过 `provider_options.retry_max` 和 `provider_options.retry_backoff_base` 可配置。

## 4. 配置设计

### 4.1 RemoteOperatorConfig

新增到 `rock/config.py`，当 `runtime.operator_type == "remote"` 时生效：

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `provider` | `str` | `"sandbox_next"` | provider 类型 |
| `base_url` | `str` | (必填) | SandboxManager API 基础 URL，例如 `http://sandbox-manager:8081` |
| `api_key` | `str \| None` | `None` | `X-Api-Key` 头认证（必填，网关由它解析租户 profile） |
| `access_token` | `str \| None` | `None` | Bearer token 认证（可选） |
| `default_timeout` | `int` | `600` | HTTP 请求超时（秒） |
| `provider_options` | `dict` | `{}` | provider 特有的额外配置，例如 `state_mapping` |
| `templates` | `dict[str, dict]` | `{}` | raw 模式渲染模板，结构同 `K8sConfig.templates`（含 `ports` 与 Pod 模板） |

`base_url` 为空时抛 `ValueError`。

### 4.2 RockConfig 集成

`RockConfig` 新增 `remote: RemoteOperatorConfig | None` 字段，`from_env()` 从 YAML `remote` 段解析。

### 4.3 YAML 配置示例

```yaml
runtime:
  operator_type: "remote"

remote:
  provider: "sandbox_next"
  base_url: "http://sandbox-manager:8081"
  api_key: "your-x-api-key"
  default_timeout: 600
  templates:                     # raw 模式渲染模板（结构同 K8sConfig.templates）
    default:
      ports: {proxy: 8000, server: 8080, ssh: 22}
      template:
        metadata:
          labels: {app: rock-sandbox}
        spec:
          tolerations:
            - operator: "Exists"
          containers:
            - name: sandbox
              image: "{{ image }}"
              command: ["/bin/sh", "-c", "pip install rl-rock[rocklet] && rocklet --port 8000"]
              resources:
                requests: {cpu: "{{ cpus }}", memory: "{{ memory }}"}
```

## 5. 工厂集成

- **OperatorContext**：新增 `remote_config: RemoteOperatorConfig | None = None` 字段
- **OperatorFactory**：`create_operator()` 新增 `"remote"` 分支，校验 `remote_config` 后创建 `RemoteOperator`，注入 `redis_provider` 和 `nacos_provider`
- **辅助函数**：`operator_requires_ray("remote")` → `False`；`operator_supports_scheduler("remote")` → `False`（远端平台自行调度）
- **Admin 启动**：`rock/admin/main.py` 构造 `OperatorContext` 时传入 `remote_config=rock_config.remote`

## 6. 文件结构

```
rock/sandbox/operator/remote/
├── __init__.py
├── operator.py                    # RemoteOperator
├── provider.py                    # RemoteProvider Protocol
├── constants.py                   # EXT_ENDPOINT, BACKEND_NAME 等常量
└── providers/
    ├── __init__.py
    └── sandbox_next_provider.py   # SandboxNextProvider

tests/unit/sandbox/operator/remote/
├── __init__.py
├── test_operator.py               # RemoteOperator 单元测试
└── test_sandbox_next_provider.py  # SandboxNextProvider 单元测试 (mock httpx)
```

## 7. 测试策略

| 测试文件 | 覆盖范围 |
|---------|---------|
| `test_operator.py` | submit/get_status/stop/delete/restart、Redis 合并逻辑、provider 委托验证、`NotImplementedError` → `BadRequestRockError` 转换 |
| `test_sandbox_next_provider.py` | HTTP 调用（`httpx.MockTransport`）、状态映射、错误处理、认证头、Template API 暂不实现（`NotImplementedError`） |

## 8. 设计决策

| 决策 | 结论 | 理由 |
|------|------|------|
| 多 Provider 支持 | 当前绑定 `SandboxNextProvider`，保留 Protocol + 工厂方法扩展点 | 暂无多平台需求，但抽象层不删 |
| 探活机制 | 与 K8s/Ray 一致，复用现有逻辑 | 统一运维 |
| 重试策略 | 5xx 指数退避（最多 3 次），4xx 直接返回 | 有限重试，避免无限等待 |
| 数据面连通 | `endpoint` 直接作为 `host_ip`，`port_mapping` 写死 | 复用现有 proxy 链路，与 K8s 一致（endpoint 为 pod IP，端口固定） |
| stop 语义 | `stop` 与 `delete` 同语义 | Rock 不使用 pause/resume 语义，简化实现 |
| 租约管理 | 不设 `timeout_seconds`，使用平台默认值；不实现 renew | Rock 自身的 `auto_archive_seconds` / `auto_delete_seconds` 控制生命周期 |
| 认证 / 租户路由 | 客户端携带 `X-Api-Key`（必填）；`X-Sandbox-Class` 在 submit 时从 `DockerDeploymentConfig` 推导；profile 由网关从 API key 解析后内部下发，不进请求体也不由客户端携带 | 网关侧维护 API key → profile 映射；`X-Sandbox-Profile-ID` 为网关内部 header |
| Raw 模式触发 | E2B 链路 DB 未命中模板时由 `E2BService` 在 `extended_params` 设置 `use_raw=true`（provider 特有信号不进一级字段），provider 内 `template_id` 优先；复用 `K8sTemplateLoader` 渲染 manifest 后以 `raw` 字符串透传 | 控制面字段不够时的高灵活性逃生舱；对齐 K8s 链路 `extended_params` 传 provider 行为的先例（`pool_name`/`template_name`）；CRD 与平台同源（sandbox.opensandbox.io/v1alpha1）无需转换 |

## 9. 与现有 Operator 对比

| 维度 | RayOperator | K8sOperator | OpenSandboxOperator | RemoteOperator |
|------|------------|------------|--------------------|---------------|
| 后端 | Ray Actor | K8s CRD | OpenSandbox SDK | HTTP REST API |
| Provider 抽象 | 无 | K8sProvider Protocol | 无 | RemoteProvider Protocol |
| 需要 Ray | 是 | 否 | 否 | 否 |
| 支持 Scheduler | 是 | 是 | 否 | 否 |
| supports_running_delete | False | False | True | True |
| restart | 支持 | 不支持 | 不支持 | 不支持 |
| Template API | 不支持 | 支持 (Pool CRD) | 不支持 | 不支持（暂不实现） |
| Proxy 层 | Rocklet RPC | Rocklet RPC | OpenSandboxBackend | Rocklet RPC (复用) |

## 10. 后续扩展

当前 `RemoteProvider` Protocol 和 `_create_provider()` 工厂方法已作为扩展点保留。后续如需接入其他平台：

1. 新增 Provider 实现 `RemoteProvider` Protocol
2. 在 `_create_provider()` 中新增分支
3. 可有独立的配置子结构
