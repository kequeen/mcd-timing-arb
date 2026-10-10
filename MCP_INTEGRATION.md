# MCP 集成说明

本文件说明本项目实际使用的麦当劳 MCP Server、Tool、调用流程与业务价值。

> **本文档基于 `mcp.mcd.cn` v1.0.0 真实 API 响应校准**（采集于 2026-10-10），
> 而非依据官方 README 的推测。真实快照见 `fixtures/real-snapshot.json`（已脱敏）。
> 二者存在多处关键差异，全部记录在第四节——**这些差异若不处理，会产生静默的错误结果**。

引擎的定位是**时序决策层**：MCP 负责把真实数据取回来，引擎负责回答
「什么时候用最划算」——这是 MCP 自身不提供的能力。

## 一、接入信息

| 项目 | 内容 |
|---|---|
| MCP Server | 远程托管，`https://mcp.mcd.cn` |
| 服务版本 | `mcd-mcp v1.0.0`（`serverInfo` 实测） |
| 传输协议 | Streamable HTTP |
| 鉴权方式 | 请求头 `Authorization: Bearer <MCP_TOKEN>` |
| 可用工具数 | **35 个**（实测；官方 README 仅列出 32 个） |
| Token 申请 | <https://open.mcd.cn/mcp> |
| 服务规则 | <https://cdn.mcd.cn/cms/pages/MCPServerRules.html> |
| 限流 | 单 Token 每分钟 600 次，超限返回 429 |

配置示例见 `mcp-config.example.json`，仅含环境变量占位符。

## 二、实际调用的 Tool

引擎调用以下 5 个 Tool。**未调用的 Tool 在第五节明确列出，
以证明本项目并非全量罗列 MCP 能力，而是按业务需要精确取用。**

### 1. `now-time-info` —— 获取当前时间信息

- **调用时机**：每次分析的起点，必须最先调用
- **真实返回**：`data.date` / `data.datetime` / `data.timestamp` / `data.timezone`
- **业务价值**：本引擎全部计算以「今天」为基准，时间基准错误会导致
  所有剩余天数计算偏移。**不使用 MCP 返回时间而依赖模型本地时间
  是不可接受的**——两者可能不一致

### 2. `query-my-coupons` —— 我的优惠券查询

- **调用时机**：每次分析的第一环
- **真实返回**：`data.coupons[]`，每条约 60 个字段
- **关键字段**：
  - 面额 `denomination`——**单位「分」**，1390 表示 ¥13.90
  - 生效/失效 `tradeStartDate` / `tradeEndDate`
  - 使用限制 `tags` / `weekdays` / `orderTypes` / `channels`
- **归一化**：`normalize_coupons()` → `Asset(kind="coupon")`
- **业务价值**：唯一权威的「我到底有哪些券」来源。不调用它，
  引擎的所有风险敞口计算都失去依据

### 3. `campaign-calendar` —— 活动日历查询

- **调用时机**：与券查询并行
- **真实返回**：`data.dailyList[].events[]` **三层嵌套**，12 天共 30 场活动
- **关键字段**：活动名在 `articleDto.title`（`activityTitle` 实测**恒为空串**）；
  `activityStage=3` 既用于「往期回顾」也用于「今日进行中」，
  **不能用于判断进行中**；`price` 实测**恒为 `"0"`**，接口不提供增益数值
- **归一化**：`normalize_campaigns()` → `Campaign`
- **业务价值**：提供**活动密度**这一完全基于接口事实的择时信号（见第六节）

### 4. `query-my-account` —— 我的积分查询

- **调用时机**：积分维度的核心数据源
- **真实返回**：全部为**字符串型**
  - `availablePoint` / `accumulativePoint`
  - `currentMouthExpirePoint`（本月将过期）
  - `expiredPoint`（历史已过期）
- **归一化**：`normalize_points()` → `PointsSnapshot`
- **业务价值**：`currentMouthExpirePoint` 是独立于券的第二个时间维度。
  券过期是「这张券作废」，积分过期是「这笔余额归零」，
  两者处置时机不同，不应混算

### 5. `available-coupons` —— 可领券列表

