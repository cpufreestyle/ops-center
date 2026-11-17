# 运维值班台

给信息中心值班用的小工具：把告警、备份、资产台账三份导出数据，变成“今天必须处理什么”和一份交接班。

## 它解决什么

- 告警重复：同一 host + service + title 合并，保留次数、首次/最近时间、最高级别。
- 备份超时：按日备/周备不同阈值判断，无成功记录直接阻断。
- 资产风险：证书/授权到期、生产资产无负责人、生产服务无备份、单机多关键服务。
- 交接班：自动生成总体、必须处理、近期变更、待观察、交接备注。

## 日常使用

```bash
python3 ops_center.py export-demo   # 第一次：生成 CSV 模板
python3 ops_center.py check         # 检查表头
python3 ops_center.py report        # 打印今天的交接班
python3 ops_center.py report --out ~/Desktop/值班交接-$(date +%F).md
python3 ops_center.py serve         # 打开值班台 UI
```

把监控/备份/资产系统导出的 CSV 放进 `data/inbox/`，刷新页面或重新运行 `report` 即可。

每天 5 分钟：

1. 8:50 从监控导出 `alerts.csv`，从备份系统导出 `backups.csv`，资产台账导出 `assets.csv`。
2. 丢进 `data/inbox/`，运行 `python3 ops_center.py check`。
3. 运行 `report --out` 生成交接班 Markdown。
4. 需要看界面时运行 `serve`，逐条点“确认”，审计写入 `data/ops_audit.jsonl`。

想用另一套数据测试，不改 `data/inbox/`：

```bash
OPS_DATA_DIR=./data/test-inbox python3 ops_center.py report
OPS_DATA_DIR=./data/test-inbox python3 ops_center.py serve --port 8891
```

## 数据格式

见 [data/inbox/README.md](data/inbox/README.md)。

## 边界

- 只读：不登录、不执行、不改现有系统；CSV 导入 + 规则判断 + 人工确认。
- 本地：默认 `127.0.0.1:8890`，不联网；演示数据全部是合成的。
- 可审计：UI 的“确认”会写入 `data/ops_audit.jsonl`。

## 真实数据

真实导出不要放 `data/inbox/` 后提交。推荐：

```bash
mkdir -p "$HOME/ops-private/inbox"
OPS_DATA_DIR="$HOME/ops-private/inbox" python3 ops_center.py report
```

`data/inbox/*.csv`、`data/private/`、`data/ops_audit.jsonl` 已在 `.gitignore` 里。
对外演示/提交前，至少替换：主机名、IP、人名、单位名、厂商名、告警原文；只保留数量、类型、时间、阈值和问题模式。

## 测试

```bash
python3 -m unittest tests.test_ops_center -v
```

`check` 会校验表头、告警级别、时间格式、资产类型和到期日；数据源有问题会直接标出来。

## 发布与交接

- 当前版本：`v0.1.1`（交接补丁）
- Gitee：https://gitee.com/cpufreestyle/ops-center
- Gitee Releases：https://gitee.com/cpufreestyle/ops-center/releases
- GitHub：https://github.com/cpufreestyle/ops-center
- GitHub Release：https://github.com/cpufreestyle/ops-center/releases/tag/v0.1.1
- 交接文档：[HANDOFF.md](HANDOFF.md)
