import copy

import pytest
from test_general_semantic import http_client
from test_harness_extensions import action, create

from agent_runtime.approval_preview import ApprovalPresentation
from agent_runtime.client_results import outcome
from agent_runtime.extensions import ExtensionRegistry, ToolDefinition
from agent_runtime.general_runtime import general_action
from scripts.showcase_adapter import manifest


class PreviewService:
    version = 1

    async def execute(self, call):
        return {
            "plan_id": "a" * 64,
            "counts": {"input_rows": 200, "accepted": 160, "duplicates": 20, "invalid": 10, "conflicts": 10},
        }

    async def reconcile(self, call):
        return None


async def pending(store, client, plan_id):
    store.extensions = ExtensionRegistry(
        manifest("http://127.0.0.1:12345"),
        handlers={"scripts.showcase_adapter:LocalService": PreviewService()},
    )
    run = await create(client, tools=["crm_preview", "crm_import"])
    await general_action(action(run.id, "crm_preview", {"artifact_id": "fixture"}))
    await general_action(action(run.id, "crm_import", {"plan_id": plan_id}, step=1))
    return await client.get(run.id)


async def test_approval_uses_bound_stored_receipt_and_preserves_exact_arguments(store):
    async with http_client(store) as client:
        run = await pending(store, client, "a" * 64)
        approval = run.approvals[0]
        assert approval.arguments == {"plan_id": "a" * 64}
        assert approval.preview.title == "Import reviewed customers"
        assert approval.preview.source_operation_id == run.id + ":action:0"
        assert {f.label: f.value for f in approval.preview.facts}["Customers to import"] == "160"
        assert not approval.preview.warnings
        assert "Customers to import: 160" in outcome(run).message
        # A later operator edit cannot change the already-persisted approval.
        store.extensions.tools["crm_import"]["approval_presentation"]["title"] = "Changed"
        assert (await client.get(run.id)).approvals[0].preview == approval.preview
        events = await store.events(run.id)
        event = next(e for e in events if e.type == "approval.required")
        assert event.data["preview"] == approval.preview.model_dump()


async def test_different_plan_cannot_borrow_a_previous_preview(store):
    async with http_client(store) as client:
        run = await pending(store, client, "b" * 64)
        preview = run.approvals[0].preview
        assert preview.facts == [] and preview.source_operation_id is None
        assert "could not be matched" in preview.warnings[0]


def test_receipt_fields_require_identity_binding():
    with pytest.raises(ValueError, match="exact argument bindings"):
        ApprovalPresentation(
            title="Import", fields=[{"label": "Count", "source": "receipt", "path": ["count"]}]
        )


def test_money_and_untrusted_control_characters_are_bounded():
    from agent_runtime.approval_preview import PreviewField, display_value

    field = PreviewField(label="Refund", path=["amount"], format="money_minor", currency="USD")
    assert display_value(4900, field) == "USD 49.00"
    with pytest.raises(ValueError):
        display_value(True, field)
    text = display_value("\x1b[31mText\nInjected" + "x" * 700, PreviewField(label="Text", path=["text"]))
    assert "\x1b" not in text and "\n" not in text and len(text) == 600


async def test_referenced_credentials_redacted_from_preview(store, monkeypatch):
    from agent_runtime.approval_preview import build_preview

    monkeypatch.setenv("PREVIEW_TEST_CREDENTIAL", "private-test-value")
    definition = ToolDefinition.model_validate(
        copy.deepcopy(manifest("http://127.0.0.1:12345")["tools"][0])
    ).model_dump()
    definition["config"]["credential_env"] = "PREVIEW_TEST_CREDENTIAL"
    definition["approval_presentation"] = {
        "title": "Inspect",
        "fields": [{"label": "Text", "path": ["text"]}],
    }
    async with store.database.sessions() as db:
        result = await build_preview(db, {}, {"extension": definition}, {"text": "private-test-value"})
    assert "private-test-value" not in str(result)
