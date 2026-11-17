# data/inbox

把每天从监控、备份、资产台账导出的 CSV 放这里，值班台会优先读 CSV；没有 CSV 时回退到内置演示数据。

## 文件

### `assets.csv`

```
id,name,type,ip,host,port,owner,vendor,env,criticality,expires,last_change
```

- `type`：`server` / `service` / `database` / `certificate` / `license`
- `env`：`生产` / `测试` / `开发`
- `expires`：证书/授权的到期日，`YYYY-MM-DD`
- `last_change`：最近变更时间，ISO 8601，可留空

### `backups.csv`

```
id,name,type,host,path,schedule,last_success,target,env,owner,criticality
```

- `type` 固定 `backup`
- `schedule` 写 `每日 02:00` 或 `每周日 04:00`，用于选择 26h/48h 或 168h/240h 阈值
- `last_success` 为最近一次成功时间，ISO 8601；没有成功记录就留空

### `alerts.csv`

```
id,time,host,service,severity,title,message
```

- `severity`：`critical` / `warning` / `info`
- `time`：ISO 8601
- 同一 `host + service + title` 会自动合并

## 快速开始

```bash
python3 ops_center.py export-demo   # 生成三份模板
python3 ops_center.py check         # 检查表头
python3 ops_center.py report        # 打印交接班
python3 ops_center.py serve         # http://127.0.0.1:8890
```

导出文件用 UTF-8（带不带 BOM 都行）。不需要任何网络、账号或密码。
