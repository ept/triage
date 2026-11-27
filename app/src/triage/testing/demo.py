"""Runs the app on this machine with made-up mail and a pretend model, to try the interface.

    uv run python -m triage.testing.demo --port 8765

Nothing here touches a mail server or a real model: the gateway is the in-memory fake with a
few synthetic messages, and the "model" follows a handful of fixed rules.
"""

from __future__ import annotations

import argparse
import tempfile
from collections.abc import Mapping
from typing import Any

from triage.server import App
from triage.testing.fake_llm import Call, Reply, ScriptedLlm
from triage.testing.samples import demo_gateway


def pretend_model(request: Mapping[str, Any]) -> Reply:
    """Decide what the pretend model does next, from the last message it was shown."""
    messages = request["messages"]
    last = messages[-1]
    if request.get("response_format"):
        return (
            '{"summary": "A synthetic message for the demonstration.", "category": "work", '
            '"needs_reply": true, "urgency": "normal", "deadline": null}'
        )
    if last["role"] == "tool":
        content = str(last["content"])
        if "<search-results>" in content:
            handles = [part.split("]")[0] for part in content.split("[")[1:4]]
            listed = ", ".join(f"[{handle}]" for handle in handles)
            return f"Here is what I found: {listed}. Open one to read it, or ask me about it."
        return f"Done. {content.splitlines()[0][:200]}"
    text = str(last.get("content", "")).lower()
    if any(word in text for word in ("dark mode", "feature", "the app", "button")):
        return Call("request_app_change")
    if "from now on" in text or "policy" in text:
        return Call(
            "propose_policy_change",
            {"new_policy": "Archive newsletters after a week.\nFlag anything from Alex Sample."},
        )
    if "folder" in text:
        return Call("list_folders")
    # Search for the longest word of the question, which tends to be the most specific.
    words = sorted(text.replace("?", " ").split(), key=len, reverse=True)[:1]
    return Call("search_mail", {"query": " ".join(words), "limit": 5})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    arguments = parser.parse_args()

    import uvicorn

    fake = demo_gateway()
    fake.llm_handler = ScriptedLlm(default=pretend_model).handler
    with tempfile.TemporaryDirectory(prefix="triage-demo-") as directory:
        app = App(
            gateway_url="http://gateway.invalid",
            cache_dir=f"{directory}/cache",
            config_dir=f"{directory}/config",
            transport=fake.transport(),
        )
        app.start()
        print(f"Email Triage demo on http://127.0.0.1:{arguments.port}/ (synthetic data)")
        try:
            uvicorn.run(app.asgi, host="127.0.0.1", port=arguments.port, log_level="warning")
        finally:
            app.close()


if __name__ == "__main__":
    main()
