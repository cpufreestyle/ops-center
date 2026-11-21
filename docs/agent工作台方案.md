# 运维值班台 · Agent 工作台设计方案

> 状态：设计稿，待选定实现方向。基于当前仓库代码（`ops_center.py` 725 行 / `templates/ops.html` 204 行）与《商业化方案》撰写，所有函数引用均对应真实代码。

## 一、背景：我们离 Agent 工作台差多少

### 1.1 现有能力盘点

| 层 | 现状 | 代码位置 |
| --- | --- | --- |
| 数据接入 | `data/inbox/` 三份 CSV（告警/备份/资产），无 CSV 回退内置演示数据；`OPS_DATA_DIR` 可指向仓库外私有目录 | `load_data()` / `data_source()`（:85–108） |
| 数据校验 | 表头、severity 枚举、时间格式、资产类型、到期日逐项校验，错误带行号 | `validate_data()`（:555–609） |
| 规则引擎 | 告警按 host+service+title 合并；备份按日备/周备双阈值；资产四类风险；跨源去重 | `dedupe_alerts` / `check_backups` / `check_assets` / `check_alerts` / `_dedupe_cross_source`（:118–373） |
| 结果组织 | 统一 findings（含 severity/detail/suggestion/evidence/owner），聚合成 state | `build_findings()` / `build_state()`（:374–476） |
| 交接产出 | 交接班 Markdown（总体/必须处理/近期变更/待观察/备注） | `handover_markdown()`（:402–437） |
| AI 环节 | 把 state 拼成一段 prompt 文本，靠人复制粘贴到办公小浣熊 | `xhx_prompt()`（:440–456）+ UI「小浣熊·PPT 任务包」 |
| 服务与审计 | 本地 HTTP（127.0.0.1:8890），`/api/state`、`/api/assets`、`/api/handover`、`/api/xhx-prompt`；人工「确认」写 `data/ops_audit.jsonl` | `serve()` / `Handler`（:612–682） |
| 旁路展示 | Kindle 白底黑字看板，5 分钟自动刷新 | `kindle_page()`（:479–524） |

### 1.2 关键差距

现在的「小浣熊」本质是一个**文案生成器**：产出一段 prompt，靠人肉搬运到另一个窗口。它不会自己查数据、不会自己跑规则、不会追问、不会把结果写回工作台。

Agent 工作台与它的区别只有一点：**Agent 自己调用工具**。把已经写好的纯函数包装成 Agent 可调用工具，人用自然语言下指令，Agent 自己走完「查数据 → 跑规则 → 解释证据 → 生成交接/汇报」。

好消息：本项目的函数几乎全是纯函数、输入输出为 dict/list，天然适合包装成工具，改造成本低。缺的只是「工具注册 + Agent 循环 + 对话界面」这一层。

## 二、定位与原则

沿用《商业化方案》的既定边界，Agent 化不改变产品定位：

1. **值班视角不变**：终点仍是「本班要处理什么、交给谁」，不是通用聊天机器人。
2. **规则出事实，AI 做组织**：所有数字、状态、风险等级必须来自工具返回值；Agent 只负责摘要、解释、追问和汇报编排，禁止自行推算或补数。
3. **只读优先，动作留痕**：查询类工具全自动执行；写动作（确认发现、导出文件）必须人工点击，全部写审计日志。
4. **数据不出域**：LLM 调用可配置为内网/本地模型；演示与测试全用合成数据。
5. **可降级**：LLM 不可用时，工作台退回现有规则模式，功能不中断。

## 三、架构：四层，只新增两层

```
┌─────────────────────────────────────────────┐
│ 界面层（改造 templates/ops.html）            │
│  现有 5 个 tab 保留 + 新增「小浣熊对话」面板  │
│  对话流 · 工具调用轨迹 · 证据卡片 · 人工确认  │
├─────────────────────────────────────────────┤
│ Agent 调度层（新增 agent_loop.py）           │
│  system prompt + 工具注册表 + 多轮 tool loop │
│  /api/chat（SSE 或轮询）· 会话审计            │
├─────────────────────────────────────────────┤
│ 工具层（新增 agent_tools.py，薄包装）        │
│  把现有纯函数注册成 JSON Schema 工具          │
├─────────────────────────────────────────────┤
│ 数据与规则层（完全复用 ops_center.py）        │
│  load_data · check_* · build_state · handover│
└─────────────────────────────────────────────┘
```

设计要点：

- **不重写引擎**。`ops_center.py` 保持可独立运行（CLI report/check/serve 不受影响），agent 模块以 import 方式复用。
- **工具层是薄包装**：每个工具 = 名称 + 描述 + JSON Schema 参数 + 一个调用现有函数的 lambda/函数，外加结果裁剪（避免把 16 项资产全量塞进上下文，默认返回摘要 + top N）。
- **Agent 循环极简**：单会话、最多 N 步（建议 8），每步要么调工具要么给最终回答；超限自动停止并说明。不引入 LangChain 等重框架，标准库 + 一个 HTTP 客户端即可。
- **状态快照绑定**：每轮对话开始时取一次 `build_state()` 作为本轮事实基线，工具默认读同一快照，避免对话中途数据变动导致前后矛盾；用户点「刷新」才换快照。

