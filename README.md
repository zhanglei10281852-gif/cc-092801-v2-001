# 红白喜事服务运营平台

这是一个面向婚庆公司、殡葬服务机构和现场调度人员的 Python 后端服务，用于管理服务套餐、家庭订单、现场执行队列、服务人员、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/ceremony-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

服务订单运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`。

## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。

领取服务单时会下发单调递增的领取世代 `lease_epoch`（fencing token），完成、失败与心跳回执必须原样带回。回执在即时事务中按“服务单状态 + 当前持有者 + 领取世代 + 租约未到期”四重条件校验并以带同样条件的 `UPDATE ... WHERE` 兜底：租约过期、单据已被安全回收/重新领取、世代不符或已处终态的旧回执一律返回 `409 lease_stale`（结构化 `reason` 区分 `lease_expired`/`lease_recycled`/`epoch_mismatch`/`task_finished`/`task_cancelled`），业务写入连同已插入的结果行整体回滚。回收任务会清空旧持有者并提升世代，使后来接手者的进度不可能被旧回执覆盖；每次回收与每次拒绝（含操作者、所报世代、当前持有者和拒绝原因）都写入 `compute_interventions`，拒绝审计在独立事务中提交，不因业务回滚而丢失。服务单查询、重试、取消与批量流程不受该链路影响。
