"""權限與計算引擎測試（合成資料，不呼叫 LLM）。"""
import json

import pytest

from conftest import PASSWORD

U35_RD = [{"field": "Level3", "op": "eq", "value": "BU-X"}, {"field": "Age", "op": "lt", "value": 35},
      {"field": "IsManager", "op": "eq", "value": "N"}, {"field": "DL_IDL", "op": "eq", "value": "IDL"},
      {"field": "Function", "op": "eq", "value": "RD"}]


def q(user, spec):
    from query import run_query
    return run_query(user, spec)


# ---------- 既有行為（由真實資料改為合成資料） ----------
def test_scope_is_full_org_path_from_own_record(users):
    assert users["bg1"].scope == (("Level1", "Group"), ("Level2", "BG-1"))
    assert users["bux"].scope == (("Level1", "Group"), ("Level2", "BG-1"), ("Level3", "BU-X"))
    assert users["group_head"].scope == (("Level1", "Group"),)
    assert users["hr1x"].scope == (("Level2", "BG-1"), ("Level3", "BU-X"))


def test_r2_cannot_query_other_bg(users):
    r = q(users["bg1"], {"filters": [{"field": "Level2", "op": "eq", "value": "BG-2"}]})
    assert not r["ok"] and r["status"] == "denied"


@pytest.mark.parametrize("name", ["group_head", "bg1", "bux"])
def test_non_hr_cannot_list_or_see_birthdate(users, name):
    assert not q(users[name], {"analysis": "list"})["ok"]
    assert not q(users[name], {"metrics": ["dist:BirthDate"]})["ok"]


def test_r3_cannot_use_perf_rating_but_r2_can(users):
    assert not q(users["bux"], {"metrics": ["dist:PerfRating"]})["ok"]
    assert q(users["bg1"], {"metrics": ["dist:PerfRating"]})["ok"]


def test_r5_sees_no_hr_data(users):
    assert not q(users["admin"], {"metrics": ["count"]})["ok"]


def test_invalid_spec_is_rejected_not_raised(users):
    r = q(users["bg1"], {"filters": [{"field": "Salary", "op": "eq", "value": 1}]})
    assert not r["ok"] and r["status"] == "invalid"


def test_exclusion_count_independent_of_filter_order(users):
    a = q(users["bg1"], {"filters": U35_RD, "metrics": ["count", "avg:Seniority"]})
    b = q(users["bg1"], {"filters": list(reversed(U35_RD)), "metrics": ["count", "avg:Seniority"]})
    assert a["result"] == b["result"]


# ---------- Top-4：完整組織路徑、待確認資料 fail-closed ----------
def test_same_bu_name_in_other_bg_is_not_visible(users):
    for name in ("bux", "hr1x"):
        r = q(users[name], {"group_by": "Level2", "metrics": ["count"]})
        assert set(r["groups"]) == {"BG-1"}, name
    r = q(users["hr1x"], {"analysis": "list", "fields": ["EmployeeID", "Level2"], "limit": 1000})
    assert {x["Level2"] for x in r["rows"]} == {"BG-1"}


def test_pending_level2_row_excluded_for_all_scoped_roles(users):
    for name in ("bg1", "bux"):
        r = q(users[name], {"metrics": ["count"]})
        assert r.get("pending_hrbp_review_not_included", 0) >= 1, name
    r = q(users["hr1x"], {"analysis": "list", "fields": ["EmployeeID"], "limit": 1000})
    assert "E000300" not in [x["EmployeeID"] for x in r["rows"]]
    assert r.get("pending_hrbp_review_not_included", 0) >= 1


# ---------- Top-1：小樣本抑制（k=5，只對不可看明細的角色） ----------
# 註：原本用 Level7 + PerfRating 測抑制；#4 修正後該組合對統計角色直接拒絕，故改用一般欄位 Gender 測抑制本身。
def test_small_cells_suppressed_for_aggregate_roles(users):
    spec = {"filters": [{"field": "IsManager", "op": "eq", "value": "Y"}], "group_by": "Level7",
            "metrics": ["count", "dist:Gender"]}
    g = q(users["bg1"], spec)["groups"]["BU-X 研發一部 第1課 第9組"]
    assert g.get("suppressed") and "dist:Gender" not in g
    spec4 = {**spec, "metrics": ["count", "dist:PerfRating"]}
    g4 = q(users["hr1x"], spec4)["groups"]["BU-X 研發一部 第1課 第9組"]
    assert g4["count"] == 1 and "dist:PerfRating" in g4


