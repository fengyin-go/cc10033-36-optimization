# 维度建模开发中心

面向数据仓库建设场景，提供维度建模、事实表设计、缓慢变化维和汇总层构建的建模平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 维度定义 | `dim_definition` | 维度表 | 维度编码、维度名称、主键定义 |
| 维度属性 | `dim_attr` | 属性字段 | 属性名称、所属维度、数据类型 |
| 事实设计 | `fact_design` | 事实表 | 事实名称、业务过程、粒度声明 |
| 度量配置 | `measure_config` | 度量字段 | 度量名称、所属事实、度量类型 |
| SCD处理 | `scd_handler` | SCD策略 | 策略名称、目标维度、SCD类型 |
| 代理键 | `surrogate_key` | 键值定义 | 键值名称、目标维度、生成策略 |
| 层级构建 | `hierarchy_build` | 层级关系 | 层级名称、所属维度、父子字段 |
| 星型查询 | `star_query` | 查询定义 | 查询名称、事实表、关联维度 |
| 汇总构建 | `agg_summary` | 汇总表 | 汇总名称、源事实表、汇总维度 |
| 桥接关联 | `dim_bridge` | 桥接表 | 桥接名称、多对多维度、关联事实 |
| ETL映射 | `etl_mapping` | 映射规则 | 规则名称、源表、目标表 |
| 快照计划 | `snapshot_plan` | 快照策略 | 策略名称、目标表、快照周期 |
| 模型血缘 | `model_lineage` | 依赖关系 | 关系ID、上游模型、下游模型 |
| 一致性维度 | `conform_dim` | 标准维度 | 标准名称、维度主题、标准键值 |
| DataVault | `data_vault` | Vault模型 | 模型名称、Hub实体、Link关系 |
| 多维立方 | `cube_builder` | Cube定义 | Cube名称、源事实表、维度组合 |
| 变更日志 | `slow_change_log` | 维度变更 | 变更ID、目标维度、变更字段 |
| 日期维度 | `date_dim_gen` | 日期记录 | 日期键、完整日期、年份 |
| 模型校验 | `model_validate` | 校验规则 | 规则名称、目标模型、校验类型 |
| 部署流水线 | `deploy_pipeline` | 部署任务 | 任务名称、目标模型、部署环境 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 日期分区连续性校验

`backend/app/services/date_partition.py` 提供以业务月（YYYY-MM）为粒度的逐日核对，
替代原先“只看数量”的校验（空分区、重复键、缺日不再能错误通过）：

- 三态结论：`完整` / `异常`（重复键、缺日）/ `暂无数据`；重复日期键不抵扣缺日，
  闰年 2 月 29 日与范围首尾两天都在应到集合内逐项核对。
- 校验记录显式绑定起止日期、覆盖分区、关联事实与数据指纹，
  旧范围的通过结论不会被新范围复用，数据变化后旧结论自动失效。
- 补数会话防并发补数；进行中或已中断的分区视为“可能只落库一半”，
  事实交付门禁与发布动作都会拦截，补齐后可按业务月单独重验。

对外接口（`backend/app/routers/date_validation.py`）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/date-validation/continuity?start=&end=&fact=` | 按起止日期输出连续性结论，写清确认范围与待补齐日期 |
| POST | `/api/date-validation/partitions/{月}/validate` | 重验指定业务月（失败分区补齐后的单独重试） |
| POST | `/api/date-validation/partitions/{月}/backfill` | 补齐缺日并清理重复键，并发补数会被拒绝 |
| GET | `/api/date-validation/facts/{事实}/delivery?months=` | 事实分区交付门禁：可交付分区与仍缺日期键 |
| POST | `/api/date-validation/facts/{事实}/revalidate?month=` | 事实视角重验指定业务月 |
| GET | `/api/date-validation/records` | 校验记录（起止日期 + 覆盖分区 + 数据指纹） |

领域：数据工程
子方向：数仓与数据湖
