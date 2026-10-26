"""The capability registry: the port of Vessel's capability labels.

Every tool, built in or from MCP, is registered with labels or not at all.
"""

import pytest

from vessel_mind.core.capabilities import (
    CapabilityConfigError,
    CapabilityLabels,
    CapabilityRegistry,
)
from vessel_mind.core.chat import ToolSpec


class FakeTool:
    def __init__(self, name: str) -> None:
        self.spec = ToolSpec(name=name, description=f"The {name} tool.", parameters={})

    async def execute(self, input):
        return {"ok": True}


class Warnings:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: str) -> None:
        self.messages.append(message)


def labels(**overrides) -> dict:
    return {
        "resource": "public_corpus",
        "effect": "read",
        "exfil": False,
        "taints_context": False,
    } | overrides


def test_a_labelled_tool_is_registered_and_offered_to_the_model():
    registry = CapabilityRegistry(warn=Warnings())

    assert registry.register(FakeTool("search_docs"), labels())

    entry = registry.get("search_docs")
    assert entry is not None
    assert entry.labels == CapabilityLabels(
        resource="public_corpus", effect="read", exfil=False, taints_context=False
    )
    assert [spec.name for spec in registry.specs()] == ["search_docs"]


def test_a_tool_without_labels_is_not_registered_and_a_warning_says_so():
    warnings = Warnings()
    registry = CapabilityRegistry(warn=warnings)

    assert not registry.register(FakeTool("notion_search"), None)

    assert registry.get("notion_search") is None
    assert registry.specs() == []
    assert warnings.messages == [
        "Capability 'notion_search' was not registered: labels are required."
    ]


@pytest.mark.parametrize(
    ("bad", "field"),
    [
        (labels(resource="the_moon"), "resource"),
        (labels(effect="delete"), "effect"),
        (labels(exfil="yes"), "exfil"),
        ({"resource": "network", "effect": "read", "exfil": True}, "taints_context"),
        (labels(extra=1), "extra"),
    ],
)
def test_invalid_labels_raise_a_config_error_naming_the_tool_and_field(bad, field):
    registry = CapabilityRegistry(warn=Warnings())

    with pytest.raises(CapabilityConfigError, match=rf"fetch\.{field}"):
        registry.register(FakeTool("fetch"), bad)

    assert registry.get("fetch") is None


def test_registering_two_tools_with_one_name_is_a_config_error():
    registry = CapabilityRegistry(warn=Warnings())
    registry.register(FakeTool("search_docs"), labels())

    with pytest.raises(CapabilityConfigError, match="search_docs"):
        registry.register(FakeTool("search_docs"), labels())


def test_tainted_input_private_data_and_exfiltration_together_warn_of_the_trifecta():
    warnings = Warnings()
    registry = CapabilityRegistry(warn=warnings)

    registry.register(FakeTool("fetch_page"), labels(resource="network", taints_context=True))
    registry.register(FakeTool("notion_read"), labels(resource="workspace"))
    assert registry.trifecta_warning is None
    registry.register(FakeTool("send_mail"), labels(resource="network", exfil=True, effect="write"))

    assert registry.trifecta_warning == (
        "The registry combines tainted input ('fetch_page'), private data ('notion_read'), "
        "and exfiltration ('send_mail')."
    )
    assert warnings.messages == [registry.trifecta_warning]
    assert registry.trust_level == "tainted"


@pytest.mark.parametrize("private", ["memory", "filesystem", "workspace"])
def test_each_private_resource_completes_the_trifecta(private):
    registry = CapabilityRegistry(warn=Warnings())
    registry.register(FakeTool("a"), labels(resource="network", taints_context=True))
    registry.register(FakeTool("b"), labels(resource=private))
    registry.register(FakeTool("c"), labels(resource="network", exfil=True))

    assert registry.trifecta_warning is not None


def test_a_public_corpus_search_is_not_private_data():
    # search_docs reads public documents: with a tainting fetch and an exfiltrating
    # tool beside it, nothing private can leak, so there is no trifecta.
    warnings = Warnings()
    registry = CapabilityRegistry(warn=warnings)

    registry.register(FakeTool("search_docs"), labels(resource="public_corpus"))
    registry.register(FakeTool("fetch_page"), labels(resource="network", taints_context=True))
    registry.register(FakeTool("send_mail"), labels(resource="network", exfil=True))

    assert registry.trifecta_warning is None
    assert warnings.messages == []


def test_a_registry_without_tainting_tools_is_trusted():
    registry = CapabilityRegistry(warn=Warnings())
    registry.register(FakeTool("search_docs"), labels())

    assert registry.trust_level == "trusted"
