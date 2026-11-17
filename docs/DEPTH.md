# 深度登记 公交刷卡换乘优惠与日封顶回算

规模：中型。使用者：公交清分中心复核员。核心规则：换乘按上次有效付费行程的时间窗计算，优惠与日封顶按确定顺序裁决并保留分币守恒。

## 功能模块

| 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
|---|---|---|---|---|
| M1 刷卡采信 | 查看并运行刷卡采信 | `/api/taps` | `app/modules/m1_taps/rules.py` | `tests/test_m1.py` |
| M2 线路换乘 | 查看并运行线路换乘 | `/api/routes` | `app/modules/m2_routes/rules.py` | `tests/test_m2.py` |
| M3 票价版本 | 查看并运行票价版本 | `/api/fares` | `app/modules/m3_fares/rules.py` | `tests/test_m3.py` |
| M4 行程配对 | 查看并运行行程配对 | `/api/journeys` | `app/modules/m4_journeys/rules.py` | `tests/test_m4.py` |
| M5 优惠回算 | 查看并运行优惠回算 | `/api/discounts` | `app/modules/m5_discounts/rules.py` | `tests/test_m5.py` |
| M6 清分对账 | 查看并运行清分对账 | `/api/clearing` | `app/modules/m6_clearing/rules.py` | `tests/test_m6.py` |
| M7 日结归档 | 查看并运行日结归档 | `/api/settlement` | `app/modules/m7_settlement/rules.py` | `tests/test_m7.py` |

## 陷阱登记

| TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
|---|---|---|---|---|---|
| TRAP-01 | 旧编码 | 默认解码失败 | 回退GB18030 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_legacy_encoding_real_sample` |
| TRAP-02 | BOM | 首列名多字符 | utf-8-sig | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_bom_real_sample` |
| TRAP-03 | 带引号换行 | 拆成两条 | csv逻辑行 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_quoted_newline_is_one_record` |
| TRAP-05 | 列数不匹配 | 静默错列 | 隔离原行 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_bad_columns_real_sample` |
| TRAP-08 | 前导零ID | 编码变形 | 全程字符串 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_identifier_preserved_real_sample` |
| TRAP-14 | 重复键冲突 | 后写覆盖 | 保留首条并隔离冲突行 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_duplicate_conflict_real_sample` |
| TRAP-18 | 空态三态 | 一律成功 | 分别错误码 | `app/modules/m1_taps/rules.py` | `tests/test_m1.py::test_three_empty_states` |
| TRAP-26 | 旧版本提交 | 覆盖新结果 | 版本条件更新 | `app/store.py` | `tests/test_e2e.py::test_stale_review_409` |
| TRAP-06 | 全角数字 | 转换失败 | NFKC与Decimal | `app/values.py` | `tests/test_m3.py::test_thousands_and_fullwidth` |
| TRAP-07 | 金额哨兵 | 零参与扣费 | 空值隔离 | `app/values.py` | `tests/test_m3.py::test_real_sentinel_quarantined` |
| TRAP-11 | 混合偏移 | 落错版本 | UTC归整 | `app/values.py` | `tests/test_m3.py::test_timezone_equal` |
| TRAP-23 | 换乘截止点 | 重复优惠 | 截止点排除 | `app/modules/m5_discounts/rules.py` | `tests/test_m5.py::test_exact_window_excluded` |
| TRAP-25 | 票价重叠 | 随意取版 | 冲突隔离 | `app/modules/m3_fares/rules.py` | `tests/test_m3.py::test_overlap_rejected` |
| TRAP-27 | 晚到撤销 | 改写已清分行 | 原账只读，另记更正账并整日重算受影响卡日 | `app/corrections.py`、`app/store.py` | `tests/test_corrections.py::test_original_clearing_is_immutable` |
| TRAP-28 | 撤销后的换乘/封顶联动 | 只冲减被撤销行 | 重排同卡同日锚点与封顶，调整分录逐行给出原因 | `app/corrections.py` | `tests/test_corrections.py::test_correction_recomputes_whole_card_day` |
| TRAP-29 | 更正重试 | 重复冲减 | 业务效果幂等键，返回原更正单 | `app/store.py` | `tests/test_corrections.py::test_repeat_correction_does_not_double_post` |
| TRAP-30 | 未复核更正放行 | 提前归档 | 日终口径统一，未复核/未对平 409 | `app/corrections.py`、`app/modules/m7_settlement/rules.py` | `tests/test_corrections.py::test_unreviewed_correction_blocks_archive` |

## 对账规则

输入原始逻辑行=最终采纳来源行+隔离来源行+重复行+空白行+分段表头。派生记录用_sources保存完整来源，因此分组不丢行。业务预期合计与上游申报合计之差等于逐行差异之和。结果按输入摘要、规则版本及参数产生唯一批次，事务写入；同源重跑只复用已有批次。

晚到撤销/补录不进入原批次重算，而是作为更正账追加：受影响卡日整日重排换乘锚点与封顶，未受影响卡日结转，调整分录之和与两版指标之差互为校验。日终只认末版账册；原清分 stages、更正前的 original_basis 与逐行调整原因同时可查，任一更正未复核或调整未对平时日结归档一律 409。

## 已知不覆盖

4MB以上文件需预先按业务键分批；不模拟生产支付与外部回调。人工复核只变更批次状态，不改写原始记录；更正只追加调整账，不回写已清分行。更正不处理跨日换乘锚点（本项目规则本身限定在同一业务日内裁决）。