def test_small_total_suppressed(users):
    r = q(users["bg1"], {"filters": [{"field": "Level7", "op": "eq", "value": "BU-X 研發一部 第1課 第9組"}],
                         "metrics": ["count", "dist:Gender"]})
    assert r["result"].get("suppressed") and r["result"]["count"] == "<5"


# ---------- #4：防止兩次查詢相減推出個人敏感資料 ----------
@pytest.mark.parametrize("spec", [
    {"filters": [{"field": "Level6", "op": "eq", "value": "BU-X 研發一部 第1課"}], "metrics": ["dist:PerfRating"]},
    {"filters": [{"field": "Level6", "op": "eq", "value": "BU-X 研發一部 第1課"},
                 {"field": "IsManager", "op": "eq", "value": "N"}], "metrics": ["dist:PerfRating"]},
    {"group_by": "Level7", "metrics": ["dist:AgeBand"]},
    {"dataset": "leavers", "group_by": "Level6", "metrics": ["dist:TermReason"]},
    {"filters": [{"field": "Level7", "op": "eq", "value": "x"}, {"field": "Age", "op": "lt", "value": 35}]}])
def test_sensitive_fields_denied_at_section_or_team_level_for_aggregate_roles(users, spec):
    assert q(users["bg1"], spec)["status"] == "denied"


def test_sensitive_fields_still_allowed_at_bu_level_and_for_hrbp(users):
    assert q(users["bg1"], {"filters": [{"field": "Level3", "op": "eq", "value": "BU-X"}],
                            "metrics": ["dist:PerfRating"]})["ok"]
    assert q(users["bg1"], {"group_by": "Level6", "metrics": ["count", "dist:Gender"]})["ok"]
    assert q(users["hr1x"], {"filters": [{"field": "Level6", "op": "eq", "value": "BU-X 研發一部 第1課"}],
                             "metrics": ["dist:PerfRating"]})["ok"]


def test_single_small_category_triggers_secondary_suppression(users):
    from query import _dist
    import pandas as pd
    out = _dist(pd.Series(["B"] * 20 + ["C"] * 6 + ["A"] * 1), suppress=True)
    hidden = [k for k, v in out["cells"].items() if v["count"] == "<5" or v.get("suppressed")]
    assert "A" in hidden and len(hidden) >= 2


# ---------- Top-2：非 HR 角色只能看年齡區間 ----------
@pytest.mark.parametrize("spec", [{"metrics": ["dist:Age"]}, {"group_by": "Age"},
                                  {"top": {"by": "Age", "percent": 10}},
                                  {"filters": [{"field": "Age", "op": "eq", "value": 34}]},
                                  {"filters": [{"field": "Age", "op": "lt", "value": 34}]}])
def test_exact_age_denied_for_non_hr(users, spec):
    assert not q(users["bg1"], spec)["ok"]


def test_band_aligned_age_filter_and_ageband_allowed(users):
    assert q(users["bg1"], {"filters": [{"field": "Age", "op": "lt", "value": 35}]})["ok"]
    assert q(users["bg1"], {"metrics": ["dist:AgeBand"]})["ok"]
    assert q(users["hr1x"], {"metrics": ["dist:Age"]})["ok"]


def test_age_boundary_on_base_date(users):
    r = q(users["hr1x"], {"analysis": "list", "fields": ["EmployeeID", "Age"],
                          "filters": [{"field": "EmployeeID", "op": "in", "value": ["E000120", "E000121"]}]})
    ages = {x["EmployeeID"]: x["Age"] for x in r["rows"]}
    assert ages == {"E000120": 35, "E000121": 34}


# ---------- Top-5：人數不因指標欄位缺值而減少 ----------
def test_headcount_keeps_rows_missing_metric_field(users):
    r = q(users["bg1"], {"filters": U35_RD, "metrics": ["count", "avg:Seniority"]})
    assert r["result"]["count"] == 11
    assert r["result"]["avg:Seniority"]["n_valid"] == 9


def test_zero_rows_give_no_average(users):
    r = q(users["hr1x"], {"filters": [{"field": "Seniority", "op": "lt", "value": 0}], "metrics": ["count", "avg:Seniority"]})
    assert r["result"]["count"] == 0 and r["result"]["avg:Seniority"]["value"] is None


