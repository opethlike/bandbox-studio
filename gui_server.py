"""Loopback-only BandBox control panel, using the same controller as MCP.

Run with the installed environment's Python. No device access occurs until a
person presses a control. There is no background polling or automatic retry.
"""
import argparse
import asyncio
import copy
import inspect
import json
from pathlib import Path
import secrets
import types
from typing import Literal, Union, get_args, get_origin, get_type_hints

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

MAX_BODY = 32 * 1024
ACTIONS = frozenset({
    "refresh", "select_preset", "create_preset", "rename_preset", "save_as",
    "save_user_preset", "set_effect_parameter", "set_effect_model",
    "set_effect_active", "read_tool", "set_tool", "set_mixer_volume", "control_looper",
})
EFFECT_ACTIONS = frozenset({"set_effect_parameter", "set_effect_model", "set_effect_active"})
READ_ACTIONS = frozenset({"refresh", "read_tool"})


def _matches_type(value, annotation):
    if annotation is inspect.Parameter.empty:
        return True
    origin = get_origin(annotation)
    if origin in (types.UnionType, Union):
        return any(_matches_type(value, choice) for choice in get_args(annotation))
    if origin is Literal:
        return any(type(value) is type(choice) and value == choice for choice in get_args(annotation))
    if annotation is float:
        return type(value) in (int, float)
    if annotation in (str, int, bool, type(None)):
        return type(value) is annotation
    return True


def _validate_args(method, arguments):
    signature = inspect.signature(method)
    signature.bind(**arguments)
    annotations = get_type_hints(method)
    for key, value in arguments.items():
        if not _matches_type(value, annotations.get(key, signature.parameters[key].annotation)):
            raise ValueError(f"Invalid value type for {key}")
    # The GUI can establish an editable baseline only by explicitly loading a
    # saved preset. It cannot manufacture a user's physical-display confirmation.
    if arguments.get("confirm_saved_settings") is not None:
        if arguments["confirm_saved_settings"] is not False:
            raise ValueError("Load the saved preset before editing")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _numbered(presets):
    return [dict(item, display_number=number) for number, item in enumerate(presets, start=1)]


class LocalOnly:
    """Validate Host even for static files, before a route or JSON parser runs."""
    def __init__(self, app, port):
        self.app = app
        self.hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        pairs = [(k.lower(), v) for k, v in scope.get("headers", [])]
        hosts = [v.decode("latin1") for k, v in pairs if k == b"host"]
        origins = [v.decode("latin1") for k, v in pairs if k == b"origin"]
        sites = [v.decode("latin1") for k, v in pairs if k == b"sec-fetch-site"]
        error = None
        if len(hosts) != 1 or hosts[0] not in self.hosts:
            error = "This panel only accepts its local address"
        elif origins and (len(origins) != 1 or origins[0] != f"http://{hosts[0]}"):
            error = "Cross-origin requests are not allowed"
        elif sites and (len(sites) != 1 or sites[0] not in ("same-origin", "none")):
            error = "Cross-site requests are not allowed"
        if error:
            response = JSONResponse({"ok": False, "error": error}, status_code=403)
            return await response(scope, receive, send)

        async def secured_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"),
                ]
            await send(message)
        await self.app(scope, receive, secured_send)