- **调用时机**：盘点可领权益
- **真实返回**：`data[]`，每条含 `couponName` / `couponStatus` / `label`
- **业务价值**：识别「有券可领但未领」——这是一种隐性的资产浪费。
  当前版本仅做展示，**未纳入自动决策**，避免擅自替用户领取

## 三、明确未调用的 Tool 及原因

以下 MCP 能力存在，但本项目**有意不调用**：

| Tool | 未调用原因 |
|---|---|
| `create-order` / `cancel-order` | 本项目是**决策辅助**，不是代下单工具。绝不让 AI 自动产生支付行为 |
| `calculate-price` | 依赖具体购物车，在决策阶段调用会诱导过早进入下单流程 |
| `draw-lottery` | 抽奖是消费行为，不属于「时序决策」范畴 |
| `auto-bind-coupons` | 会自动领取券，属于代替用户做决定 |
| `mall-create-order` | 积分兑换会真实扣减积分，性质同上|
| `delivery-create-address` | 涉及用户地址写入，纯决策场景不需要 |
| `query-meals` / `query-meal-detail` | 菜单数据与时间价值计算无关 |
| `mall-points-products` | 兑换效率分析需要人工配置商品现金等价，暂不自动化 |

这个取舍是刻意的：**一个可信的决策工具不应该有能力替你花钱。**

## 四、真实 API vs 初始假设（关键适配记录）

这是本项目最重要的适配记录。**四处差异如果不处理，会产生静默的错误结果**：

| 语义 | 初始假设 | **真实字段** | 不适配的后果 |
|---|---|---|---|
| 券面额 | `discount` / `amount` | **`denomination`** | 读成 ¥0（字段不存在） |
| 面额单位 | 元 | **分**（1390 = ¥13.90） | **夸大 100 倍** |
| 失效日 | `expireAt` | **`tradeEndDate`** | 落入兜底默认 7 天 |
| 活动名 | `activityTitle` | **`articleDto.title`** | `activityTitle` 恒为 `""`，解析不到|
| 活动结构 | 扁平数组 | **`data.dailyList[].events[]`** | 解析不到任何活动 |

其中「分/元单位混用」最危险：`denomination`（分）与
`discountInfo.discountValue`（元）单位不同，若不区分会把 ¥13.90 读成 ¥1390。

### 零值陷阱

真实返回中 `discountInfo.discountValue="0"` 表示**固定售价券**
（如「随心配1+1套餐」用券价格为 0），是有效数据而非缺失。
因此 `_first_of()` 只把 `None` 和 `""` 视为缺失，**不把 0 当假值跳过**。

同理活动的 `price="0"` 应理解为「接口未提供增益」——
既不编造数字，也不报错。

## 五、字段映射与容错

麦当劳 MCP 字段命名与初始假设差异较大，引擎采用多字段兼容策略。

### 券字段

| 语义 | 兼容字段名 | 缺失降级 |
|---|---|---|
| 券名称 | `title` / `name` / `couponName` / `activityTitle` / `activityName` | **跳过该条** |
| 面额 | `denomination`（分）/ `currentBalance` / `discountInfo.discountValue` / `reducePrice` / `faceValue` / `amount` / `value` | `0.0` |
| 失效日 | `tradeEndDate` / `tradeEndDateTime` / `expiresAt` / `expireAt` / `endTime` | 默认 7 天后 |
| 生效日 | `tradeStartDate` / `tradeStartDateTime` / `startTime` | `today` |

### 活动字段

| 语义 | 兼容字段名 | 缺失降级 |
|---|---|---|
| 活动名 | `articleDto.title` / `activityTitle` / `activityName` | **跳过该条** |
| 开始日 | 父级 `dailyList[].date` / `startTime` / `startDate` | `today` |
| 结束日 | `endTime` / `endDate`（扁平结构）；单日（嵌套结构） | 同开始日 |
| 增益 | `price`（实测恒 0） | `0.0` |

### 日期格式

支持 `2026-10-15` / `2026/10/15` / `2026.10.15` /
`2026-10-15 23:59:59` / `20261015` / `datetime` 对象。

### 返回结构兼容