## 四、工具清单（每项对应真实函数）

### 4.1 只读查询类（Agent 可自动调用）

| 工具名 | 包装函数 | 参数 | 返回 | 说明 |
| --- | --- | --- | --- | --- |
| `get_overview` | `build_state()`（:459） | 无 | counts、alerts/backups/assets 汇总、generated_at、source | 本轮快照总览，作为事实基线；不返回全量列表 |
| `list_findings` | `build_findings()`（:374） | severity?（blocker/warning/info）、source?（alert/backup/asset）、limit=10 | findings 摘要（id/severity/title/detail/suggestion/owner） | 「本班必须处理什么」的主入口 |
| `get_finding_detail` | findings 按 id 过滤 | id | 完整 detail + evidence（asset/field/value） | 回答「为什么」时必须引用 evidence，不许编造 |
| `list_alerts` | `dedupe_alerts()`（:118） | severity?、host?、limit=10 | 合并后告警（count/first_seen/last_seen/最高级别） | 保留合并语义，不暴露原始重复条目 |
| `check_backup_status` | `check_backups()`（:190） | 无（读快照） | ok/warning/blocker 分布 + 每项时效小时数与阈值 | 「哪些备份超时」直接命中 |
| `search_assets` | `asset_search()`（:394） | q（名称/IP/负责人/厂商/主机）、limit=10 | 匹配资产列表 | 复用现有搜索字段，行为与 UI 一致 |
| `check_data_quality` | `validate_data()`（:555）改造 | 无 | 结构化校验结果（缺失表头/坏行/行号） | 现函数只 print，需小改为返回 list[dict]，CLI 行为不变 |
| `get_handover` | `handover_markdown()`（:402） | 无 | 交接班 Markdown 全文 | Agent 可引用/摘要，不重写数字 |
| `get_recent_changes` | `assets_list` 过滤 last_change ≤24h | hours=24 | 近期变更资产列表 | 复用 handover 内已有逻辑（:421–427），抽成函数 |

### 4.2 生成类（LLM 自身完成，输入必须来自工具）

| 能力 | 输入 | 输出 | 约束 |
| --- | --- | --- | --- |
| 交接班解读/问答 | `get_overview` + `list_findings` + evidence | 自然语言回答，逐条带 finding id | 数字只能来自工具返回；答不了就说数据里没有 |
| 8 页 PPT 大纲 | 替代 `xhx_prompt()` 的人肉搬运：直接把 state 喂给模型 | `{"slides":[…]}` JSON（与 `docs/xhx/ops_ppt_outline.json` 同构） | 沿用现有约束「不要编造数字」；输出后与 state 数字核对 |
| 值班摘要（Kindle/推送用） | `get_overview` + blocker top3 | ≤200 字摘要 | 供后续定时任务复用 |

### 4.3 写动作类（必须人工确认后执行）

| 工具名 | 落点 | 审计 |
| --- | --- | --- |
| `ack_finding` | 复用 `/api/ack` 逻辑（:661–681），写 `data/ops_audit.jsonl` | Agent 只能「建议确认」，UI 弹确认按钮，人点了才落库；记录 who/when/note |
| `export_handover` | `report --out` 同款写文件（:704–709） | 导出路径限工作目录/用户指定目录，写审计 |

**明确不做**：登录设备、执行命令、改配置、重启服务——与《商业化方案》「只读：不登录、不执行、不改现有系统」一致，Agent 权限不超过现有 UI。

## 五、Agent 调度层设计

### 5.1 循环协议

```
用户消息 → 取快照(build_state) → LLM(带工具 schema)
  ├─ tool_call → 执行工具 → 结果裁剪 → 回填 → 继续（≤8 步）
  └─ 最终回答 → 渲染（文字 + 证据卡片 + 可选动作按钮）
每一步写 data/agent_audit.jsonl：会话id/步号/工具名/参数/结果摘要/耗时
```

### 5.2 System Prompt 骨架（要点）

- 角色：信息中心值班助手，服务对象是值班员和接班员。
- 铁律：所有数字/状态来自工具；每条结论附 finding id 或证据字段；无数据就明说；不给处置命令，只给建议；写动作一律转人工确认。
- 输出风格：先结论后依据，阻断项优先，中文。

### 5.3 模型接入（可插拔）

| 模式 | 场景 | 说明 |
| --- | --- | --- |
| 内网 OpenAI 兼容端点 | 政府内网试点 | `OPS_LLM_BASE_URL` / `OPS_LLM_MODEL` 环境变量；支持本地部署模型 |
| 云端 API | 本机演示/黑客松 | 仅发送合成数据；密钥走环境变量，不落仓库 |
| 无模型（降级） | LLM 不可用 | 对话面板隐藏，`xhx_prompt` 复制粘贴模式保留，规则功能全量可用 |

安全：发送前对 payload 做字段白名单（沿用 README「对外演示前替换主机名/IP/人名」的脱敏清单）；真实数据模式下默认只允许内网端点。

## 六、界面设计（在现有 UI 上增量改）

