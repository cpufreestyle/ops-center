# HANDOFF — 运维值班台

最后更新：2026-11-17
当前版本：`v0.1.1`（交接补丁）
状态：可运行 MVP，已发布到 Gitee / GitHub

## 0. 仓库与版本

- Gitee：https://gitee.com/cpufreestyle/ops-center
- Gitee Releases：https://gitee.com/cpufreestyle/ops-center/releases
- GitHub：https://github.com/cpufreestyle/ops-center
- GitHub Release：https://github.com/cpufreestyle/ops-center/releases/tag/v0.1.1
- 本地目录：`/Users/a1-6/AI Shared/repo/ops-center`
- 默认分支：`master`

版本：

- `v0.1.1`：交接补丁，新增本文件，README/OPS_CENTER 补发布链接。
- `v0.1.0`：首个 MVP，提交 `4bfce8e`。

## 1. 一句话

把监控告警、备份结果、资产台账三份 CSV，合成“本班必须处理什么”和一份可交接的 Markdown；不接内网、不登录、不改现有系统。

## 2. 现在能跑什么

- `export-demo`：生成 `assets.csv` / `backups.csv` / `alerts.csv` 模板。
- `check`：校验表头、告警级别、时间格式、资产类型、到期日。
- `report`：生成交接班 Markdown，包含总体、必须处理、近期变更、待观察、交接备注。
- `serve`：启动本地 UI，支持确认告警，审计写 `data/ops_audit.jsonl`。
- `/kindle`：Kindle 白底黑字看板，无 JavaScript，每 5 分钟刷新。
- 小浣熊：`docs/xhx/` 保留检查规则、8 页交接 PPT 大纲、300 字简介、4 分钟路演脚本。

## 3. 目录

```text
ops_center.py                 主程序（纯标准库）
templates/ops.html            值班台 UI
config/ops_assets.json        合成资产演示数据
data/ops_alerts.json          合成告警演示数据
data/inbox/README.md          CSV 字段说明
kindle/README.md              Kindle 看板使用说明
tests/test_ops_center.py      11 个单元测试
docs/ops-center.png           UI 截图
docs/kindle-board.png         Kindle 看板截图
docs/商业化方案.md            客户、定价、90 天试点
docs/xhx/                     小浣熊 prompt 与输出
```

## 4. 运行

```bash
cd "/Users/a1-6/AI Shared/repo/ops-center"
python3 ops_center.py export-demo
python3 ops_center.py check
python3 ops_center.py report
python3 ops_center.py serve
# http://127.0.0.1:8890
```

真实数据不要放仓库：

```bash
mkdir -p "$HOME/ops-private/inbox"
OPS_DATA_DIR="$HOME/ops-private/inbox" python3 ops_center.py report
OPS_DATA_DIR="$HOME/ops-private/inbox" python3 ops_center.py serve --port 8891
```

## 5. 数据契约

`data/inbox/` 下三份 CSV：

- `alerts.csv`：`host,service,title,level,first_seen,last_seen,count,source`
- `backups.csv`：`asset,plan,last_success,status,size_gb`
- `assets.csv`：`asset,type,env,owner,criticality,expires_at,backup_policy,service`

字段细节见 `data/inbox/README.md`。时间支持 ISO 8601、`Z` 后缀和 `YYYY-MM-DD HH:MM:SS`；无时区按 Asia/Shanghai。

## 6. 关键实现

- `ops_center.py`
  - `parse_ts`：统一时间解析，处理 naive/Z。
  - `load_csv` / `load_demo`：CSV 与内置演示数据。
  - `dedupe_alerts`：按 `host + service + title` 合并，保留次数、首次/最近时间、最高级别。
  - `check_backups`：日备/周备阈值，无成功记录/超时直接阻断。
  - `check_assets`：证书/授权到期、生产资产无负责人、生产服务无备份、单机多关键服务。
  - `build_state`：汇总 findings、备份、资产、交接班。
  - `render_markdown` / `render_kindle`：交接班与 Kindle 页面。
  - `serve`：本地 HTTP 服务与确认审计。

## 7. 验收

```bash
python3 -m unittest discover -s tests -v
```

当前：11/11 通过。

演示输出（2026-11-17）：

- 资产 16 项；原始告警 6 条，合并后 5 条。
- 阻断 5、警告 6、提示 2。
- 备份：正常 0、警告 2、异常 2（演示数据时间已过期，属预期）。

## 8. 已知限制

- 只读 CSV：不做采集、登录、执行、改配置。
- 规则是阈值 + 启发式，不是 AI 根因分析。
- 没有 RBAC、多租户、告警发送、工单系统双向同步。
- Kindle 是 5 分钟刷新的看板，不是实时 pager。
- 演示数据是合成的；真实数据必须脱敏，且不要提交到仓库。

## 9. 下一步

- P0：真实 CSV 字段映射配置化；把去重键、备份阈值、到期阈值做成配置。
- P1：交接班模板/审批/PDF 导出；确认记录可回滚。
- P1：接入 Zabbix/Prometheus/备份系统只读 API，减少手工导出。
- P2：小浣熊 API 自动生成 PPT；多租户/RBAC。
- P2：把“值班台”扩展成“交接报告”产品：证据、风险、计划三件套。

## 10. 4 分钟演示

1. 0:00—0:30：问题——值班每天在群里问“有没有事”，交接靠嘴。
2. 0:30—1:30：三份 CSV 丢进 `data/inbox/`，`check` 报错，`report` 出交接班。
3. 1:30—2:30：UI 看 5 条阻断，逐条确认，审计落盘。
4. 2:30—3:10：Kindle 看板放在桌上，抬头一眼看到本班 3 件事。
5. 3:10—4:00：小浣熊生成 8 页交接 PPT 大纲；边界是只读、本地、可审计。

## 11. 安全

- 默认只监听 `127.0.0.1:8890`，不要暴露公网。
- 真实导出放 `$HOME/ops-private/inbox`，用 `OPS_DATA_DIR` 指向。
- `data/inbox/*.csv`、`data/private/`、`data/ops_audit.jsonl` 已在 `.gitignore`。
- 对外演示前替换主机名、IP、人名、单位名、厂商名和告警原文。
