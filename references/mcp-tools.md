# MCP 工具清单与字段映射

## 一、调用状态一览

| Tool | 状态 | 场景 | 归一化函数 |
|---|---|---|---|
| `now-time-info` | 必调 | 所有场景 | 直接取时间 |
| `query-my-coupons` | 必调 | 所有场景 | `normalize_coupons()` |
| `campaign-calendar` | 必调 | 所有场景 | `normalize_campaigns()` |
| `query-store-coupons` | 条件 | 区分门店券 | `normalize_coupons()` |
| `query-my-account` | 条件 | 积分决策 | 自定义 |
| `mall-points-products` | 条件 | 积分兑换效率 | 自定义 |
| `create-order` | **禁用** | — | — |
| `draw-lottery` | **禁用** | — | — |
| `query-meals` | **未用** | — | — |

禁用原因见 `MCP_INTEGRATION.md` 第三节。

## 二、字段映射表

麦当劳 MCP 各版本返回的字段命名存在差异，引擎采用多字段兼容。

### 券字段

| 语义 | 候选字段名 | 类型 | 缺失降级 |
|---|---|---|---|
| 标识 | `couponId` / `id` / `code` | str | `coupon-{i}` |
| 名称 | `title` / `name` / `couponName` / `couponTitle` / `activityName` | str | **跳过该条** |
| 面值 | `faceValue` / `face_value` / `amount` / `value` / `discount` / `discountAmount` | float | `0.0` |
| 生效日 | `startTime` / `validStartTime` / `beginTime` / `startDate` | date | `today` |
| 失效日 | `expiresAt` / `expireAt` / `endTime` / `validEndTime` / `endDate` | date | `today + 7d` |
| 来源 | `source` / `channel` / `scene` | str | `"query-my-coupons"` |
| 说明 | `desc` / `description` / `useDesc` | str | `""` |
| 可叠加 | `stackable` | bool | `True` |

### 活动字段

| 语义 | 候选字段名 | 类型 | 缺失降级 |
|---|---|---|---|
| 标识 | `id` / `campaignId` / `code` | str | `campaign-{i}` |
| 名称 | `title` / `name` / `activityName` / `campaignName` | str | **跳过该条** |
| 开始日 | `startTime` / `startDate` / `beginTime` | date | `today` |
| 结束日 | `endTime` / `endDate` / `finishTime` | date | `today + 3d` |
| 增益 | `estimatedGain` / `gain` / `value` | float | `0.0` |
| 说明 | `desc` / `description` | str | `""` |

## 三、日期解析

支持以下格式，解析失败时按上表降级：

```
2026-10-12
2026/10/12
2026.10.12
2026-10-12 08:30:00
20261012
```

`datetime` / `date` 对象直接取日期部分。

## 四、返回结构兼容

`_iter_items()` 可从以下包裹中挖出列表：

```json
[{...}]                      // 直接数组
{"data": [...]}
{"records": [...]}
{"list": [...]}
{"items": [...]}
{"result": [...]}
{"result": {"records": [...]}}   // 递归下钻
{"coupons": [...]}
{"campaigns": [...]}
```

### 容错策略

| 异常 | 处理 |
|---|---|
| `None` 输入 | 返回空列表 |
| 列表内混入非字典 | 跳过该条 |
| 券名为空 | 跳过该条（无法展示的券无价值） |
| 面值缺失 | 记为 `0.0`，仍保留 |
| 日期缺失 | 按默认天数兜底 |
| 金额含 `¥` 或千分位 | 自动清洗 |

容错逻辑由 `test_arb_core.py` 第 7~9 组断言覆盖。

## 五、错误处理

| 码 | 含义 | 处理 |
|---|---|---|
| 401 | Token 无效/过期 | 提示重新申请，**不静默降级** |
| 429 | 触发限流 | 降低频率，串行化请求 |

单次分析仅需 6 次调用，远低于 600 次/分钟上限。

单个 Tool 失败时，用已获得的数据继续出报告，并在输出中标注缺失项。
**部分结果优于无结果，但缺失必须对用户可见。**

## 六、Token 安全

- Token 仅存在于本地 MCP 配置
- 仓库内`mcp-config.example.json` 只含 `${MCD_MCP_TOKEN}` 占位符
- Token 绝不写入日志、报告、快照或任何提交内容