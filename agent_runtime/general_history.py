"""Private translation seam retaining legacy-readable session messages."""

from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)


def completed_turn(history, prompt, answer, *, bounded=False):
    messages = ModelMessagesTypeAdapter.validate_python(history)
    messages += [ModelRequest(parts=[UserPromptPart(prompt)]), ModelResponse(parts=[TextPart(answer)])]
    if bounded:
        # Exact turn input/output remains in owned run rows; this is only the legacy adapter cache.
        messages = messages[-8:]
    encoded = ModelMessagesTypeAdapter.dump_json(messages)
    while bounded and len(encoded) > 250000 and len(messages) > 2:
        messages = messages[2:]
        encoded = ModelMessagesTypeAdapter.dump_json(messages)
    if len(encoded) > 250000:
        raise ValueError("session_history_limit")
    return ModelMessagesTypeAdapter.dump_python(messages, mode="json")


def context(history):
    messages = ModelMessagesTypeAdapter.validate_python(history)
    items = [
        {
            "role": "user" if isinstance(p, UserPromptPart) else "assistant",
            "content": str(p.content)[:1000],
            "truncated": len(str(p.content)) > 1000,
        }
        for m in messages
        for p in m.parts
        if isinstance(p, (UserPromptPart, TextPart))
    ]
    return {"messages": items[-4:], "omitted_messages": max(0, len(items) - 4)}
