# MCP 工具清单与字段映射

> **本文档基于 `mcp.mcd.cn` v1.0.0 真实 API 快照校准，非推测。**
> 采集时间 2026-10-10，快照见 `fixtures/real-snapshot.json`（已脱敏）。
> 真实结构与最初的离线假设有四处关键差异，均已在代码中适配并加回归测试。

## 一、调用状态一览

实测该 Token 可用工具共 **35 个**（官方 README 仅列出 32 个，
`query-meal-assistance` / `query-survey-coupon` 等未在文档中）。

| Tool | 状态 | 场景 | 归一化函数 |
|---|---|---|---|
| `now-time-info` | 必调 | 所有场景 | 直接取时间 |
| `query-my-coupons` | 必调 | 所有场景 | `normalize_coupons()` |
| `campaign-calendar` | 必调 | 所有场景 | `normalize_campaigns()` |
| `query-my-account` | 必调 | 积分维度 | `normalize_points()` |
| `available-coupons` | 条件 | 可领券盘点 | 未纳入计算 |
| `query-store-coupons` | 条件 | 区分门店券 | `normalize_coupons()` |
| `mall-points-products` | 条件 | 积分兑换效率 | 未纳入计算 |
| `create-order` | **禁用** | — | — |
| `draw-lottery` | **禁用** | — | — |
| `query-meals` | **未用** | — | — |

禁用原因见 `MCP_INTEGRATION.md`。

## 二、真实返回结构 vs 初始假设

这是本项目最重要的适配记录。四处差异如果不处理，会导致**静默的错误结果**：

| 语义 | 初始假设 | **真实字段** | 不适配的后果 |
|---|---|---|---|
| 券面额 | `discount` / `amount` | **`denomination`**（单位「分」） | 读成 ¥0（字段不存在） |
| 面额单位 | 元 | **分**（1390 = ¥13.90） | 夸大 100 倍 |
| 失效日 | `expireAt` | **`tradeEndDate`** | 落入兜底默认 7 天 |
| 活动名 | `activityTitle` | **`articleDto.title`** | `activityTitle` 实测恒为 `""` |
| 活动结构 | 扁平数组 | **`data.dailyList[].events[]`** | 解析不到任何活动 |

其中「分/元单位混用」最危险：`denomination` 与 `discountInfo.discountValue`
单位不同，若不区分会把 ¥13.90 读成 ¥1390。

## 三、券字段映射（真实）

| 语义 | 真实字段 | 类型 | 兼容别名 |
|---|---|---|---|
| 标识 | `id` | str | `couponId` / `code` |
| 名称 | `title` | str | `name` / `couponName` / `activityName` |
| 面额 | `denomination` | int（**分**） | `currentBalance` |
| 面额备选 | `discountInfo.discountValue` | str（元） | — |
| 减免额 | `reducePrice` | str | `faceValue` / `amount` |
| 生效日 | `tradeStartDate` | date | `tradeStartDateTime` |
| 失效日 | `tradeEndDate` | date | `tradeEndDateTime` |
| 使用限制 | `tags` / `weekdays` / `orderTypes` / `channels` | list | — |
| 领取时间 | `receivedDaysMsg` | str | — |

### 零值陷阱

真实返回中 `discountInfo.discountValue="0"` 表示**固定售价券**
（如「随心配1+1套餐」用券价格为 0），是有效数据而非缺失。
因此 `_first_of()` 只把 `None` 和 `""` 视为缺失，**不把 `0` 当假值跳过**。

同理 `campaign` 的 `price="0"` 也应视为「接口未提供增益」，
既不编造也不报错。

## 四、活动字段映射（真实）

`campaign-calendar` 是三层嵌套：

```
data
├── currentTime: "2026-10-10 14:26:40"
├── dailyList[]
│   ├── date: "2026-10-12"
│   ├── dateText: "2026年10月12日"
│   └── events[]
│       ├── activityCode: "b1"
│       ├── activityStage: 3      # 1未开始 2进行中 3已结束
│       ├── activityTitle: ""     # 实测恒空
│       ├── price: "0"            # 实测恒 0
│       └── articleDto
│           ├── title: "蓝莓爆爆珠麦旋风上新"
│           ├── content / highlights
│           └── imgList[]
└── subscribedEvents[]
```

**关键：`activityStage=3` 既用于「往期回顾」也用于「今日进行中」**，
不能用它判断进行中，必须依赖 `date` 与当前时间比较。

## 五、活动密度：唯一可靠的择时信号

接口不提供「下单能省多少」，`price` 实测恒为 `0`。
因此本项目**不编造增益数字**，改用完全来自接口事实的信号：

```
密度(日期) = 当日活动场次
```

实测数据（2026-10-10）：

| 日期 | 场次 |
|---|---:|
| 10-10（今日） | 15 |
| 10-11 | 2 |
| 10-12 | 2 |
| 10-13~10-15 | 各 1 |
| 10-23 | 2 |

### 「今日」被活动堆砌污染

接口把**所有进行中的活动都挂在今天**，导致今天 15 场、
未来每天 1~2 场。若直接取最大密度，答案永远是「今天」，择时失效。

因此 `pick_best_density_day()` 只在 `today+1 ~ today+21` 范围内比较。

### 密度不能凌驾于券的寿命

场景：10-12 有 2 场活动（最高密度），但某张¥500 券 10-11 到期。
去 10-12 会让这张券归零，密度收益远小于损失。

因此密度评分必须减去等待损失：

```
score(候选日) = 密度 × 10 − 在候选日之前到期的资产面值
```

若最优候选日仍会导致资产归零，则**行动日回退到今天**。

## 六、积分字段映射（真实）

```
data.availablePoint            "973.4"   可用积分（字符串）
data.accumulativePoint         "10715.7"  累计积分
data.currentMouthExpirePoint   "36.8"     本月将过期  ← 关键
data.expiredPoint              "1641.3"   历史已过期
data.frozenPoint               "0"
```

全部为**字符串型**，需显式转 float。

`currentMouthExpirePoint` 是独立于券的第二个时间维度：
券过期是「这张券作废」，积分过期是「这笔余额归零」，
两者的处置时机不同，不应混算。

## 七、日期解析

支持：`2026-10-15` / `2026/10/15` / `2026.10.15` /
`2026-10-15 23:59:59` / `20261015` / `datetime` / `date` 对象。

## 八、返回结构兼容

`_iter_items()` 支持：直接数组、`data`、`records`、`list`、`items`、
`result`、`coupons`、`campaigns`，并可递归下钻 `result.records`。

`_iter_campaign_events()` 额外支持 `data.dailyList[].events[]` 三层结构，
**同时保留扁平格式的 start..end 区间**（多日活动不能退化成单日）。

## 九、错误处理

| 码 | 含义 | 处理 |
|---|---|---|
| 401 | Token 无效/过期 | 提示重新申请，不静默降级 |
| 429 | 触发限流 | 降低频率，串行化请求 |

单次分析仅需 4~6 次调用，远低于 600 次/分钟上限。

## 十、Token 安全

- Token 仅存在于 `~/.workbuddy/mcp.json`
- 仓库内 `mcp-config.example.json` 只含 `${MCD_MCP_TOKEN}` 占位符
- `fixtures/real-snapshot.json` 已移除图片签名 URL 与 Token
- 提交前有专项检查确保 Token 不入库