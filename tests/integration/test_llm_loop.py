"""Exercises the Claude tool-use loop with a scripted fake client (no network, no credentials)."""

from types import SimpleNamespace as NS

import pytest

from atlas.research import engine, llm


class ScriptedMessages:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.script.pop(0)(kwargs)


USAGE = NS(input_tokens=1000, output_tokens=200, cache_read_input_tokens=5000, cache_creation_input_tokens=0)


def _tool_turn(_req):
    return NS(stop_reason="tool_use", model="claude-opus-5-5", usage=USAGE, content=[
        NS(type="thinking", thinking=""),
        NS(type="tool_use", id="tu_1", name="get_latest_quote", input={"dataset_id": "CMD.SUGAR.ISA"}),
        NS(type="tool_use", id="tu_2", name="get_futures_curve", input={"underlying": "CMD.SUGAR.ISA"}),
    ])


def _final_turn(req):
    # Write a narrative from the actual tool result so verification can pass.
    import json

    results = req["messages"][-1]["content"]
    quote = json.loads(results[0]["content"])
    return NS(stop_reason="end_turn", model="claude-opus-5-5", usage=USAGE, content=[NS(type="text", text=(
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
    # Token usage and cost are recorded on the run: 2 calls x (1000 in, 200 out, 5000 cache-read) at Opus 5.5 prices.
    expected = 2 * (1000 * 4.00 + 200 * 20.00 + 5000 * 0.20) / 1e6
    assert result["llm_usage"]["requests"] == 2 and result["llm_usage"]["cost_usd"] == pytest.approx(expected)
    assert engine.get_run(run_id)["cost_usd"] == pytest.approx(expected)
    assert first["cache_control"] == {"type": "ephemeral"}
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


def test_monthly_budget_degrades_auto_and_blocks_explicit_llm(seeded, monkeypatch):
    from atlas import auth

    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(auth, "llm_budget", lambda ws: 0.0)
    auto = engine.execute_run(engine.create_run("copper outlook", {"engine": "auto", "depth": "quick", "formats": []}))
    assert auto["engine"] == "deterministic"
    run = engine.list_runs(1)[0]
    assert any("budget reached" in p["message"] for p in engine.get_run(run["run_id"])["progress"])
    explicit = engine.execute_run(engine.create_run("copper outlook", {"engine": "llm", "formats": []}))
    assert "budget reached" in explicit["error"]


def test_follow_up_gives_claude_the_thread_without_stale_evidence_ids(seeded, monkeypatch):
    parent = engine.create_run("Analyze the relationship between the Brazilian real and sugar prices over the last five years.",
                               {"engine": "deterministic", "formats": [], "cache": False})
    engine.execute_run(parent)

    messages = ScriptedMessages([_tool_turn, _final_turn])
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm.anthropic, "Anthropic", lambda *a, **k: NS(beta=NS(messages=messages)))
    child = engine.create_run("What about over ten years?", {"engine": "llm", "formats": [], "cache": False, "parent_run_id": parent})
    result = engine.execute_run(child)

    prompt = messages.requests[0]["messages"][0]["content"]
    assert "follow-up in a research thread" in prompt and "relationship between the Brazilian real" in prompt
    previous = prompt.split("<previous_answer>")[1].split("</previous_answer>")[0]
    assert "[E" not in previous  # earlier evidence ids are not valid in this run
    assert result["verification"]["passed"]
    assert "*Follow-up to: Analyze the relationship" in result["narrative_markdown"]