现有 5 个 tab（发现/告警/备份/资产台账/交接班）全部保留，改动集中在三处：

1. **新增第 6 个 tab「小浣熊」**（或右侧常驻抽屉）：
   - 对话流：用户消息 + Agent 回答；回答中的 finding id 渲染成可点卡片，点击跳回「发现」tab 对应条目。
   - **工具轨迹条**：每轮回答上方折叠展示「调用了 check_backup_status → list_findings(blocker) …」，让值班员知道结论怎么来的——这是和纯聊天窗的本质区别，也是答辩演示点。
   - 快捷指令 chips：「本班必须处理什么」「哪些备份超时」「证书快到期了吗」「生成交接班」「生成 8 页 PPT 大纲」。
2. **替换「小浣熊·PPT 任务包」卡片**：从「复制 prompt」升级为「一键生成大纲」，生成结果直接在面板内预览 8 页标题+要点，可下载 JSON；复制 prompt 作为降级入口保留。
3. **确认动作打通**：Agent 建议「确认 F-xxx」时渲染确认按钮，点击走现有 `/api/ack`，审计不变。

Kindle 看板不加对话（无 JS 约束），仅可选加一行 Agent 生成的 ≤200 字摘要。

## 七、典型对话（验收用例）

| 用户问 | 期望工具链 | 期望回答要点 |
| --- | --- | --- |
| 本班必须处理什么？ | get_overview → list_findings(blocker) | 按优先级列阻断项，每条带 id、建议、负责人 |
| 哪些备份超时了？ | check_backup_status | 列出 blocker/warning 备份，给小时数与阈值，引用 last_success |
| srv-app-01 上有什么？ | search_assets(q=srv-app-01) | 该主机的服务清单 + 单机多关键服务风险提示 |
| 为什么磁盘那条是阻断？ | get_finding_detail(id) | 引用 evidence 原文（使用率/持续时长），不添油加醋 |
| 生成交接班 | get_handover | 输出 Markdown，数字与 state 一致 |
| 帮我确认所有告警 | —（拒绝批量自动写） | 逐条列出待确认项 + 确认按钮，人点才落审计 |
| 把数据库重启一下 | —（越权拒绝） | 明确说明只读边界，给处置建议而非执行 |

这 7 条可直接做成回归测试：前 5 条断言工具调用序列与数字一致性，后 2 条断言拒绝行为。

## 八、实施路线图

### 阶段 A：黑客松/演示版（约 2–3 天工作量）

1. `agent_tools.py`：注册 4.1 的 9 个只读工具（其中 validate_data 小改返回结构化结果）。
2. `agent_loop.py`：极简 tool loop（≤8 步）+ `/api/chat`；接一个 OpenAI 兼容端点。
3. UI 加「小浣熊」tab：对话 + 工具轨迹 + 快捷指令；PPT 大纲一键生成。
4. 用第七节 7 条用例验收；`python3 -m unittest` 现有测试保持全绿。

产出：可演示的「自然语言值班助手」，路演脚本 3:30–3:50 的小浣熊环节从「闪过大纲」升级为「现场对话 + 工具轨迹」。

### 阶段 B：试点版（对齐商业化 90 天 MVP）

- 会话持久化与审计检索（谁问过什么、Agent 答过什么）。
- 内网模型接入 + 字段脱敏白名单；无模型降级路径验收。
- 写动作确认流完善（ack 带 note、export 限目录）。
- 定时 Autopilot（可选）：每天 8:50 自动跑 check → 生成交接班草稿 + 摘要，人上班只做确认——对应「每天 5 分钟」流程的进一步压缩。

### 阶段 C：通用化（赛后按需，不承诺）

工具注册表抽象成场景包（值班包/备份核查包/资产盘点包），支持挂载第二个场景后再考虑独立工作台框架。**先不做**，避免范围膨胀。

## 九、风险与对策

| 风险 | 对策 |
| --- | --- |
| AI 编造数字 | 工具出事实 + 回答附证据 id + PPT 大纲生成后与 state 程序化核对 |
| 上下文塞爆 | 工具默认返回摘要+limit，明细走 get_finding_detail 按需取 |
| 内网无可用 LLM | 三级接入模式，降级保留复制 prompt 与全部规则功能 |
| Agent 越权 | 工具白名单只有只读+人工确认写；system prompt 与工具层双重拒绝 |
| 数据敏感 | 快照脱敏白名单；真实数据默认仅内网端点；审计本地留存 |
| 规则引擎被绕过 | Agent 不直接读 CSV，一切经由现有 check_* 函数，口径唯一 |

## 十、待你拍板的三个问题

1. **模型端点**：演示阶段用哪个 LLM？（云端 API / 你已有的内网兼容端点 / 先做无模型骨架）
2. **对话入口形态**：第 6 个 tab，还是右侧常驻抽屉？（建议 tab，改动最小）
3. **是否要阶段 B 的定时 Autopilot**：涉及定时任务与推送渠道，试点前确认。

---

*本文档为设计稿；未改动任何现有代码。函数行号基于当前 `ops_center.py`（725 行版本），实现时以最新代码为准。*