"""Optional network-path descriptions, provided and resolved by each plugin."""
from dataclasses import dataclass, field
from collections.abc import Callable
from urllib.request import getproxies
from urllib.parse import urlsplit
from .config_io import resolve_plugin_proxy


@dataclass(frozen=True)
class DiagnosticNetworkRoute:
    label: str
    proxy: str = field(default="", repr=False)
    no_proxy: str = ""

    def __post_init__(self):
        if not isinstance(self.label,str) or not self.label.strip():
            raise ValueError("Network route needs a label")
        if self.proxy:
            value = urlsplit(self.proxy)
            if value.scheme not in {"http","https"} or not value.hostname or value.query or value.fragment:
                raise ValueError("Invalid diagnostic proxy")
            value.port


def configured_proxy_route(
    load: Callable,
    field_name: str,
    *,
    default=None,
    resolver: Callable[[str], dict[str, str]] | None = None,
):
    """Opt-in helper: the plugin supplies its private field name and storage reader."""
    value = load().get(field_name, default)
    if value is None or not str(value).strip():
        raise ValueError("Plugin proxy configuration is incomplete")
    if resolver is not None:
        settings = resolver(str(value))
        return (
            DiagnosticNetworkRoute(
                "当前生效的 HTTP 路径（不携带业务凭据）",
                settings["proxy"],
                settings["no_proxy"],
            ),
        )
    return (
        DiagnosticNetworkRoute(
            "配置的 HTTP 路径（不携带业务凭据）",
            resolve_plugin_proxy(str(value)),
            getproxies().get("no", ""),
        ),
    )
