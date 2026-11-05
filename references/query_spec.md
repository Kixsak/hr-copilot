# QuerySpec 與計算定義

| 參數 | 說明 |
|---|---|
| `analysis` | `aggregate`（統計，預設）、`attrition`（離職率）、`list`（個人明細，僅 R4） |
| `dataset` | `active`（截至 2026-02-28 在職，預設）、`leavers`（2026 年 2 月離職） |
| `filters` | `[{field, op, value}]`，op：`eq` `ne` `in` `lt` `lte` `gt` `gte` |
| `top` | `{by: Seniority|Age, percent}`：取前 percent%，k = ceil(n × percent%)，與第 k 名同值者一併納入 |
| `group_by` | 分組欄位 |
| `metrics` | `count`、`avg:<數值欄位>`、`dist:<欄位>`（人數與百分比） |
| `fields` / `sort_by` / `limit` | `list` 用：輸出欄位、降冪排序欄位、最多筆數 |

## 計算定義

- 基準日 2026-02-28；`Age` 為基準日足歲，`AgeBand` 為 5 歲區間。
- 離職率 = 2026 年 2 月離職人數 ÷ 2026-02-28 在職人數（`attrition`，另附自願離職率）。
- 某欄位缺值或無效時，用到該欄位的計算會排除該筆，並回報「符合其他條件但該欄位缺值」的筆數（與條件順序無關）。
- 組織欄位待 HRBP 確認的資料不納入，筆數回報在 `pending_hrbp_review_not_included`。
