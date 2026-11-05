"""HR Copilot 網頁介面：python -m streamlit run app.py"""
import os
import time
from pathlib import Path

import pandas as pd
import streamlit as st

import auth
import clean
import review
from agent import Conversation
from auth import ROLE_NAMES
from query import _clean_dir, _log_dir, audit

st.set_page_config(page_title="HR Copilot", layout="wide")
ss = st.session_state
IDLE_SEC = 30 * 60


def login_page() -> None:
    st.title("HR Copilot 登入")
    with st.form("login"):
        u = st.text_input("帳號")
        p = st.text_input("密碼", type="password")
        if st.form_submit_button("登入"):
            user, reason = auth.login_status(u, p)
            audit({"user": u if auth.get_user(u) else "<unknown>", "event": "login", "status": reason})
            if user:
                ss.clear()
                ss.user, ss.conv, ss.history, ss.last_active = user, None, [], time.time()
                st.rerun()
            st.error("帳號或密碼錯誤，或帳號尚未啟用／暫時鎖定")


def chat_tab() -> None:
    if st.button("開新對話"):
        ss.conv, ss.history = None, []
    for q, a, u in ss.history:
        st.chat_message("user").write(q)
        with st.chat_message("assistant"):
            st.markdown(a)
            st.caption(f"tokens in={u['input']} out={u['output']}｜工具呼叫 {u['tool_calls']}｜{u['seconds']}s｜${u['cost_usd']}")
    q = st.chat_input("用自然語言詢問人事資料")
    if q:
        if ss.conv is None:
            ss.conv = Conversation(ss.user)
        with st.spinner("查詢中…"):
            r = ss.conv.ask(q)
        ss.history.append((q, r["answer"], r["usage"]))
        st.rerun()


def review_tab() -> None:
    """R4：逐筆核准／駁回（只列出自己是審核方的項目；權責在 review.decide 伺服器端檢查）。"""
    mine = review.items_for(ss.user)
    st.write(f"需要您審核的項目：{sum(review.status(x) == 'pending' for x in mine)} 筆待決／共 {len(mine)} 筆。"
             "所有審核方都核准才生效；任一方駁回即駁回，欄位維持缺值。")
    for x in mine:
        c1, c2, c3 = st.columns([6, 1, 1])
        sides = "、".join(f"{'/'.join(r.values())}:{x.get('approvals', {}).get(str(i), {}).get('decision', '未決')}"
                          for i, r in enumerate(x["reviewers"]))
        c1.write(f"`{x['employee_id']}`（{x['file']}）{x['field']}：{x['issue']} → 建議 **{x['proposed']}**｜"
                 f"狀態：{review.status(x)}｜各方：{sides}")
        for col, label, decision in ((c2, "核准", "approved"), (c3, "駁回", "rejected")):
            if col.button(label, key=f"{decision}-{x['id']}"):
                try:
                    review.decide(ss.user, x["id"], decision)
                    audit({"user": ss.user.username, "role": "R4", "event": "review", "item": x["id"],
                           "status": decision})
                except (PermissionError, KeyError, ValueError) as e:
                    st.error(str(e))
                st.rerun()


def activation_tab() -> None:
    names = auth.pending_accounts_for(ss.user)
    if not names:
        st.info("沒有需要您啟用的帳號")
    for n in names:
        c1, c2 = st.columns([6, 1])
        t = auth.get_user(n)
        c1.write(f"`{n}`｜{t.role} {ROLE_NAMES[t.role]}")
        if c2.button("啟用", key=f"act-{n}"):
            try:
                auth.activate(ss.user, n)
                audit({"user": ss.user.username, "role": ss.user.role, "event": "activate_user", "target": n})
            except (PermissionError, ValueError) as e:
                st.error(str(e))
            st.rerun()


def admin_tabs() -> None:
    t1, t2, t3 = st.tabs(["資料清理", "稽核紀錄與用量", "帳號管理"])
    with t1:
        st.write("清理作業只回報統計數字，系統管理員看不到資料內容。")
        if st.button("執行資料清理"):
            data_dir = Path(os.environ.get("HRC_DATA_DIR", "data"))
            try:
                res = clean.run(data_dir, _clean_dir(), ss.user)
                audit({"user": ss.user.username, "role": "R5", "event": "clean", "status": "ok", "result": res})
                st.success(f"完成：{res}")
            except Exception as e:  # 不把錯誤堆疊（可能含資料值）顯示給 R5
                audit({"user": ss.user.username, "role": "R5", "event": "clean", "status": f"error:{type(e).__name__}"})
                st.error(f"清理失敗（{type(e).__name__}），請由維運人員查看主機紀錄。")
        rep = _clean_dir() / "cleaning_report.md"
        if rep.exists():
            st.markdown(rep.read_text(encoding="utf-8"))
    with t2:
        for name in ("llm_usage.jsonl", "audit.jsonl"):
            f = _log_dir() / name
            if f.exists():
                df = pd.read_json(f, lines=True)
                st.subheader(name)
                if name == "llm_usage.jsonl":
                    st.write(df.groupby("model")[["input", "output", "cost_usd"]].sum())
                st.dataframe(df.tail(200).astype(str), width="stretch")
    with t3:
        st.write("新帳號一律為「未啟用」：R1–R4 需由負責的 HRBP 啟用，R5 需由另一位 R5 啟用；不可覆寫既有帳號。")
        with st.form("add"):
            u, p = st.text_input("帳號"), st.text_input("密碼（至少 8 碼）", type="password")
            role = st.selectbox("角色", list(ROLE_NAMES))
            emp = st.text_input("員工編號（R1–R3：本人員工資料，用來推算資料範圍）")
            bg, bu = st.text_input("負責 BG（R4）"), st.text_input("負責 BU（R4）")
            if st.form_submit_button("建立帳號") and u and p:
                try:
                    auth.create_account(ss.user, u, p, role, emp, bg, bu)
                    audit({"user": ss.user.username, "role": "R5", "event": "create_user", "target": u,
                           "target_role": role})
                    st.success(f"已建立 {u}（未啟用）")
                except (PermissionError, ValueError) as e:
                    st.error(str(e))
        st.dataframe(pd.DataFrame(auth.list_accounts()).astype(str), width="stretch")
        activation_tab()


def main() -> None:
    if "user" not in ss:
        login_page()
        return
    if time.time() - ss.get("last_active", 0) > IDLE_SEC:  # 閒置逾時：清除身分與對話
        ss.clear()
        st.warning("閒置超過 30 分鐘，請重新登入。")
        login_page()
        return
    ss.last_active = time.time()
    u = ss.user
    st.sidebar.write(f"**{u.username}**｜{u.role} {ROLE_NAMES[u.role]}")
    st.sidebar.write(f"資料範圍：{u.scope_label}")
    if st.sidebar.button("登出"):
        ss.clear()
        st.rerun()
    if u.role == "R5":
        admin_tabs()
    elif u.role == "R4":
        t1, t2, t3 = st.tabs(["問答", "待人工確認資料", "帳號啟用"])
        with t1:
            chat_tab()
        with t2:
            review_tab()
        with t3:
            activation_tab()
    else:
        chat_tab()


main()
