# ROCK Template API 设计文档

## 1. 概述

Template API 提供 Sandbox 预热池的创建与查询能力。调用方通过提交镜像和资源规格创建 Template，获得稳定的 `templateID`。该 ID 对应一个 OpenSandbox Pool CRD，由 Pool Controller 自动维护热备 Pod。后续创建 Sandbox 时，`templateID` 作为 `poolRef` 被引用，实现秒级分配。

| 接口 | 说明 |
| --- | --- |
| `POST /apis/envs/sandbox/v1/templates` | 提交 Template 创建请求，同步返回 `templateID` 及状态 |
| `GET /apis/envs/sandbox/v1/templates/{templateID}` | 查询 Template 当前状态 |
| `POST /apis/envs/sandbox/v1/templates/{templateID}/scale` | 调整 Template 容量（PATCH 语义） |
| `DELETE /apis/envs/sandbox/v1/templates/{templateID}` | 删除 Template（= 删除 Pool CRD） |

### 设计要点

- **无独立存储**：K8s Pool CRD 即为持久化层，Informer 本地缓存提供高效读取，无需 DB。
- **同步创建**：Pool CRD 创建不耗时，请求内同步完成，无需后台异步任务。
- **天然去重**：相同 spec 生成相同 `templateID`（= Pool Name），K8s API Server 通过 409 Conflict 自动去重。容量字段不参与 `templateID` 计算。
- **模版渲染**：Pool 的 PodTemplateSpec、capacitySpec 等通过 YAML 模版配置 + Jinja2 渲染。

### 概念说明

外部 API 遵循 E2B 风格，使用 "Template" 概念。Template 是统一抽象——描述 Sandbox Pod 如何被创建的配置。差异只在交付机制：

| | Warm（当前实现） | Cold（未来可选，不实现） |
| --- | --- | --- |
| 底层资源 | Pool CRD | BatchSandbox template config |
| Pod 何时创建 | 预创建热备，引用时秒级分配 | Sandbox 请求时即时创建 |
| templateID 映射 | `tpl-xxx` → Pool Name | template name → config key |
| 状态查询 | 需查 Pool reconcile 进度 | 天然 `ready`（无需预热） |

Operator 内部将 Template 概念转换为 Pool 操作：

| 外部概念 | 内部映射 | K8s 资源 |
| --- | --- | --- |
| Template | Pool | Pool CRD |
| templateID | Pool Name | Pool.metadata.name |
| Template status | Pool status 映射 | Pool.status |

## 2. 整体设计

### 2.1 链路

```
创建 Template
    ├─ 生成 templateID = tpl- + sha256(非空 spec 字段)[:16]
    ├─ 从 pool_template 配置渲染 Pool CRD manifest
    ├─ K8sApiClient(Pool).create_custom_object()
    │    └─ 409 → Pool 已存在，返回已有 templateID + status
    └─ 返回 templateID + status

查询 Template
    ├─ templateID = Pool Name
    ├─ K8sApiClient(Pool).get_custom_object()  ← Informer 本地缓存
    ├─ 映射 Pool status → template status
    └─ 返回 templateID + status + timestamps

删除 Template
    ├─ templateID = Pool Name
    ├─ K8sApiClient(Pool).delete_custom_object()
    │    └─ 404 → not found = already deleted
    └─ K8s GC 自动清理 Pool 拥有的 Pod

扩缩容 Template
    ├─ templateID = Pool Name
    ├─ 校验容量字段（pool_min/pool_max/buffer_min/buffer_max）
    ├─ 仅将提供的非空字段 PATCH 到 Pool.spec.capacitySpec
    ├─ K8sApiClient(Pool).update_custom_object()
    └─ 返回更新后的 templateID + status + capacity
```

### 2.2 templateID 生成

覆盖完整 spec 的**非空字段**，忽略 null/空值，保证前向兼容——后续新增字段传入非空值时自动参与哈希，传入 null 时不影响已有映射。

- 格式：`tpl-{sha256(排序后的非空字段)[:16]}`
- 前缀用连字符 `-`（符合 K8s RFC 1123 命名规范）
- 参与哈希的非空字段：`from_image`、`cpu_count`、`memory_mb`；`disk_gb`/`num_gpus`/`accelerator_type` 为 null 时不参与哈希
- 容量字段（`pool_min`/`pool_max`/`buffer_min`/`buffer_max`）被排除在外，因为容量不影响 Template 身份

### 2.3 状态映射

Pool CRD status 字段为 `available`/`total`（非 `readyReplicas`/`replicas`）：

| 条件 | Template status | 说明 |
| --- | --- | --- |
| Pool 不存在 | 404 | templateID 无效 |
| `available > 0` | `ready` | 至少一个热备 Pod 就绪 |
| `available == 0 && total > 0` | `building` | Pod 已创建但未就绪 |
| `available == 0 && total == 0` | `building` | Controller 尚未处理或 Pool 为空 |
| conditions 中 `Ready=False` | `error` | Pool 创建失败 |

返回的 Template status 中还包含容量信息，结构如下：

