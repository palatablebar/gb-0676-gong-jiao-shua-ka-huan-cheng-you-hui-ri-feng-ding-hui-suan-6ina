# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV；日终未放行时返回409。

## 晚到撤销与补录

`POST /api/runs/<id>/corrections` 登记一笔更正，正文：

```json
{"version":0,"void":["0011"],"add":[],"reason":"晚到撤销，凭证 R-9"}
```

- `version` 为本批次当前账册版本（已有更正数），过期返回409；省略则不校验。
- `void` 为要撤销的事件号；`add` 为补录行对象数组，字段与CSV列一致，必须通过线路、票价、行程同一套规则，否则整笔422。
- `reason` 必填，作为可追溯依据写入更正单与导出证据。
- 同一撤销集合与补录内容重试返回同一 `correction_id` 且 `replayed=true`，不会二次冲减。

响应含 `final_rows`（终版账册）、`original_basis`（原清分依据中受影响的卡日）、
`replaced_rows`（被整日重算覆盖的原行）、`adjustments`（逐行程调整分录，含
`TRANSFER_ANCHOR_CHANGED`/`TRANSFER_DISCOUNT_CHANGED`/`CAP_DISCOUNT_CHANGED`/
`EXPECTED_CHANGED`/`DAY_CUMULATIVE_CHANGED`/`VOIDED`/`ADDED` 原因）与
`metrics.checks`（行内票价守恒、应收、换乘、封顶、卡日五项核验）及 `metrics.per_day`。

- `GET /api/runs/<id>/corrections` 更正列表与日终状态。
- `GET /api/runs/<id>/corrections/<cid>` 单笔详情，含 `final_rows` 与 `original_basis`。
- `POST /api/runs/<id>/corrections/<cid>/review` 需 `version` 与 `decision`（已复核/退回复核），版本过期409。更正单裁决后封闭，不能二次复核；退回后需按同一依据重新登记。
- 只有 `已复核` 的更正计入日终账册；`待复核` 不改账但阻断日结，`退回复核` 也不改账且末笔退回同样阻断，直到有一笔新的已复核更正。

批次详情 `GET /api/runs/<id>` 的 `stages` 始终是原始清分，不改写；`settlement`
为日终唯一口径（最终应收、换乘命中、封顶减免、差异、摘要、`ready` 与 `blocking`）。
末笔更正未复核、退回或调整未对平会使 `ready=false`，`/archive` 返回409。