# ---------- Top-10：離職期間、N/A、並列、零分母 ----------
def test_attrition_uses_base_month_only_and_reports_ties(users):
    r = q(users["hr1x"], {"analysis": "attrition", "group_by": "Level3"})
    g = {x["group"]: x for x in r["groups"]}
    assert g["BU-X"]["leavers"] == 6            # 1 月離職者與待確認（兩檔重複）者不計
    assert r["excluded_out_of_period"] == 1
    r2 = q(users["bg1"], {"analysis": "attrition", "group_by": "Level3"})
    g2 = {x["group"]: x for x in r2["groups"]}
    assert g2["BU-Y"]["attrition_rate_pct"] is None   # 分母 0
    assert "highest_attrition_rate_groups" in r2 and "most_leavers_groups" in r2


def test_attrition_term_filter_only_applies_to_leavers(users):
    r = q(users["bg1"], {"analysis": "attrition", "group_by": "Level3",
                         "filters": [{"field": "TermType", "op": "eq", "value": "自願"}]})
    assert r["ok"] and any(isinstance(g["active_month_end"], int) and g["active_month_end"] > 0 for g in r["groups"])


def test_higher_level_manager_is_not_applicable_not_missing(users):
    r = q(users["bg1"], {"group_by": "Level3", "metrics": ["count"]})
    assert r["not_applicable_higher_level_managers"] >= 1
    assert r["excluded_missing_or_invalid"].get("Level3", 0) == 0


# ---------- Top-6：待確認項目套用 ----------
def test_pending_both_and_duplicate_rows_are_excluded(users):
    r = q(users["hr1x"], {"analysis": "list", "fields": ["EmployeeID"], "limit": 1000})
    ids = [x["EmployeeID"] for x in r["rows"]]
    assert "E000400" not in ids and "E000500" not in ids and "" not in ids


def test_approved_items_take_effect_only_after_all_sides(users):
    import review
    items = {x["field"]: x for x in review.load_items()}
    review.decide(users["hr1x"], items["Level2"]["id"], "approved")
    assert q(users["bux"], {"filters": [{"field": "Level3", "op": "eq", "value": "BU-X"}], "metrics": ["count"]}
             ).get("pending_hrbp_review_not_included")
    review.decide(users["hr2x"], items["Level2"]["id"], "approved")
    r = q(users["hr1x"], {"analysis": "list", "fields": ["EmployeeID"], "limit": 1000})
    assert "E000300" in [x["EmployeeID"] for x in r["rows"]]


def test_approved_both_item_moves_record_to_leavers_only(users):
    import review
    both = next(x for x in review.load_items() if x["file"] == "both")
    review.decide(users["hr1x"], both["id"], "approved")
    act = q(users["hr1x"], {"analysis": "list", "fields": ["EmployeeID"], "limit": 1000})
    lv = q(users["hr1x"], {"analysis": "list", "dataset": "leavers", "fields": ["EmployeeID"], "limit": 1000})
    assert "E000400" not in [x["EmployeeID"] for x in act["rows"]]
    assert "E000400" in [x["EmployeeID"] for x in lv["rows"]]


def test_approving_one_duplicate_row_keeps_only_that_row(users):
    import review
    dup = [x for x in review.load_items() if x["field"] == "_row"]
    review.decide(users["hr1x"], dup[1]["id"], "approved")
    r = q(users["hr1x"], {"analysis": "list", "fields": ["EmployeeID", "Seniority"],
                          "filters": [{"field": "EmployeeID", "op": "eq", "value": "E000500"}]})
    assert [x["Seniority"] for x in r["rows"]] == [9.0]


# ---------- Top-7：稽核去識別、資料版本 ----------
def test_audit_redacts_identifiers_and_records_data_version(users, env):
    q(users["hr1x"], {"analysis": "list", "filters": [{"field": "EmployeeID", "op": "eq", "value": "E000101"}]})
    lines = (env / "logs" / "audit.jsonl").read_text(encoding="utf-8")
    assert "E000101" not in lines and '"data_version"' in lines


def test_redact_text_masks_ids_and_dates():
    from query import redact_text
    s = redact_text("E123456 生日 1990-01-02 或 19900102")
    assert "E123456" not in s and "1990-01-02" not in s and "19900102" not in s


# ---------- Top-8：CLI 需要密碼 ----------
def test_cli_requires_password(env, monkeypatch, capsys):
    import query
    monkeypatch.setenv("HRC_USER_ID", "bg1")
    monkeypatch.delenv("HRC_PASSWORD", raising=False)
    monkeypatch.setattr("sys.argv", ["query.py", "--spec", '{"metrics":["count"]}'])
    monkeypatch.setattr("getpass.getpass", lambda *a, **k: "wrong")
    assert query.main() == 1
    monkeypatch.setenv("HRC_PASSWORD", PASSWORD)
    assert query.main() == 0
    assert '"ok": true' in capsys.readouterr().out
