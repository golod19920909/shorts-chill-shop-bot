# AGENTS.md

Guidance for AI agents (and humans acting as one) working in this repository.

## Environment

- Windows / PowerShell. NEVER use bash syntax (`ls`, `cat`, `rm`) in terminal
  commands — use `Get-ChildItem`, `Get-Content`, `Remove-Item`, etc.
- Python: `.venv\Scripts\python.exe` (Python 3.14).
- No git metadata in this folder — don't attempt `git status/diff`.

## Golden rules

1. **Read before editing.** Inspect the real current source (signatures, call
   sites) before claiming anything about behaviour. Never reason from memory
   of a previous session.
2. **Minimal changes.** Touch only files the task requires. Do not rewrite
   working code, do not chase unrelated test failures, do not "improve"
   `services/query_intent.py` unless the assigned bug proves it necessary.
3. **Verify with tests, not claims.** Run the focused test files for the area
   you changed. A full-suite run (`pytest tests -q`, ~40s) is appropriate once
   per session or after broad changes; otherwise stay focused.
4. **No network in tests.** All tests must pass offline with fakes/stubs.
   Any new test must not require Gemini, AliExpress, or Telegram connectivity.
5. **Honest-data invariant.** The bot never fabricates products, prices,
   links, discounts, or "AI results". Any change must preserve this.

## Test commands

```powershell
.venv\Scripts\python.exe -m pytest tests -q                    # full suite
.venv\Scripts\python.exe -m pytest tests/test_agent_service.py -q   # agent loop/tools
```

## Known pitfalls

- `_agent_search_adapter` (handlers/search.py) takes currency as the
  KEYWORD `currency_arg`; passing `currency=` as a keyword collides with the
  removed positional slot. `services.agent_service.run_tool` maps
  `validated["currency"]` → `currency_arg=`. Regression test:
  `HandlerAgentTests::test_agent_search_adapter_accepts_currency_kwarg`.
- The Gemini tool loop disables SDK automatic function calling explicitly
  (`AutomaticFunctionCallingConfig(disable=True)` in
  `services/agent_loop.py::_build_config`) — do not remove this; the SDK
  otherwise emits "Direct use of AFC ... is not recommended" warnings.
- Log files like `bot_stdout.log` are historical runtime logs, not specs.
  `tmp_*` files are scratch artifacts — never treat them as source.

## Where to look first

| Topic | File |
|---|---|
| Agent tool loop | `services/agent_loop.py`, `services/agent_service.py` |
| Search pipeline | `services/search_service.py` |
| Gemini client | `services/ai_service.py` |
| Handler wiring / cards | `handlers/search.py`, `handlers/common.py` |
| Long-form architecture | `DOCS/PROJECT_OVERVIEW.md`, `PROJECT_CONTEXT.md` |
