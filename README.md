# cc-schema-lab

CC_Codex 二期（26.11）**数据工程**产品线：**契约换版台**（aiohttp + 原生 JS SPA + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**表契约版本登记 → 破坏性检查 → 换版应用 → 目录对账**

与现有九线正交（不是采集、ETL、同步、解析、编排、摄取、留存、清单、水印）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（表契约 / 换版闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、aiohttp、SQLite |
| 前端 | 原生 JS SPA（全中文），暖杏纸面 + 墨绿强调；顶栏胶囊导航 |
| 测试 | pytest + pytest-aiohttp（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 表契约版本（表名 / 版本号 / 现行或草稿）
2. 对候选字段做破坏性检查（删列 / 改类型）
3. 换版应用（草稿升现行，旧现行停用）
4. 契约与目录表对账

## 本轮新增：误升回退

换版升错了版本要退回上一版，契约与目录各退各的，同时保证目录指针 / 附页 / 对账水线都落在同一版号上。

**回退命运只贯彻一条**（二选一，选边决定误升版是否仍可读）：

| 命运 | 契约状态 | 目录附页 | 误升版可否查阅 |
|------|----------|----------|----------------|
| 整笔回滚 | `停用` | `停用` | 否（读详情 409） |
| 部分留存 | `只读历史` | `只读历史` | 是 |

**四处联查**（误升回退页右栏随选中表联动）：

- **回滚卡片**：亮窗口剩余时限与命运芯片，写明误升版去向；
- **版本时间线**：登记 / 换版 / 回退让位 / 挂回现行逐条留痕；
- **对账水线**：契约版 / 目录指针 / 目录附页三处并排，同版才算对齐；
- **目录附页**：每版一页，回退时误升版的页跟着命运走。

**四道闸门**：

1. **同源拦截**：契约现行版、目录指针、附页现行页三者不一致就直接拦，先对账再来；
2. **窗口时限**：默认换版后 30 分钟（`ROLLBACK_WINDOW_MINUTES`），超时回退窗口关闭；
3. **值班锁**：同表同一时刻只许一名值班改写版指针，后到者收白话 `409`，不会双写；
4. **两轮复验**：第一轮在回退事务内、第二轮在提交后另开连接，两轮版号全等才落「两轮一致」。

一次回退只落一笔写事务（`BEGIN IMMEDIATE`），出错整体回滚、页面收白话。

预留切口（仍未做）：跨表依赖冻结、双写迁移 vs 锁表切流。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/contracts` | 契约列表（带 `readable`） |
| GET | `/api/contracts/{id}` | 契约字段（`停用` 时 409） |
| POST | `/api/contracts/{id}/breaking-check` | 破坏性检查 |
| POST | `/api/contracts/{id}/apply` | 换版应用（回 `window_deadline`） |
| GET | `/api/timeline?table=` | 版本时间线 |
| GET | `/api/catalog-pages?table=` | 目录附页 |
| GET/POST | `/api/reconcile-reports` | 对账水线与历史 |
| GET | `/api/rollback/targets` | 回滚卡片数据（窗口/拦因/命运选项） |
| GET | `/api/rollback/{table}/preview` | 单表回退预览 |
| POST | `/api/rollback/{table}` | 执行回退（`{mode, actor, note}`） |
| GET | `/api/rollback/rollbacks` | 回滚卡片历史与两轮复验 |
| POST/DELETE | `/api/rollback/{table}/lock` | 占住 / 交还回退锁 |

## 本地

```powershell
cd claude-workspace\cc-schema-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7100/login · **admin / 123456**

## Docker

端口 **7100**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
