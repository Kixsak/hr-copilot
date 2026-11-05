"""Agent 迴圈（假 client，不呼叫 API）與清理流程（合成原始檔）測試。"""
import json
from types import SimpleNamespace as NS

import pandas as pd
import pytest

from conftest import COLS


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = self

    def create(self, **kw):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def resp(content, stop):
    return NS(content=content, stop_reason=stop,
              usage=NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0))


def text(t):
    return NS(type="text", text=t)


def tool(spec):
    return NS(type="tool_use", id="t1", name="hr_query", input=spec)


def test_api_error_is_reported_and_history_rolled_back(users):
    import anthropic
    from agent import Conversation
    err = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)  # 不需實際 HTTP 請求
    c = Conversation(users["bg1"], client=FakeClient([err]))
    r = c.ask("BU-X 有幾人？")
    assert "暫時無法回答" in r["answer"] and c.messages == []


def test_missing_api_key_is_reported_not_raised(users):
    from agent import Conversation
    err = TypeError('"Could not resolve authentication method. Expected one of api_key, auth_token, or credentials"')
    c = Conversation(users["bg1"], client=FakeClient([err]))
    r = c.ask("BU-X 有幾人？")
    assert "暫時無法回答" in r["answer"] and "ANTHROPIC_API_KEY" in r["answer"] and c.messages == []


def test_unrelated_type_error_is_not_swallowed(users, monkeypatch):
    from agent import Conversation

    def boom(*a, **k):
        raise TypeError("bug in our code")
    monkeypatch.setattr("agent.run_query", boom)
    c = Conversation(users["bg1"], client=FakeClient([resp([tool({"metrics": ["count"]})], "tool_use")]))
    with pytest.raises(TypeError):
        c.ask("x")


def test_truncated_answer_is_flagged(users):
    from agent import Conversation
    c = Conversation(users["bg1"], client=FakeClient([resp([text("部分內容")], "max_tokens")]))
    assert "截斷" in c.ask("列出全部")["answer"]


def test_truncated_tool_call_is_not_left_dangling(users):
    from agent import Conversation
    c = Conversation(users["bg1"], client=FakeClient([resp([tool({"metrics": ["count"]})], "max_tokens")]))
    c.ask("x")
    assert not any(isinstance(m["content"], list) and any(getattr(b, "type", "") == "tool_use" for b in m["content"])
                   for m in c.messages)


def test_tool_call_goes_through_permission_engine(users):
    from agent import Conversation
    fc = FakeClient([resp([tool({"filters": [{"field": "Level2", "op": "eq", "value": "BG-2"}]})], "tool_use"),
                     resp([text("無法提供")], "end_turn")])
    c = Conversation(users["bg1"], client=fc)
    c.ask("BG-2 有幾人")
    tr = c.messages[2]["content"][0]
    assert tr["is_error"] and "denied" in tr["content"]


def test_logged_question_is_redacted(users, env):
    from agent import Conversation
    Conversation(users["hr1x"], client=FakeClient([resp([text("ok")], "end_turn")])).ask("E000101 的生日 1995-05-05？")
    log = (env / "logs" / "llm_usage.jsonl").read_text(encoding="utf-8")
    assert "E000101" not in log and "1995-05-05" not in log and '"cost_is_estimate": true' in log


def test_client_backend_defaults_to_anthropic_and_foundry_is_opt_in(monkeypatch):
    import anthropic
    import agent
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.delenv("ANTHROPIC_FOUNDRY_RESOURCE", raising=False)
    assert type(agent._client()) is anthropic.Anthropic
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_RESOURCE", "demo-resource")
    monkeypatch.setenv("ANTHROPIC_FOUNDRY_API_KEY", "test-key")
    assert isinstance(agent._client(), anthropic.AnthropicFoundry)


# ---------- 清理：只有 R5、兩方審核者、決定不會套到變動後的資料 ----------
def _raw(tmp_path, level2_of_conflict="BG-2"):
    raw = tmp_path / "raw"
    raw.mkdir(exist_ok=True)
    base = {c: "" for c in COLS}
    base.update(EmpStatus="Active", Level1="Group", BirthDate="1990-01-01", EduLevel="學士", Seniority="3",
                Gender="M", JobGrade="G10", IsManager="N", Function="RD", DL_IDL="IDL", WorkLocation="台灣-台北")
    rows = [{**base, "EmployeeID": "E000001", "Level2": "BG-1", "Level3": "BU-01", "Level4": "BU-01 研發處"},
            {**base, "EmployeeID": "E000002", "Level2": level2_of_conflict,
             "Level3": "BU-01", "Level4": "BU-01 研發處"}]
    pd.DataFrame(rows).to_csv(raw / "employees_active.csv", index=False)
    lv = [{**rows[0], "EmployeeID": "E000009", "EmpStatus": "Inactive", "TermDate": "2026-02-01", "TermType": "自願",
           "TermReason": "x"}]
    pd.DataFrame(lv).to_csv(raw / "employees_inactive.csv", index=False)
    return raw


def test_clean_requires_r5(users, tmp_path):
    import clean
    with pytest.raises(PermissionError):
        clean.run(_raw(tmp_path), tmp_path / "out", users["hr1x"])


def test_level2_conflict_needs_both_sides_and_stale_decision_not_reused(users, tmp_path):
    import clean
    out = tmp_path / "out"
    clean.run(_raw(tmp_path), out, users["admin"])
    items = json.loads((out / "pending_review.json").read_text(encoding="utf-8"))
    it = next(x for x in items if x["field"] == "Level2")
    assert it["reviewers"] == [{"Level2": "BG-1", "Level3": "BU-01"}, {"Level2": "BG-2"}]
    it["approvals"] = {"0": {"decision": "approved", "by": "h", "at": "t"}}
    (out / "pending_review.json").write_text(json.dumps(items), encoding="utf-8")
    clean.run(_raw(tmp_path), out, users["admin"])                       # 同資料重跑：保留決定
    again = json.loads((out / "pending_review.json").read_text(encoding="utf-8"))
    assert next(x for x in again if x["field"] == "Level2")["approvals"]
    clean.run(_raw(tmp_path, "BG-3"), out, users["admin"])             # 原始值變了：舊決定不可套用
    changed = json.loads((out / "pending_review.json").read_text(encoding="utf-8"))
    assert not next(x for x in changed if x["field"] == "Level2").get("approvals")