`_iter_items()` 支持直接数组、`data`、`records`、`list`、`items`、
`result`、`coupons`、`campaigns`，并可递归下钻。
`_iter_campaign_events()` 额外支持 `data.dailyList[].events[]` 三层结构，
**同时保留扁平格式的 start..end 区间**（多日活动不能退化成单日）。

### 错误处理

| MCP 错误码 | 含义 | 引擎处理 |
|---|---|---|
| 401 | Token 无效或过期 | 明确提示重新申请，**不静默降级为「无数据」** |
| 429 | 触发限流 | 降低调用频率，串行化请求（单次分析仅需 5~6 次调用，远低于 600/分钟上限） |

单个 Tool 失败时，引擎继续用已成功获取的数据出报告，并在输出中
标注缺失项——**部分结果优于无结果，且缺失必须对用户可见。**

## 六、活动密度：唯一可靠的择时信号

接口不提供「下单能省多少」，`price` 实测恒为 `0`。
因此本项目**不编造增益数字**，改用完全来自接口事实的信号：

```
密度(日期) = 当日活动场次
```

实测数据（2026-10-10 采集）：

| 日期 | 场次 |
|---|---:|
| 10-10（今日） | 15 |
| 10-11 | 2 |
| 10-12 | 2 |
| 10-13~ 10-15 | 各 1 |
| 10-18 / 10-22 | 各 1 |
| 10-23 | 2 |

### 「今日」被活动堆砌污染

接口把**所有进行中的活动都挂在今天**，导致今天 15 场、未来每天 1~2 场。
若直接取最大密度，答案永远是「今天」，择时完全失效。

因此 `pick_best_density_day()` 只在 `today+1 ~ today+21` 范围内比较。

### 密度不能凌驾于券的寿命

场景：10-12 有 2 场活动（最高密度），但某张 ¥500 券 10-11 到期。
去 10-12 会让这张券归零，密度收益远小于损失。

因此密度评分必须减去等待损失：

```
score(候选日) = 密度 × 10 − 在候选日之前到期的资产面值
```

若最优候选日仍会导致资产归零，则**行动日回退到今天**。
该行为由 `test_density_respects_asset_lifetime` 断言锁定。

## 七、调用流程

```
用户提问「我的券什么时候用划算」
    │
    ├─ 1. now-time-info          ──> 确定时间基准 T0
    │
    ├─ 2. query-my-coupons       ──> 账户券清单（denomination 分 → 元）
    │      campaign-calendar     ──> 活动日历（三层嵌套）
    │      query-my-account      ──> 积分与临期积分
    │      available-coupons     ──> 可领权益盘点
    │
    ├─ 3. 归一化（scripts/arb_core.py）
    │      normalize_coupons()   ──> [Asset, ...]
    │      normalize_campaigns() ──> [Campaign, ...]
    │      normalize_points()    ──> PointsSnapshot
    │
    ├─ 4. 评分（纯函数，无 MCP 依赖）
    │      urgency()             ──> 过期压力（排序用）
    │      decay()               ──> 兑现概率（金额用）
    │      at_risk_value()       ──> 风险敞口
    │      campaign_density()    ──> 逐日活动密度
    │      pick_best_density_day() ──> 行动日（含券寿命约束）
    │
    └─ 5. build_report()         ──> 结构化行动日历
```

第 3~5 步是纯计算，与网络无关，因此可用 `fixtures/real-snapshot.json`
离线复现：

```bash
python3 scripts/run_report.py --snapshot fixtures/real-snapshot.json
```

## 八、业务价值总结

麦当劳 MCP 现有的 35 个 Tool 提供的是**查询能力**：
「你有哪些券」「有什么活动」「多少积分」。

它没有回答的是**决策问题**：「什么时候用？」

本引擎补上的正是这一层：

1. 把 MCP 返回的离散数据，通过兑现概率模型转成**带日期的行动清单**
2. 用**风险敞口**量化「不行动」的代价，而非只罗列券的过期时间
3. 用**活动密度**（唯一的接口事实信号）识别错峰机会，
   且严格约束不让高密度日凌驾于券的寿命之上
4. 引入**积分临期**这一独立时间维度——券过期与积分过期是两种不同的损失