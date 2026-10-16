"""Exercises the Claude tool-use loop with a scripted fake client (no network, no credentials)."""

from types import SimpleNamespace as NS

from atlas.research import engine, llm


class ScriptedMessages:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.script.pop(0)(kwargs)


def _tool_turn(_req):
    return NS(stop_reason="tool_use", content=[
        NS(type="thinking", thinking=""),
        NS(type="tool_use", id="tu_1", name="get_latest_quote", input={"dataset_id": "CMD.SUGAR.ISA"}),
        NS(type="tool_use", id="tu_2", name="get_futures_curve", input={"underlying": "CMD.SUGAR.ISA"}),
    ])


def _final_turn(req):
    # Write a narrative from the actual tool result so verification can pass.
    import json

    results = req["messages"][-1]["content"]
    quote = json.loads(results[0]["content"])
    return NS(stop_reason="end_turn", content=[NS(type="text", text=(
        f"# Sugar brief\n\n## Executive summary\n\nSugar last printed {quote['value']:.2f} {quote['unit']} "
        f"on {quote['observation_date']} [{quote['evidence_id']}]. Futures curve data is not connected [E2]."
    ))])


def test_llm_loop_runs_tools_and_verifies(seeded, monkeypatch):
    messages = ScriptedMessages([_tool_turn, _final_turn])
    fake_client = NS(beta=NS(messages=messages))
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda *a, **k: fake_client)

    run_id = engine.create_run("What's happening with sugar?", {"engine": "llm", "formats": []})
    result = engine.execute_run(run_id)

    assert result["engine"] == "llm"
    assert result["verification"]["passed"], result["verification"]
    assert [e["tool"] for e in result["evidence"]] == ["get_latest_quote", "futures_curve_unavailable"]
    first = messages.requests[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in first["betas"]
    # Both tool results go back in a single user message, matched by id.
    tool_results = messages.requests[1]["messages"][2]["content"]  # [user, assistant tool_use, user results, ...]
    assert [r["tool_use_id"] for r in tool_results] == ["tu_1", "tu_2"]


def test_llm_failure_falls_back_to_deterministic(seeded, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("auth failed")

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm.anthropic, "Anthropic", boom)
    run_id = engine.create_run("copper outlook", {"engine": "auto", "depth": "quick", "formats": []})
    result = engine.execute_run(run_id)
    assert result["engine"] == "deterministic"
    assert result["verification"]["passed"]


def test_explicit_llm_without_credentials_fails_clearly(seeded, monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    run_id = engine.create_run("sugar", {"engine": "llm", "formats": []})
    result = engine.execute_run(run_id)
    assert "no Anthropic credentials" in result["error"]
    assert engine.get_run(run_id)["status"] == "failed"