```json
{
  "capacity": {
    "spec": {
      "poolMin": ...,
      "poolMax": ...,
      "bufferMin": ...,
      "bufferMax": ...
    },
    "status": {
      "available": ...,
      "total": ...,
      "allocated": ...
    }
  }
}
```

### 2.4 Pool 标识

Template API 创建的 Pool 携带 Label `rock.sandbox/managed-by: template-api`，与 Nacos 配置的系统 Pool 区分。

### 2.5 关于 Update

不支持修改 Template 的 spec 字段（镜像、CPU、内存等）。templateID 由这些 spec 字段的 hash 生成，改 spec 即产生新 templateID，本质是 create 而非 update。配置变更场景：创建新 Template → 更新引用方配置 → 删除旧 Template。

容量字段（`pool_min`/`pool_max`/`buffer_min`/`buffer_max`）可通过 `POST /templates/{templateID}/scale` 单独调整，采用 PATCH 语义：仅更新请求中提供的非空字段，允许缩容到 0，并在 `TemplateScaleRequest` 中校验 `min <= max`。

### 2.6 冒烟测试

`tests/smoke/` 通过命令行参数指定 admin 地址和镜像，不硬编码内部镜像：

```bash
pytest tests/smoke/ --admin-url http://localhost:8080 --smoke-image python:3.11
```

| Case | 覆盖链路 | 关键断言 |
| --- | --- | --- |
| `test_template_lifecycle` | create → wait ready → delete | templateID 以 `tpl-` 开头、status 变为 `ready`、删除成功 |
| `test_template_idempotent_create` | 同 spec POST 两次 | 返回相同 templateID |

两个 case 均通过 `try/finally` 保证失败时也清理模板。

## 3. 实现要点

### 3.1 Pool 模版配置

`K8sConfig.pool_template` 字段（YAML 配置 + Jinja2 渲染）生成 Pool CRD spec。关键约定：

- capacitySpec 使用 **camelCase** 键（`bufferMin`/`bufferMax`/`poolMin`/`poolMax`），匹配 Pool CRD spec
- Jinja2 变量需加**双引号**（`"{{ from_image }}"`），避免 YAML flow mapping 解析错误
- 渲染后 capacitySpec 值为字符串，需 `int()` 转换才能被 K8s 接受
- 渲染上下文：`from_image`、`cpu_count`、`memory_mb`（容量字段不再来自 `TemplateSpec`，由 `pool_template` 默认配置提供）

### 3.2 关键常量

- `TEMPLATE_ID_PREFIX = "tpl-"`（RFC 1123 连字符）
- `LABEL_MANAGED_BY = "rock.sandbox/managed-by"`，值为 `"template-api"`
- Pool CRD：plural=`pools`，kind=`Pool`

### 3.3 BatchSandboxProvider 扩展

Provider 新增 Pool informer（复用同一 `ApiClient`，独立 watch `pools` CRD），并提供三组方法：

| 外部方法（template 术语） | 内部方法（pool 术语） | 行为 |
| --- | --- | --- |
| `create_template(spec)` | `_create_pool(name, spec)` | 渲染 manifest → create_custom_object；409 时返回已有 Pool |
| `get_template_status(id)` | `_get_pool(name)` | 从 Informer 缓存读取 Pool → 映射 status；未找到返回 None |
| `delete_template(id)` | `_delete_pool(name)` | delete_custom_object；404 视为已删除 |

辅助方法：`_build_pool_manifest_from_template`（渲染）、`_map_pool_to_template_status`（状态映射）。

## 4. 涉及文件

| 文件 | 变更 |
| --- | --- |
| `rock/sandbox/operator/k8s/constants.py` | 新增 Pool CRD 常量、Label 常量、`TEMPLATE_ID_PREFIX` |
| `rock/sandbox/operator/k8s/provider.py` | 新增 Pool informer、template/pool 转换方法、scale_template |
| `rock/sandbox/sandbox_manager.py` | 新增 template 代理方法（含 scale_template） |
| `rock/sandbox/operator/abstract.py` | 新增 `scale_template` 抽象方法默认实现 |
| `rock/admin/entrypoints/template_api.py` | 新增 `template_router`（POST/GET/DELETE/SCALE 端点） |
| `rock/admin/main.py` | 注册 `template_router`，prefix `/apis/envs/sandbox/v1` |
| `rock/admin/proto/request.py` | 新增 `TemplateCreateRequest` / `TemplateScaleRequest` |
| `rock/admin/proto/response.py` | 新增 `TemplateCreateResponse` / `TemplateStatusResponse` / `TemplateCapacityResponse` |
| `rock-conf/rock-junxin.yml` | 新增 `pool_template` 配置段 |
| `tests/unit/test_template_api.py` | 单元测试：ID 生成、渲染、状态映射、常量 |
| `tests/smoke/conftest.py` | 冒烟测试配置：`--admin-url` / `--smoke-image` |
| `tests/smoke/test_template.py` | 冒烟测试：lifecycle + 幂等创建 |

