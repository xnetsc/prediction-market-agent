from __future__ import annotations

from pathlib import Path
from typing import Any

from mangum import Mangum
from fastapi import HTTPException, Request

from ..core.config import ApplicationConfigStore, Config
from .dashboard import create_app
from .engine import TradingEngine


def cloud_config() -> Config:
    persistent_locations = (Path("/data"), Path("/mnt/prediction-market-agent"))
    workspace = next(
        (path for path in persistent_locations if path.is_dir()),
        Path("/tmp/prediction-market-agent"),
    )
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = workspace / "config/application.json"
    store = ApplicationConfigStore(config_path)
    if not store.path.exists():
        store.save({"working_directory": str(workspace)})
    return Config.load(config_path)


application = create_app(cloud_config())
_http_handler = Mangum(application, lifespan="off")


def run_once_event() -> dict[str, Any]:
    engine = TradingEngine(cloud_config())
    try:
        return engine.run_once()
    finally:
        engine.close()


@application.post("/invoke")
def aliyun_runtime_invoke(request: Request) -> dict[str, Any]:
    """Handle Function Compute event-source calls on the reserved runtime path."""
    if request.headers.get("x-fc-control-path") != "/invoke":
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True, "result": run_once_event()}


def lambda_handler(event: dict[str, Any], context: Any) -> Any:
    """Serve API Gateway requests or execute one robot cycle for scheduled events."""
    if "requestContext" in event:
        return _http_handler(event, context)
    return {"ok": True, "result": run_once_event()}


def aliyun_event_handler(event: Any, context: Any) -> dict[str, Any]:
    del event, context
    return {"ok": True, "result": run_once_event()}
