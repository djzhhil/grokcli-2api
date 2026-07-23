# 多账号/多上游额度池的单请求可靠性优化调研

> 调研日期：2026-07-23  
> 范围：`grokcli-2api → New API → Zoo Code / Kilo Code / Roo Code`，以及可替代或补充的网关、工作流组件。  
> 资料原则：仅引用官方文档、官方源码与协议规范；本文不涉及业务代码修改。

## 结论摘要

1. **一个流式响应只要已有正文或工具参数发给客户端，就不能再由网关透明换号重跑。** OpenAI 和 Claude 的流式接口都通过 SSE 增量发送内容；Claude 的工具参数甚至是“部分 JSON 字符串”，到内容块结束后才完整。HTTP 规范也不允许中间代理盲目自动重试非幂等请求。除非协议另外提供可恢复的事件 ID、游标和确定性重放语义，否则换号重跑只能产生一条新的生成，可能重复、分叉或破坏 JSON，而不是原请求的续传。[OpenAI Streaming](https://developers.openai.com/api/docs/guides/streaming-responses)、[Claude Streaming Messages](https://platform.claude.com/docs/en/build-with-claude/streaming)、[RFC 9110 §9.2.2](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2)、[WHATWG SSE `Last-Event-ID`](https://html.spec.whatwg.org/multipage/server-sent-events.html#the-last-event-id-header)
2. **LiteLLM、New API、Envoy 能改善“选哪个上游”和“何时重试”，不能恢复已经执行到一半的 Agent 任务。** 它们的预算主要是网关自身记录的消费预算或 TPM/RPM，不等价于对上游账号隐藏剩余额度进行强一致预占；Envoy 的 `per_try_timeout` 也明确只在响应尚未向下游发送时适用于重试。[LiteLLM Router](https://docs.litellm.ai/docs/routing)、[LiteLLM Budget Routing](https://docs.litellm.ai/docs/proxy/provider_budget_routing)、[Envoy timeouts](https://www.envoyproxy.io/docs/envoy/latest/faq/configuration/timeouts)、[New API 渠道管理](https://docs.newapi.pro/zh/docs/guide/feature-guide/admin/channel)
3. **真正跨账号恢复工具任务，需要任务级检查点和工具幂等，而不是继续堆 HTTP 重试。** Temporal 通过事件历史重放 Workflow，并要求可能重复执行的 Activity 具备幂等性；LangGraph 在每个 graph step 保存 checkpoint，但恢复时仍可能重新执行后续节点，因此工具节点同样必须幂等。[Temporal Workflow Execution](https://docs.temporal.io/workflow-execution)、[Temporal Activity Definition](https://docs.temporal.io/activity-definition)、[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[LangGraph Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)
4. **当前最合适的路线不是立即替换 New API，而是分层治理：** `grokcli-2api` 负责最靠近账号池的额度预估、原子预占和响应提交前换号；New API 负责统一协议、跨渠道策略与计费；Zoo/Kilo/Roo 保持兼容模式；另建“可恢复任务入口”承载检查点和有副作用的工具执行。普通 OpenAI/Claude 兼容接口只能提供请求级可靠性，无法凭空获得任务级可靠性。

## 1. 当前链路及其真实故障边界

当前链路可抽象为：

```text
Zoo / Kilo / Roo（Agent 循环和本地工具执行）
          │ OpenAI Chat/Responses 或 Claude Messages
          ▼
New API（鉴权、格式转换、渠道路由、用量统计）
          ▼
grokcli-2api（Grok 协议适配、账号挑选、冷却、换号）
          ▼
Grok 多账号池
```

仓库现状已经正确建立了一条重要红线：[`OpenWithFailover`](../../internal/proxy/failover.go) 只在响应未提交时尝试其他账号，提交后返回 `ErrCommitted`；[`chat.go`](../../internal/proxy/chat.go) 还会先探测首个有效事件，并为“空流”保留短暂失败转移窗口。README 也明确写明 `stream_started` 只在真正向下游发送帧后锁定账号，见 [`README.md`](../../README.md)。这类实现能处理“账号刚选中就失败”“连接成功但没有有效帧”等情况，但无法处理账号生成到一半后额度耗尽。

另一方面，[`picker.go`](../../internal/pool/picker.go) 的候选状态主要是启用、额度禁用、冷却、模型屏蔽、请求次数和权重；当前没有“已知剩余额度减并发预占额度”的准入判断。因此多个并发大请求仍可能同时选中一个看起来可用、实际上已接近耗尽的账号。

### 1.1 为什么已输出后不能透明重试

流式响应包含三个不可逆事实：

- **字节已被观察。** 客户端已经把文本 delta 展示给用户，或者把工具调用 delta 累积进状态。新上游无法撤回这些字节。
- **新生成不是旧生成的续传。** 另一个账号发起的是新模型调用，没有原调用的隐藏生成状态；即使把已输出前缀重新放进提示词，也只能做应用层续写，无法保证逐字连续、无重复或相同的工具选择。
- **工具参数在结束事件前可能不完整。** OpenAI 函数调用和 Claude `tool_use` 都允许参数分块发送；Claude 官方文档明确说明 `input_json_delta` 是部分 JSON，需累积至 `content_block_stop` 后再解析。[OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)、[Claude Streaming Messages](https://platform.claude.com/docs/en/build-with-claude/streaming)

SSE 标准确实定义了 `id` / `Last-Event-ID` 的重连基础设施，但只有服务端生成事件 ID 并实现按 ID 恢复时才成立；OpenAI 兼容 Chat Completions 并没有规定跨请求、跨账号重放同一生成的契约。[WHATWG SSE](https://html.spec.whatwg.org/multipage/server-sent-events.html#the-last-event-id-header)

因此安全边界应是：

```text
尚未向客户端提交语义内容 ── 可以换号并完整重试
已经提交任意正文/工具参数 ── 禁止透明重试；终止本次 turn，由任务层恢复
```

“语义内容”不包括可以丢弃的握手、心跳或空事件，但包括任何正文 delta、reasoning delta、工具名称、工具参数和结束状态。完整缓冲整个上游响应后再一次性下发，可以扩大安全重试窗口，但会牺牲首字延迟、实时体验和内存；它适合离线任务，不适合 Zoo/Kilo/Roo 的交互式默认模式。

## 2. 成熟网关能做什么，不能做什么

### 2.1 LiteLLM Proxy

LiteLLM Router 提供部署负载均衡、冷却、fallback、超时和重试，也有 usage-based、latency-based、least-busy 等策略；Proxy 还能按 key、user、team 设置预算，或按 provider/model/tag 的已记录 spend 做预算路由。[Router](https://docs.litellm.ai/docs/routing)、[Fallbacks](https://docs.litellm.ai/docs/proxy/reliability)、[Budgets and Rate Limits](https://docs.litellm.ai/docs/proxy/users)、[Budget Routing](https://docs.litellm.ai/docs/proxy/provider_budget_routing)

能力边界：

- TPM/RPM 和 Proxy 预算是 LiteLLM 自己记录、配置或在 Redis 中聚合的限制，不是上游账号余额的权威实时值。
- 官方预算路由示例体现的是“根据已累计 spend 跳过部署”；如果一次调用本身足以越过余额，它并未承诺先原子预留这次调用的最大成本。
- fallback 仍然是请求级重试，不能恢复已经发给客户端的半截流，也不保存客户端本地工具执行状态。

所以 LiteLLM 可以替代或补充 New API 的网关能力，但**不能单独解决本问题**。如果只是为了中途耗尽而替换 New API，迁移收益不足。

### 2.2 Envoy 与通用成熟网关

Envoy 提供重试条件、单次尝试超时、熔断、retry budget、host predicate 和 hedging。其 retry budget 用于限制重试流量相对正常请求的比例，防止重试风暴；这不是模型额度预算。[Envoy Retry Budget](https://www.envoyproxy.io/docs/envoy/latest/api-v3/config/cluster/v3/circuit_breaker.proto.html)、[Envoy HTTP routing](https://www.envoyproxy.io/docs/envoy/latest/intro/arch_overview/http/http_routing.html)

更关键的是，Envoy 官方文档说明 `per_try_timeout` 必须在响应任何部分发送到下游之前触发，才适合用于重试流式端点；响应开始后发生上游 reset，会以 `upstream_reset_after_response_started` 之类的终止原因暴露，而不是生成一条无缝替代流。[Envoy timeouts](https://www.envoyproxy.io/docs/envoy/latest/faq/configuration/timeouts)、[Envoy response code details](https://www.envoyproxy.io/docs/envoy/latest/configuration/http/http_conn_man/response_code_details)

Envoy 适合承担连接级健康、熔断、并发上限和“首字节前”重试，不理解 token、工具调用、账号剩余额度或任务检查点。

### 2.3 New API

New API 官方文档和源码说明其能力包括渠道优先级、同优先级权重、多 Key 轮询、失败 Key 跳过、自动禁用和失败重试，并支持 OpenAI/Claude 等协议及部分格式转换。[New API 渠道管理](https://docs.newapi.pro/zh/docs/guide/feature-guide/admin/channel)、[New API 官方仓库](https://github.com/QuantumNous/new-api)

在本链路中，New API 最适合继续负责：

- 统一入口、鉴权、租户额度与成本核算；
- OpenAI / Claude 的协议适配和模型映射；
- 跨渠道或跨供应商的请求前路由；
- 对明确发生在响应提交前的失败做有限重试。

不应让 New API 和 `grokcli-2api` 同时进行多轮无上限重试，否则会形成重试乘法。例如客户端 2 次 × New API 3 次 × `grokcli-2api` 3 个账号，最坏可能放大为 18 次上游尝试。建议为每类失败指定唯一重试责任方：账号内换号由 `grokcli-2api` 负责，跨供应商 fallback 由 New API 负责，客户端只对“确认未提交”或幂等查询做极少量重试。

## 3. 可恢复工作流：Temporal 与 LangGraph

### 3.1 Temporal

Temporal Workflow 的状态来自持久化事件历史；Worker 中断后可以重放历史并从最近状态继续。外部 API、模型调用和工具调用应封装为 Activity，而不是写进要求确定性的 Workflow 逻辑。[Workflow Execution](https://docs.temporal.io/workflow-execution)、[Temporal 官方架构说明](https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md)

Temporal Activity 默认可能重试，官方明确要求 Activity 幂等，并建议用 Workflow Run ID 与 Activity ID 组合构造幂等键。Activity 还可 heartbeat，适合长任务汇报进度和检测取消。[Activity Definition](https://docs.temporal.io/activity-definition)、[Retry Policies](https://docs.temporal.io/encyclopedia/retry-policies)

适用性：最适合跨进程、跨机器、运行数分钟到数天、需要强恢复语义和运维审计的自建任务平台。代价是新增 Temporal Server、Worker、历史兼容和工作流版本管理，初期改造较重。

### 3.2 LangGraph

LangGraph 在每个 graph step 保存 checkpoint，并以 thread 组织状态；失败后可从最近成功 step 恢复，pending writes 还可避免同一 superstep 中成功节点被全部重跑。[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

但它不是“exactly once”魔法。官方 Functional API 说明 task 可能重执行，因此应保持幂等；interrupt 恢复时节点会从头执行，interrupt 之前的副作用也可能再次发生。[Functional API](https://docs.langchain.com/oss/python/langgraph/functional-api)、[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)

适用性：如果后续自己的 Agent 编排主要使用 Python，LangGraph 能以较低成本快速引入 checkpoint。它比 Temporal 轻，但跨服务长期运行、复杂重试治理和运维可视化通常不如 Temporal 完整。

### 3.3 对现有 Zoo/Kilo/Roo 的硬边界

[Kilo Code](https://github.com/Kilo-Org/kilocode)、[Zoo Code](https://github.com/Zoo-Code-Org/Zoo-Code/) 和 [Roo Code](https://github.com/RooCodeInc/Roo-Code) 是 Agent 客户端：模型提出工具调用，客户端在本地读写文件、执行命令，再把 tool result 带入下一轮模型请求。网关只看到了模型请求和后续回传的消息，无法知道某次本地写文件究竟是“没执行”“执行成功但结果没回传”还是“只执行了一半”。

所以，不能仅靠修改 New API 或 `grokcli-2api` 给这些既有客户端增加完整 durable execution。要获得任务级恢复，至少要满足以下之一：

1. 客户端支持稳定的 `task_id`、`step_id`、幂等键和 checkpoint receipt；
2. 有副作用的工具迁移到受控 MCP/工具服务，由服务端保存执行账本；
3. 使用独立的 durable job API，由 Temporal/LangGraph 编排并执行工具，而不是把它伪装成普通 `/v1/chat/completions`。

## 4. 工具调用的幂等键、状态机和检查点

OpenAI 的 `call_id` 用来关联模型的 function call 和后续 `function_call_output`；MCP 的工具调用 ID 也用于将请求与结果对应。两者都不是对外部副作用的自动去重承诺。[OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)、[MCP Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)、[MCP Schema](https://modelcontextprotocol.io/specification/2025-11-25/schema)

因此需要在业务/工具执行层另设稳定的 `operation_id`，不要直接把模型每次重试都可能变化的 `call_id` 当幂等键。推荐：

```text
operation_id = SHA-256(
  task_id + step_id + tool_name + canonical_json(arguments) + target_revision
)
```

最小状态机：

```text
planned → claimed → executing → succeeded
                         ├──────→ failed_retryable
                         ├──────→ failed_terminal
                         └──────→ unknown_effect
```

每次工具执行至少持久化：

```json
{
  "task_id": "task_...",
  "step_id": "step_...",
  "model_call_id": "call_...",
  "operation_id": "sha256:...",
  "tool_name": "write_file",
  "canonical_arguments": {"path": "...", "content_hash": "..."},
  "expected_target_revision": "sha256:old...",
  "status": "succeeded",
  "result": {"new_revision": "sha256:new..."},
  "attempt": 1
}
```

执行规则：

- **只读工具**：通常可安全重放，但结果可能随时间变化；checkpoint 应记录结果和时间，恢复时明确选择“复用旧结果”还是“重新读取”。
- **文件写入/补丁**：使用 `operation_id` 唯一约束，并附带预期文件 hash/revision；目标已是期望新 hash 时直接返回旧成功结果，旧 hash 不匹配时进入冲突，而不是覆盖。
- **Shell、发消息、支付、创建资源等副作用**：优先把幂等键传给目标服务；目标不支持时，先写 outbox/执行账本，再执行并查询目标状态。连接中断且结果未知时进入 `unknown_effect`，先 reconcile，禁止盲重试。
- **流式工具参数**：只在参数结束事件到达、JSON 完整解析、schema 校验通过并写入 `planned` checkpoint 后执行。OpenAI 的 strict mode 要求 schema 的属性完整且禁止额外属性，可减少参数形状错误，但不提供副作用幂等。[OpenAI Function Calling](https://developers.openai.com/api/docs/guides/function-calling)
- **checkpoint 时机**：完整模型输出、工具执行 claim、工具完成结果、结果回填模型，四个边界分别落盘。若“工具已成功但 tool result 未送达模型”，恢复时读取账本并回填旧结果，不重新执行工具。

Temporal 明确指出 Activity 可能多次执行或部分执行，幂等键应由被调用服务强制执行；这也是上述设计的直接依据。[Temporal Activity Definition](https://docs.temporal.io/activity-definition)

## 5. 额度预估、预占、动态输出上限与上下文压缩

### 5.1 不能只靠轮询和冷却

冷却是事后反应：账号已经因额度不足失败后才退出候选集。要避免“大请求把最后一点额度用完”，需要在选号前进行 admission control，并对并发请求原子预占。

建议的账号预算状态：

```text
account_id
quota_unit                 # token、credit、byte 或供应商自定义单位
remaining_estimate
reserved_inflight
observed_at / reset_at
confidence                 # authoritative、derived、stale
generation                 # 乐观锁版本
```

原子准入条件：

```text
remaining_estimate - reserved_inflight
    >= estimated_request_cost + safety_margin
```

成功选号时原子增加 `reserved_inflight`；请求结束后用实际 usage 结算差额，失败/超时释放预占，租约过期由回收任务处理。Redis Lua、数据库条件更新或带版本号的 compare-and-swap 都可实现，关键是“检查与预占必须是一个原子操作”。

如果上游所谓“1 MB”不是标准 token 配额，不能直接用 tokenizer 换算。应同时记录请求/响应字节、输入/输出 token、reasoning token、工具 schema 大小与供应商返回的实际扣减，以最近窗口的 P90/P95 比例估计本次成本，并保留 15%～25% 安全余量。安全余量属于应按实测校准的工程参数，不是协议常量。

### 5.2 动态 `max_tokens` / `max_output_tokens`

OpenAI 提供 token counting，可对与 Responses 请求相同的输入（包括 tools）进行模型侧计数；Anthropic 也提供 Message token counting，覆盖工具、图片等输入。两者都说明工具定义会进入上下文成本。[OpenAI Counting Tokens](https://developers.openai.com/api/docs/guides/token-counting)、[Anthropic Token Counting](https://platform.claude.com/docs/en/build-with-claude/token-counting)

可以据此计算动态上限：

```text
available = remaining - reserved - safety_margin

safe_output = min(
  client_requested_output,
  model_context_limit - counted_input - protocol_reserve,
  quota_to_output_tokens(available - estimated_input_charge)
)
```

需要注意：

- 动态上限只是避免明显超额，不保证模型一定在逻辑完整处停止。
- 工具场景应预留足够空间生成完整工具名、参数和结束事件；上限太小会提高半截 JSON 的概率。
- 如果计算出的 `safe_output` 小于任务最小可用阈值，应在请求前换账号或拒绝，并返回可重试错误；不要明知无法完成仍启动流。
- 对不提供权威 token count 的 Grok CLI 上游，可用本地近似计数加历史校准，但状态必须标记为 `derived`，选择更大的安全余量。

### 5.3 上下文压缩

OpenAI Responses 支持服务端 compaction：达到阈值后用压缩项延续状态；Anthropic context editing 可清除旧的 tool result；xAI 也提供 context compaction，把历史折叠为可继续传递的加密内容。[OpenAI Compaction](https://developers.openai.com/api/docs/guides/compaction)、[Anthropic Context Editing](https://platform.claude.com/docs/en/build-with-claude/context-editing)、[xAI Context Compaction](https://docs.x.ai/developers/advanced-api-usage/context-compaction)

但压缩解决的是“输入上下文越来越大”，不是“某个账号总余额不够完成一次输出”。并且只有当前上游接口实际支持相应 compaction API 时才能使用；不能假设 New API 的协议转换或 Grok CLI 适配会透明保留供应商私有的压缩对象。

对当前兼容链路，更稳妥的通用做法是：

- 保留系统规则、当前计划、最近 N 轮工具交互和未完成操作；
- 把旧工具输出压缩为结构化摘要，但保留文件路径、hash、错误码和关键事实；
- 不压缩正在等待 tool result 的调用对；
- 摘要本身带版本和来源 checkpoint，避免恢复时混入过期状态；
- 支持原生 compaction 的供应商单独启用，不在协议转换层伪造兼容。

## 6. 方案比较

| 方案 | 账号/上游路由 | 已输出流恢复 | 任务检查点 | 工具幂等 | 对当前场景的定位 |
| --- | --- | --- | --- | --- | --- |
| `grokcli-2api` 当前实现 | 有轮询、冷却、首事件前换号 | 不支持，且正确禁止 | 无 | 无 | 保留为 Grok 协议与账号池适配层 |
| New API | 渠道优先级、权重、多 Key、失败重试 | 无协议保证 | 无 Agent 检查点 | 无 | 继续作为统一入口与跨渠道网关 |
| LiteLLM Proxy | 丰富路由、fallback、TPM/RPM、预算 | 不能恢复已提交 SSE | 无 Agent 检查点 | 无 | 有替换网关需求时评估，不是本问题的必要前置 |
| Envoy | L4/L7 健康、熔断、retry budget、首响应前重试 | 不支持语义续流 | 无 | 无 | 基础设施保护层，不是 Agent 恢复层 |
| LangGraph | 可自行实现模型路由 | 通过下一 checkpoint 重新执行，不是续流 | 强 | 仍需工具实现 | 适合较快建设 Python Agent/任务入口 |
| Temporal | 可在 Activity 中实现路由 | 通过 Workflow/Activity 恢复，不是续流 | 最强 | 明确要求并支持该模式 | 适合长期、多服务、强审计的任务平台 |
| 完整响应缓冲 | 响应下发前可换号 | 成功前客户端看不到任何流 | 无 | 仅模型输出无副作用时安全 | 适合离线/批处理，不宜作为交互默认值 |

## 7. 推荐路线

### 阶段 0：先建立可观测的“提交边界”

不改变外部协议，先统一记录：

```text
task_id / turn_id / request_id / attempt_id
new_api_channel_id / grok_account_id
selected_at / first_upstream_event_at / first_downstream_commit_at / finished_at
input_tokens / output_tokens / reasoning_tokens / request_bytes / response_bytes
failure_class / upstream_status / committed / retry_owner
```

核心指标是：首个下游语义帧前失败率、提交后中断率、每请求实际尝试数、额度预测误差 P50/P95、因预占跳过账号的次数、重复工具操作数。没有这些数据，动态阈值和安全余量只能靠猜。

### 阶段 1：在 `grokcli-2api` 做额度感知准入

这是近期收益最高、改动边界最清晰的位置，因为它最接近真实账号池：

1. 建立 `remaining_estimate + reserved_inflight + confidence`；
2. 以原子操作完成检查和预占；
3. 根据输入计数和历史 P95 消耗动态收紧输出上限；
4. 余额不确定或不足时，在任何下游内容提交前换号；
5. 保留当前 `ErrCommitted` 红线，绝不在提交后静默重跑；
6. New API 只保留有限的跨渠道 fallback，避免嵌套重试放大。

这一步不能让超出单账号容量的不可拆分请求凭空成功，但能显著减少“明知余额不足仍选中”和并发踩空。

### 阶段 2：把交互兼容与可恢复任务拆成两个入口

**兼容模式**继续提供 OpenAI/Claude API，服务 Zoo/Kilo/Roo：

- 保留实时流；
- 仅首语义帧前自动换号；
- 提交后失败返回明确终止，不伪装成功；
- 客户端在下一个 model turn 继续，工具仍由客户端负责。

**Durable Job 模式**增加任务接口：

```text
POST /tasks             # 提交任务，返回 task_id
GET  /tasks/{id}        # 查询状态和检查点
GET  /tasks/{id}/events # 仅用于观察进度，不承担恢复游标
POST /tasks/{id}/resume # 人工或策略恢复
```

任务由 LangGraph 或 Temporal 驱动，工具通过受控 MCP/工具服务执行并写幂等账本。账号耗尽时结束当前模型 Activity，从最近 checkpoint 选新账号发起下一 turn，而不是拼接两个账号的半截 SSE。

### 阶段 3：LangGraph 起步，达到条件后再上 Temporal

推荐选择：

- **近期**：若主要目标是自己的代码 Agent/自动任务，先用 LangGraph + PostgreSQL checkpointer，快速验证 step 粒度、operation ledger 和恢复语义。
- **长期**：当任务跨多个服务、持续时间长、并发和审计要求高，或需要成熟的 timeout/retry/heartbeat/worker 故障恢复时，迁移或新建 Temporal Workflow。
- **暂不建议**：仅为了账号中途耗尽就整体替换 New API 为 LiteLLM，或在前面加 Envoy 后期待其解决任务恢复。两者可以增强网关，但不会改变“已提交流不可透明重试”和“本地工具副作用不可见”这两个根因。

## 8. 最终建议架构

```text
                         ┌─ 兼容模式：Zoo / Kilo / Roo
                         │  实时 SSE；只做 commit 前 failover
                         ▼
客户端 ───────────────→ New API ─────────────→ grokcli-2api ─→ Grok 账号池
                         │                      │
                         │ 跨渠道策略/计费       └─ 余额预估、原子预占、账号冷却
                         │
                         └─ Durable Job API
                              │
                              ▼
                    LangGraph（近期）/ Temporal（长期）
                              │
                              ├─ task/step checkpoint
                              ├─ tool operation ledger
                              └─ 受控 MCP / 工具执行服务
```

核心原则可以压缩成三句话：

1. **网关只承诺响应提交前的请求级换号。**
2. **额度不足尽量在选号前通过预估与原子预占避免。**
3. **已经发生的模型输出和工具副作用，由任务状态机、检查点和幂等账本恢复。**

这条路线既保留当前 Zoo/Kilo/Roo 的兼容性，也为真正的大任务提供可恢复执行，不需要让每一种客户端、每一个新工具继续以“碰一个补一个”的方式修改协议适配器。
