# 把账本接进 hub 现有看板：补丁说明（安装时才做，现在不做）

目标：不在 hub 旁边另起一个看板，账本就出现在 hub 现有看板
的“任务”页里。以下三步只在将来用户点头安装时执行，本仓库
现在不改路由器上的任何文件。

## 1. 放文件（将来安装时）

- 把 `seq_runner.py`、`ledger.py`、`hub_adapter.py` 放到 hub
  所在目录（与 `wb_proxy.py` 同级），这样 `hub_adapter`
  才能 `import wb_tasks`。
- 账本目录单独放，不与账号凭证目录混在一起，例如
  `seq-ledger/`（与 `/etc/workbuddy2api/` 账号目录分开）。

## 2. 看板端点（在 `wb_proxy.py` 的 panel 路由里加三条）

- `GET /seq-ledger` → 返回 `Ledger(...).panel_json()`
- `GET /seq-ledger?uid=<uid>` → 该号最近流水（`recent_runs`
  按号过滤，uid 只做精确匹配，不做模糊搜索）
- `POST /seq-ledger/trigger` → 手动触发一次当日队列，
  复用 `/scheduler/trigger` 同一套 panel 鉴权与防重入锁，
  不要另起一套鉴权。

三条端点都走 `_is_panel_route` 的 panel 鉴权；账本里不含
token，返回前仍只给脱敏后的 `display`（见 `ledger.mask_uid`）。

## 3. 看板页面（在 `dashboard.html` 的任务页里加一段）

- 把 `dashboard_fragment.html` 的 `<div id="seqLedgerSection">`
  插到任务页调度器条（`#schedulerBar`）之后，不要新建主 tab。
- 在任务页的刷新函数里加一行调用 `renderSeqLedger()`，
  与现有的 `getJSON('/scheduler')` 刷新走同一节奏。
- 不要把整个 `dashboard.html`（18003 行）复制进本仓库：
  上游一升级就漂移，补丁说明比全量复制更耐用。

## 4. 调度器并存规则（安装时必须先定）

- 本顺序队列接管凌晨一趟后，hub 自带调度器里重叠的
  签到/旅行/夜猫/国际活跃时段要关掉或改期，否则两个调度器
  会轮流尝试同一批任务。hub 的 `can_*` 守卫会让重复尝试
  多半是空操作，但“多半”不是安装方案。
- Token 保活（hub 每日 22:00）与本队列分属两条线：
  保活不动，凌晨队列里不重复做保活，除非某号在凌晨
  轮到它时已临近过期（`hub_adapter` 里只在临期时刷新，
  不常规刷新）。