class Panel:
    def __init__(self, controller, static_root, refresh_reader):
        self.controller = controller
        self.static_root = Path(static_root)
        self.refresh_reader = refresh_reader
        self.token = secrets.token_urlsafe(32)
        self.action_lock = asyncio.Lock()
        self.state = {
            "connected": False, "presets": [], "current": None,
            "editable": False, "dirty": False, "busy": False,
            "view_role": "none", "message": "Connect to read the amp. No settings have changed.",
        }

    def snapshot(self):
        result = copy.deepcopy(self.state)
        valid = self.controller.preview_valid()
        if valid and self.controller.preview:
            info = self.controller.preview["info"]
            current = result.get("current")
            if current and current["preset_id"] == info["preset_id"]:
                result["current"] = copy.deepcopy(info)
                result["editable"] = True
                result["view_role"] = "verified_preview"
            else:
                result["editable"] = False
        else:
            result["editable"] = False
        result["busy"] = self.action_lock.locked()
        if result["current"]:
            found = next((item for item in result["presets"]
                          if item["preset_id"] == result["current"]["preset_id"]), None)
            result["current"]["display_number"] = found["display_number"] if found else None
        return result

    async def bootstrap(self, request):
        return JSONResponse({"ok": True, "app_id": "bandbox-studio", "token": self.token,
                             "catalog": await self.controller.catalog(),
                             "controls": await self.controller.list_controls(),
                             "state": self.snapshot()})

    async def cached_state(self, request):
        return JSONResponse({"ok": True, "state": self.snapshot()})

    async def static(self, request):
        name = request.url.path.lstrip("/") or "index.html"
        # Routes are exact, so no user-controlled traversal reaches FileResponse.
        path = self.static_root / name
        if not path.is_file():
            return JSONResponse({"ok": False, "error": "Panel file not installed"}, status_code=404)
        return FileResponse(path)

    async def _body(self, request):
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError("Use application/json")
        length = request.headers.get("content-length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_BODY):
            raise OverflowError("Request is too large")
        data = bytearray()
        async for part in request.stream():
            data.extend(part)
            if len(data) > MAX_BODY:
                raise OverflowError("Request is too large")
        payload = json.loads(data, object_pairs_hook=_unique_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid number")))
        if not isinstance(payload, dict) or set(payload) - {"action", "args"}:
            raise ValueError("Expected an action and arguments")
        action, arguments = payload.get("action"), payload.get("args", {})
        if not isinstance(action, str) or action not in ACTIONS:
            raise ValueError("Unknown panel action")
        if not isinstance(arguments, dict):
            raise ValueError("Arguments must be an object")
        if action == "refresh":
            if arguments:
                raise ValueError("Refresh takes no arguments")
        else:
            _validate_args(getattr(self.controller, action), arguments)
        return action, arguments

    async def action(self, request):
        token = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(token.encode("utf-8"), self.token.encode("ascii")):
            return JSONResponse({"ok": False, "error": "Reload this panel before using controls",
                                 "state": self.snapshot()}, status_code=403)
        try:
            action, arguments = await self._body(request)
        except OverflowError as exc:
            return JSONResponse({"ok": False, "error": str(exc), "state": self.snapshot()}, status_code=413)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc), "state": self.snapshot()}, status_code=400)
        if self.action_lock.locked():
            return JSONResponse({"ok": False, "error": "An amp operation is already running. Wait for its result.",
                                 "state": self.snapshot()}, status_code=409)
        result = None
        code = 200
        async with self.action_lock:
            try:
                result = await self.perform(action, arguments)
                if result.get("verified") is not True:
                    if action not in READ_ACTIONS:
                        self.controller.preview = None
                        self.state["editable"] = False
                    self.state["message"] = "The amp did not verify this operation. No retry was attempted."
                    code = 409
            except (TypeError, ValueError) as exc:
                code = 400
                error = str(exc)
                if action == "refresh":
                    self.state["connected"] = False
                if action not in READ_ACTIONS:
                    self.controller.preview = None
                self.state["message"] = error
            except (RuntimeError, OSError, TimeoutError) as exc:
                code = 503
                error = str(exc)
                self.controller.preview = None
                self.state.update(connected=False, editable=False, message=error)
        # Build after releasing the action lock so this response isn't 'busy'.
        if code != 200:
            response = {"ok": False, "error": self.state["message"], "state": self.snapshot()}
            if result is not None:
                response["result"] = result
            return JSONResponse(response, status_code=code)
        return JSONResponse({"ok": True, "result": result, "state": self.snapshot()})

    async def perform(self, action, arguments):
        if action == "refresh":
            current, inventory = await self.refresh_reader()
            if not current.get("current_preset_read_verified"):
                raise ValueError("Could not verify the selected preset")
            info = current["preset_info"]
            old = self.state["current"]
            keep_edits = bool(self.state["dirty"] and old is not None
                              and old["preset_id"] == info["preset_id"])
            if self.controller.preview and self.controller.preview["info"]["preset_id"] != info["preset_id"]:
                self.controller.preview = None
            self.state.update(connected=True, presets=_numbered(inventory), current=old if keep_edits else info,
                              editable=False, view_role="saved", message="Saved preset read. Load it to edit its sound.")
            if old is not None and old["preset_id"] != info["preset_id"]:
                self.state["dirty"] = False
            if self.controller.preview_valid():
                self.state["message"] = "Amp connected. Your verified preview is ready to edit."
            elif keep_edits:
                self.state.update(view_role="stale_preview", message="Your last verified edits are still shown, but their current state is unverified. Reloading replaces any unsaved sound changes.")
            return {"verified": True, "setting_writes": 0}

        # Prevent expired/stale GUI edits even when a client sends a forged
        # confirmation flag. Controls also re-checks the selected preset.
        if action in EFFECT_ACTIONS or action in ("save_user_preset", "save_as"):
            if not self.snapshot()["editable"]:
                raise ValueError("Load this preset before editing or saving. Saved reads do not verify unsaved changes.")
        result = await getattr(self.controller, action)(**arguments)
        if result.get("verified") is not True:
            return result
        self.state["connected"] = True
        if result.get("inventory") is not None:
            self.state["presets"] = _numbered(result["inventory"])
        if action == "select_preset":
            self.state.update(current=copy.deepcopy(result["preset"]), dirty=False,
                              editable=True, view_role="verified_preview", message="Preset loaded. Ready to edit.")
        elif action in EFFECT_ACTIONS:
            if self.controller.preview_valid():
                self.state["current"] = copy.deepcopy(self.controller.preview["info"])
            self.state.update(dirty=True, message="Sound updated. Save to keep these changes.")
        elif action == "save_user_preset":
            self.state.update(current=copy.deepcopy(result["preset"]), dirty=False,
                              message="Preset saved and verified.")
        elif action in ("create_preset", "save_as"):
            self.state["message"] = "New preset saved and verified. Select it to play or edit it."
        elif action == "rename_preset":
            if self.state["current"] and self.state["current"]["preset_id"] == arguments["preset_id"]:
                self.state["current"]["name"] = arguments["name"]
            self.state["message"] = "Preset renamed and verified."
        else:
            self.state["message"] = "Amp response verified."
        return result


