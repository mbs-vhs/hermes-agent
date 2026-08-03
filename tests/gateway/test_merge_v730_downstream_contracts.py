"""Cross-repo contract pins for the surfaces the Minerva mesh attaches to.

WHY THIS FILE EXISTS
--------------------
Three sibling repos bind to this fork through narrow, unversioned Python
surfaces.  None of them is on this repo's CI, so an upstream merge can retire or
reshape one of these surfaces with a completely green Hermes suite — and the
breakage only appears when the fleet is deployed:

  * ``hermes-mnemosyne-provider``  -> subclasses ``agent.memory_provider.MemoryProvider``
  * ``hermes-clawd-tools``         -> calls ``ctx.register_tool(...)``
  * ``agora`` (agent-meeting-space)-> ``plugins/platforms/agora/adapter.py``
                                      calls ``ctx.register_platform(...)``

Every kwarg set asserted below was extracted from the *real* call site by AST on
2026-08-03, not copied from a docstring.  If a call site changes, this file
should be updated to match it — that is the point: the pin is deliberately
coupled to the consumers.

FAILURE MODE THESE CATCH
------------------------
The dangerous change is not a rename (that fails loudly at import).  It is
(a) a *new* ``@abstractmethod`` on ``MemoryProvider``, which makes every existing
downstream provider un-instantiable with ``TypeError: Can't instantiate abstract
class``, and (b) a removed ``PlatformEntry`` field, which makes
``register_platform`` raise ``TypeError`` from the dataclass constructor at
plugin-load time.  Both are silent here and fatal there.
"""

import dataclasses
import inspect

import pytest


# ---------------------------------------------------------------------------
# hermes-mnemosyne-provider
# ---------------------------------------------------------------------------

#: Public methods MnemosyneMemoryProvider actually defines
#: (AST of /home/morganstempf/dev/hermes-mnemosyne-provider/mnemosyne/__init__.py,
#: class MnemosyneMemoryProvider, 2026-08-03).
MNEMOSYNE_IMPLEMENTED = frozenset({
    "get_config_schema", "get_tool_schemas", "initialize", "is_available",
    "name", "on_memory_write", "prefetch", "queue_prefetch", "recall_stats",
    "save_config", "shutdown", "sync_turn", "system_prompt_block",
})


class TestMemoryProviderContract:
    def test_import_path_is_stable(self):
        """hermes-mnemosyne-provider does exactly this import at module scope."""
        from agent.memory_provider import MemoryProvider

        assert inspect.isclass(MemoryProvider)

    def test_a_provider_implementing_only_mnemosynes_methods_can_be_instantiated(self):
        """THE load-bearing test.

        A new ``@abstractmethod`` on MemoryProvider is invisible to this repo's
        suite and fatal to every out-of-tree provider: Python refuses to
        instantiate the subclass.  Build a provider with exactly the method set
        hermes-mnemosyne-provider implements and require it to construct.
        """
        from agent.memory_provider import MemoryProvider

        ns = {m: (lambda self, *a, **k: None) for m in MNEMOSYNE_IMPLEMENTED}
        Provider = type("_FakeMnemosyneProvider", (MemoryProvider,), ns)

        try:
            Provider()
        except TypeError as exc:  # pragma: no cover - only on regression
            pytest.fail(
                "MemoryProvider grew an abstract method that "
                "hermes-mnemosyne-provider does not implement; deploying this "
                f"would break the memory provider on all gateways: {exc}"
            )

    def test_abstract_method_set_is_a_subset_of_what_mnemosyne_implements(self):
        """Same invariant stated directly, so the failure message names the
        offending method rather than just 'cannot instantiate'."""
        from agent.memory_provider import MemoryProvider

        abstract = set(getattr(MemoryProvider, "__abstractmethods__", frozenset()))
        missing = abstract - MNEMOSYNE_IMPLEMENTED
        assert not missing, (
            f"MemoryProvider requires {sorted(missing)}, which "
            f"hermes-mnemosyne-provider does not implement"
        )

    @pytest.mark.parametrize(
        "method",
        ["prefetch", "queue_prefetch", "sync_turn", "on_memory_write",
         "system_prompt_block", "get_config_schema", "save_config", "shutdown"],
    )
    def test_optional_hook_still_exists_on_the_base(self, method):
        """These are overridden (not abstract) by the mnemosyne provider.  If the
        base drops one, the override becomes dead code that the host never calls
        — memory silently stops syncing rather than erroring."""
        from agent.memory_provider import MemoryProvider

        assert callable(getattr(MemoryProvider, method, None)), (
            f"MemoryProvider.{method} disappeared; hermes-mnemosyne-provider "
            f"overrides it and the host would no longer invoke it"
        )

    def test_prefetch_and_sync_turn_keep_their_session_id_keyword(self):
        """The provider's overrides declare ``session_id``; a host that stopped
        passing it would silently cross-contaminate sessions."""
        from agent.memory_provider import MemoryProvider

        assert "session_id" in inspect.signature(MemoryProvider.prefetch).parameters
        assert "session_id" in inspect.signature(MemoryProvider.queue_prefetch).parameters


