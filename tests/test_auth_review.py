"""帳號生命週期、登入鎖定、待確認審核權責、權限範圍快取（合成資料）。"""
import pandas as pd
import pytest

from conftest import PASSWORD


# ---------- Top-3：R5 不能讓自己取得 HR 權限；角色與範圍需相符 ----------
def test_r5_created_account_is_inactive_until_r4_activates(users):
    import auth
    auth.create_account(users["admin"], "newr3", "Abcdefg1!", "R3", employee_id="E000003")
    assert auth.login("newr3", "Abcdefg1!") is None
    with pytest.raises(PermissionError):
        auth.activate(users["admin"], "newr3")          # R5 不能啟用 HR 角色
    with pytest.raises(PermissionError):
        auth.activate(users["hr2x"], "newr3")           # 不同 BG 的 HRBP 不能啟用
    auth.activate(users["hr1x"], "newr3")
    assert auth.login("newr3", "Abcdefg1!").role == "R3"


def test_r5_cannot_overwrite_existing_account(users):
    import auth
    with pytest.raises(ValueError):
        auth.create_account(users["admin"], "bg1", "Abcdefg1!", "R2", employee_id="E000002")
    assert auth.login("bg1", PASSWORD) is not None


def test_only_r5_can_create_accounts(users):
    import auth
    with pytest.raises(PermissionError):
        auth.create_account(users["hr1x"], "x", "Abcdefg1!", "R4", assigned_bg="BG-1", assigned_bu="BU-X")


@pytest.mark.parametrize("role,emp", [("R3", "E000001"), ("R2", "E000003"), ("R1", "E000002"), ("R2", "E000101")])
def test_role_must_match_org_level_of_bound_record(users, role, emp):
    import auth
    with pytest.raises(ValueError):
        auth.create_account(users["admin"], "bad", "Abcdefg1!", role, employee_id=emp)


def test_r4_assignment_must_exist(users):
    import auth
    with pytest.raises(ValueError):
        auth.create_account(users["admin"], "bad", "Abcdefg1!", "R4", assigned_bg="BG-2", assigned_bu="BU-Q")


def test_r5_account_needs_another_r5(users):
    import auth
    auth.create_account(users["admin"], "r5b", "Abcdefg1!", "R5")
    with pytest.raises(PermissionError):
        auth.activate(users["admin"], "r5b")            # 建立者不能自己啟用
    auth.activate(users["admin2"], "r5b")
    assert auth.login("r5b", "Abcdefg1!").role == "R5"


# ---------- Top-8：登入鎖定 ----------
def test_lockout_after_repeated_failures(users):
    import auth
    for _ in range(5):
        assert auth.login("bg1", "wrong") is None
    assert auth.login("bg1", PASSWORD) is None
    assert auth.login_status("bg1", PASSWORD)[1] == "locked"


def test_unknown_user_and_wrong_password_look_the_same(users):
    import auth
    assert auth.login_status("nobody", "x")[0] is None and auth.login_status("bg1", "x")[0] is None


# ---------- Top-6：兩方 HRBP 審核、伺服器端權責檢查、歷史 ----------
def test_review_requires_matching_hrbp_and_keeps_history(users):
    import review
    item = next(x for x in review.load_items() if x["field"] == "Level2")
    with pytest.raises(PermissionError):
        review.decide(users["hr1y"], item["id"], "approved")      # 不相關的 HRBP
    with pytest.raises(PermissionError):
        review.decide(users["admin"], item["id"], "approved")     # R5
    review.decide(users["hr1x"], item["id"], "approved")
    assert review.status(review.get(item["id"])) == "pending"
    review.decide(users["hr2x"], item["id"], "rejected")
    x = review.get(item["id"])
    assert review.status(x) == "rejected" and len(x["history"]) == 2


def test_items_visible_only_to_responsible_hrbp(users):
    import review
    assert any(x["field"] == "Level2" for x in review.items_for(users["hr2x"]))
    assert review.items_for(users["hr1y"]) == []


# ---------- Top-10：組織異動後權限範圍要更新 ----------
def test_scope_cache_refreshes_when_data_changes(users, env):
    import os, time
    import auth
    assert auth.get_user("bux").scope[-1] == ("Level3", "BU-X")
    p = env / "work" / "active.csv"
    df = pd.read_csv(p, dtype=str, keep_default_na=False)
    df.loc[df.EmployeeID == "E000003", "Level3"] = "BU-Y"
    time.sleep(0.05)
    df.to_csv(p, index=False)
    os.utime(p, None)
    assert auth.get_user("bux").scope[-1] == ("Level3", "BU-Y")