def create_app(controller=None, *, port=8765, static_root=None, refresh_reader=None):
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    if controller is None:
        import server
        from controls import Controls
        from drive_control import device_lease
        server.RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
        controller = Controls(server.ROOT, server.RUNTIME, server.lock, server._run_probe,
                              server.active_identifier, invalidate_drive=server.pending_changes.invalidate_all,
                              preview_max_age=None, channel=server.CHANNEL)
        server.controls = controller
        async def refresh_reader():
            async with server.lock:
                with device_lease(server.RUNTIME):
                    return await controller.current(), await controller.inventory()
    if refresh_reader is None:
        raise ValueError("An injected controller requires a refresh_reader")
    panel = Panel(controller, static_root or Path(__file__).with_name("gui"), refresh_reader)
    app = Starlette(routes=[
        Route("/api/bootstrap", panel.bootstrap, methods=["GET"]),
        Route("/api/state", panel.cached_state, methods=["GET"]),
        Route("/api/action", panel.action, methods=["POST"]),
        *(Route(path, panel.static, methods=["GET"]) for path in ("/", "/index.html", "/app.js", "/styles.css")),
    ])
    app.state.panel = panel
    app.add_middleware(LocalOnly, port=port)
    return app


def main():
    parser = argparse.ArgumentParser(description="Open the local BandBox control panel")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(port=args.port), host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
