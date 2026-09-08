"""Complete Hook plugin example with lifecycle-safe registration and unregistration."""

from prediction_paper_bot.plugins.discovery import PluginInitializationContext, PluginSpec


def initialize_plugin(context: PluginInitializationContext) -> PluginSpec:
    registrations = []

    def factory(config, services):
        del config
        manager = services["hooks"]

        def print_order(event, payload):
            print({"event": event, "platform": payload.get("platform")})

        for event in ("after_order", "after_cancel"):
            manager.register(event, print_order)
            registrations.append((manager, event, print_order))
        return {"events": ["after_order", "after_cancel"]}

    def teardown():
        while registrations:
            manager, event, callback = registrations.pop()
            manager.unregister(event, callback)

    return PluginSpec(
        kind="hook",
        name="audit_hook",
        description="打印订单和撤单摘要的完整 Hook 插件示例。",
        origin=str(context.module_path),
        factory=factory,
        configuration=None,
        teardown=teardown,
    )