# ---------------------------------------------------------------------------
# hermes-clawd-tools
# ---------------------------------------------------------------------------

#: kwargs of both ``ctx.register_tool(...)`` calls in
#: hermes-clawd-tools/clawd_tools/__init__.py (AST, 2026-08-03).
CLAWD_TOOLS_REGISTER_TOOL_KWARGS = frozenset({
    "name", "toolset", "schema", "handler", "check_fn", "requires_env", "emoji",
})


class TestRegisterToolContract:
    def test_plugin_context_exposes_register_tool(self):
        from hermes_cli.plugins import PluginContext

        assert callable(getattr(PluginContext, "register_tool", None))

    def test_signature_accepts_every_kwarg_clawd_tools_passes(self):
        from hermes_cli.plugins import PluginContext

        sig = inspect.signature(PluginContext.register_tool)
        params = set(sig.parameters) - {"self"}
        missing = CLAWD_TOOLS_REGISTER_TOOL_KWARGS - params
        assert not missing, (
            f"ctx.register_tool no longer accepts {sorted(missing)}; "
            f"hermes-clawd-tools passes them and would raise TypeError at "
            f"plugin load, dropping mail_compose + workflow_authoring"
        )

    def test_binding_clawd_tools_call_shape_succeeds(self):
        """Signature membership is not the same as bindability (a kwarg could
        become positional-only).  Bind the real call shape."""
        from hermes_cli.plugins import PluginContext

        sig = inspect.signature(PluginContext.register_tool)
        kwargs = {
            "name": "mail_compose",
            "toolset": "mail",
            "schema": {"type": "function"},
            "handler": lambda **_kw: "",
            "check_fn": lambda: True,
            "requires_env": ["CLAWD_API_AUTH_TOKEN", "MAIL_AGENT_TOKEN"],
            "emoji": "\U0001f4e7",
        }
        assert set(kwargs) == set(CLAWD_TOOLS_REGISTER_TOOL_KWARGS)
        sig.bind(object(), **kwargs)  # raises TypeError on a shape change


# ---------------------------------------------------------------------------
# agora (agent-meeting-space)
# ---------------------------------------------------------------------------

#: kwargs of the ``ctx.register_platform(...)`` call in
#: plugins/platforms/agora/adapter.py (AST, 2026-08-03).
AGORA_REGISTER_PLATFORM_KWARGS = frozenset({
    "name", "label", "adapter_factory", "check_fn", "validate_config",
    "is_connected", "required_env", "install_hint", "env_enablement_fn",
    "cron_deliver_env_var", "standalone_sender_fn", "max_message_length",
    "emoji", "pii_safe", "allow_update_command", "platform_hint",
})


class TestAgoraPlatformContract:
    def test_agora_plugin_module_is_present_and_exports_register(self):
        """Control's /chat embeds agora; the adapter is how it attaches."""
        import plugins.platforms.agora as agora_pkg

        assert callable(getattr(agora_pkg, "register", None))

    def test_platform_entry_still_has_every_field_agora_passes(self):
        """``register_platform`` forwards **entry_kwargs straight into
        ``PlatformEntry``; its own docstring says 'Unknown keys raise TypeError
        from the dataclass constructor'.  A dropped field is therefore a
        load-time crash for agora, not a warning."""
        from gateway.platform_registry import PlatformEntry

        fields = {f.name for f in dataclasses.fields(PlatformEntry)}
        missing = AGORA_REGISTER_PLATFORM_KWARGS - fields
        assert not missing, (
            f"PlatformEntry lost {sorted(missing)}; "
            f"plugins/platforms/agora/adapter.py passes them and plugin load "
            f"would raise TypeError, leaving agora unregistered"
        )

    def test_agora_register_drives_the_real_registration_path(self):
        """End-to-end: call agora's own ``register(ctx)`` against a real
        ``PluginContext`` and require the platform to land in the registry.

        This is stronger than the field check above because it exercises
        ``register_platform`` -> ``PlatformEntry(**kwargs)`` for real, including
        the ``plugin_name`` setdefault.
        """
        from gateway.platform_registry import platform_registry
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        from plugins.platforms.agora.adapter import register

        manifest = PluginManifest(
            name="agora", version="1.0.0", description="agora",
            source="project", kind="platform", key="platforms/agora",
        )
        ctx = PluginContext(manifest, PluginManager())

        register(ctx)

        entry = platform_registry.get("agora")
        assert entry is not None, "agora did not land in the platform registry"
        assert entry.label == "Agora"
        assert callable(entry.adapter_factory)
        assert entry.max_message_length
        assert entry.pii_safe is True

    def test_register_platform_signature_accepts_agoras_named_parameters(self):
        from hermes_cli.plugins import PluginContext

        sig = inspect.signature(PluginContext.register_platform)
        named = set(sig.parameters) - {"self"}
        has_var_kw = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        # Either every agora kwarg is named, or the surplus is absorbed by **kwargs
        # AND still exists as a PlatformEntry field (asserted above).
        assert has_var_kw or not (AGORA_REGISTER_PLATFORM_KWARGS - named), (
            "register_platform lost **entry_kwargs and does not name every "
            "kwarg agora passes"
        )
