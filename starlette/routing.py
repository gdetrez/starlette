import asyncio
import inspect
import re
import traceback
import typing
from enum import Enum

from starlette.concurrency import run_in_threadpool
from starlette.convertors import CONVERTOR_TYPES, Convertor
from starlette.datastructures import URL, Headers, URLPath
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket, WebSocketClose


class NoMatchFound(Exception):
    """
    Raised by `.url_for(name, **path_params)` and `.url_path_for(name, **path_params)`
    if no matching route exists.
    """


class Match(Enum):
    NONE = 0
    PARTIAL = 1
    FULL = 2


def request_response(func: typing.Callable) -> ASGIApp:
    """
    Takes a function or coroutine `func(request) -> response`,
    and returns an ASGI application.
    """
    is_coroutine = asyncio.iscoroutinefunction(func)

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive=receive, send=send)
        if is_coroutine:
            response = await func(request)
        else:
            response = await run_in_threadpool(func, request)
        await response(scope, receive, send)

    return app


def websocket_session(func: typing.Callable) -> ASGIApp:
    """
    Takes a coroutine `func(session)`, and returns an ASGI application.
    """
    # assert asyncio.iscoroutinefunction(func), "WebSocket endpoints must be async"

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        session = WebSocket(scope, receive=receive, send=send)
        await func(session)

    return app


def get_name(endpoint: typing.Callable) -> str:
    if inspect.isfunction(endpoint) or inspect.isclass(endpoint):
        return endpoint.__name__
    return endpoint.__class__.__name__


def replace_params(
    path: str,
    param_convertors: typing.Dict[str, Convertor],
    path_params: typing.Dict[str, str],
) -> typing.Tuple[str, dict]:
    for key, value in list(path_params.items()):
        if "{" + key + "}" in path:
            convertor = param_convertors[key]
            value = convertor.to_string(value)
            path = path.replace("{" + key + "}", value)
            path_params.pop(key)
    return path, path_params


# Match parameters in URL paths, eg. '{param}', and '{param:int}'
PARAM_REGEX = re.compile("{([a-zA-Z_][a-zA-Z0-9_]*)(:[a-zA-Z_][a-zA-Z0-9_]*)?}")


def compile_path(
    path: str,
) -> typing.Tuple[typing.Pattern, str, typing.Dict[str, Convertor]]:
    """
    Given a path string, like: "/{username:str}", return a three-tuple
    of (regex, format, {param_name:convertor}).

    regex:      "/(?P<username>[^/]+)"
    format:     "/{username}"
    convertors: {"username": StringConvertor()}
    """
    path_regex = "^"
    path_format = ""

    idx = 0
    param_convertors = {}
    for match in PARAM_REGEX.finditer(path):
        param_name, convertor_type = match.groups("str")
        convertor_type = convertor_type.lstrip(":")
        assert (
            convertor_type in CONVERTOR_TYPES
        ), f"Unknown path convertor '{convertor_type}'"
        convertor = CONVERTOR_TYPES[convertor_type]

        path_regex += path[idx : match.start()]
        path_regex += f"(?P<{param_name}>{convertor.regex})"

        path_format += path[idx : match.start()]
        path_format += "{%s}" % param_name

        param_convertors[param_name] = convertor

        idx = match.end()

    path_regex += path[idx:] + "$"
    path_format += path[idx:]

    return re.compile(path_regex), path_format, param_convertors


class BaseRoute:
    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        raise NotImplementedError()  # pragma: no cover

    def url_path_for(self, name: str, **path_params: str) -> URLPath:
        raise NotImplementedError()  # pragma: no cover

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise NotImplementedError()  # pragma: no cover


class Route(BaseRoute):
    def __init__(
        self,
        path: str,
        endpoint: typing.Callable,
        *,
        methods: typing.List[str] = None,
        name: str = None,
        include_in_schema: bool = True,
    ) -> None:
        assert path.startswith("/"), "Routed paths must start with '/'"
        self.path = path
        self.endpoint = endpoint
        self.name = get_name(endpoint) if name is None else name
        self.include_in_schema = include_in_schema

        if inspect.isfunction(endpoint) or inspect.ismethod(endpoint):
            # Endpoint is function or method. Treat it as `func(request) -> response`.
            self.app = request_response(endpoint)
            if methods is None:
                methods = ["GET"]
        else:
            # Endpoint is a class. Treat it as ASGI.
            self.app = endpoint

        if methods is None:
            self.methods = None
        else:
            self.methods = set(method.upper() for method in methods)
            if "GET" in self.methods:
                self.methods.add("HEAD")

        self.path_regex, self.path_format, self.param_convertors = compile_path(path)

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] == "http":
            match = self.path_regex.match(scope["path"])
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                child_scope = {"endpoint": self.endpoint, "path_params": path_params}
                if self.methods and scope["method"] not in self.methods:
                    return Match.PARTIAL, child_scope
                else:
                    return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, **path_params: str) -> URLPath:
        seen_params = set(path_params.keys())
        expected_params = set(self.param_convertors.keys())

        if name != self.name or seen_params != expected_params:
            raise NoMatchFound()

        path, remaining_params = replace_params(
            self.path_format, self.param_convertors, path_params
        )
        assert not remaining_params
        return URLPath(path=path, protocol="http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.methods and scope["method"] not in self.methods:
            if "app" in scope:
                raise HTTPException(status_code=405)
            else:
                response = PlainTextResponse("Method Not Allowed", status_code=405)
            await response(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, Route)
            and self.path == other.path
            and self.endpoint == other.endpoint
            and self.methods == other.methods
        )


class WebSocketRoute(BaseRoute):
    def __init__(
        self, path: str, endpoint: typing.Callable, *, name: str = None
    ) -> None:
        assert path.startswith("/"), "Routed paths must start with '/'"
        self.path = path
        self.endpoint = endpoint
        self.name = get_name(endpoint) if name is None else name

        if inspect.isfunction(endpoint) or inspect.ismethod(endpoint):
            # Endpoint is function or method. Treat it as `func(websocket)`.
            self.app = websocket_session(endpoint)
        else:
            # Endpoint is a class. Treat it as ASGI.
            self.app = endpoint

        self.path_regex, self.path_format, self.param_convertors = compile_path(path)

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] == "websocket":
            match = self.path_regex.match(scope["path"])
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                child_scope = {"endpoint": self.endpoint, "path_params": path_params}
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, **path_params: str) -> URLPath:
        seen_params = set(path_params.keys())
        expected_params = set(self.param_convertors.keys())

        if name != self.name or seen_params != expected_params:
            raise NoMatchFound()

        path, remaining_params = replace_params(
            self.path_format, self.param_convertors, path_params
        )
        assert not remaining_params
        return URLPath(path=path, protocol="websocket")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, WebSocketRoute)
            and self.path == other.path
            and self.endpoint == other.endpoint
        )


class Mount(BaseRoute):
    def __init__(
        self,
        path: str,
        app: ASGIApp = None,
        routes: typing.List[BaseRoute] = None,
        name: str = None,
    ) -> None:
        assert path == "" or path.startswith("/"), "Routed paths must start with '/'"
        assert (
            app is not None or routes is not None
        ), "Either 'app=...', or 'routes=' must be specified"
        self.path = path.rstrip("/")
        if app is not None:
            self.app = app  # type: ASGIApp
        else:
            self.app = Router(routes=routes)
        self.name = name
        self.path_regex, self.path_format, self.param_convertors = compile_path(
            self.path + "/{path:path}"
        )

    @property
    def routes(self) -> typing.List[BaseRoute]:
        return getattr(self.app, "routes", None)

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] in ("http", "websocket"):
            path = scope["path"]
            match = self.path_regex.match(path)
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                remaining_path = "/" + matched_params.pop("path")
                matched_path = path[: -len(remaining_path)]
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                root_path = scope.get("root_path", "")
                child_scope = {
                    "path_params": path_params,
                    "app_root_path": scope.get("app_root_path", root_path),
                    "root_path": root_path + matched_path,
                    "path": remaining_path,
                    "endpoint": self.app,
                }
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, **path_params: str) -> URLPath:
        if self.name is not None and name == self.name and "path" in path_params:
            # 'name' matches "<mount_name>".
            path_params["path"] = path_params["path"].lstrip("/")
            path, remaining_params = replace_params(
                self.path_format, self.param_convertors, path_params
            )
            if not remaining_params:
                return URLPath(path=path)
        elif self.name is None or name.startswith(self.name + ":"):
            if self.name is None:
                # No mount name.
                remaining_name = name
            else:
                # 'name' matches "<mount_name>:<child_name>".
                remaining_name = name[len(self.name) + 1 :]
            path_kwarg = path_params.get("path")
            path_params["path"] = ""
            path_prefix, remaining_params = replace_params(
                self.path_format, self.param_convertors, path_params
            )
            if path_kwarg is not None:
                remaining_params["path"] = path_kwarg
            for route in self.routes or []:
                try:
                    url = route.url_path_for(remaining_name, **remaining_params)
                    return URLPath(
                        path=path_prefix.rstrip("/") + str(url), protocol=url.protocol
                    )
                except NoMatchFound:
                    pass
        raise NoMatchFound()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, Mount)
            and self.path == other.path
            and self.app == other.app
        )


class Host(BaseRoute):
    def __init__(self, host: str, app: ASGIApp, name: str = None) -> None:
        self.host = host
        self.app = app
        self.name = name
        self.host_regex, self.host_format, self.param_convertors = compile_path(host)

    @property
    def routes(self) -> typing.List[BaseRoute]:
        return getattr(self.app, "routes", None)

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] in ("http", "websocket"):
            headers = Headers(scope=scope)
            host = headers.get("host", "").split(":")[0]
            match = self.host_regex.match(host)
            if match:
                matched_params = match.groupdict()
                for key, value in matched_params.items():
                    matched_params[key] = self.param_convertors[key].convert(value)
                path_params = dict(scope.get("path_params", {}))
                path_params.update(matched_params)
                child_scope = {"path_params": path_params, "endpoint": self.app}
                return Match.FULL, child_scope
        return Match.NONE, {}

    def url_path_for(self, name: str, **path_params: str) -> URLPath:
        if self.name is not None and name == self.name and "path" in path_params:
            # 'name' matches "<mount_name>".
            path = path_params.pop("path")
            host, remaining_params = replace_params(
                self.host_format, self.param_convertors, path_params
            )
            if not remaining_params:
                return URLPath(path=path, host=host)
        elif self.name is None or name.startswith(self.name + ":"):
            if self.name is None:
                # No mount name.
                remaining_name = name
            else:
                # 'name' matches "<mount_name>:<child_name>".
                remaining_name = name[len(self.name) + 1 :]
            host, remaining_params = replace_params(
                self.host_format, self.param_convertors, path_params
            )
            for route in self.routes or []:
                try:
                    url = route.url_path_for(remaining_name, **remaining_params)
                    return URLPath(path=str(url), protocol=url.protocol, host=host)
                except NoMatchFound:
                    pass
        raise NoMatchFound()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)

    def __eq__(self, other: typing.Any) -> bool:
        return (
            isinstance(other, Host)
            and self.host == other.host
            and self.app == other.app
        )


class Lifespan(BaseRoute):
    def __init__(
        self,
        on_startup: typing.Union[typing.Callable, typing.List[typing.Callable]] = None,
        on_shutdown: typing.Union[typing.Callable, typing.List[typing.Callable]] = None,
    ):
        self.startup_handlers = self.to_list(on_startup)
        self.shutdown_handlers = self.to_list(on_shutdown)

    def to_list(
        self, item: typing.Union[typing.Callable, typing.List[typing.Callable]] = None
    ) -> typing.List[typing.Callable]:
        if item is None:
            return []
        return list(item) if isinstance(item, (list, tuple)) else [item]

    def matches(self, scope: Scope) -> typing.Tuple[Match, Scope]:
        if scope["type"] == "lifespan":
            return Match.FULL, {}
        return Match.NONE, {}

    def add_event_handler(self, event_type: str, func: typing.Callable) -> None:
        assert event_type in ("startup", "shutdown")

        if event_type == "startup":
            self.startup_handlers.append(func)
        else:
            assert event_type == "shutdown"
            self.shutdown_handlers.append(func)

    def on_event(self, event_type: str) -> typing.Callable:
        def decorator(func: typing.Callable) -> typing.Callable:
            self.add_event_handler(event_type, func)
            return func

        return decorator

    async def startup(self) -> None:
        for handler in self.startup_handlers:
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                handler()

    async def shutdown(self) -> None:
        for handler in self.shutdown_handlers:
            if asyncio.iscoroutinefunction(handler):
                await handler()
            else:
                handler()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        message = await receive()
        assert message["type"] == "lifespan.startup"

        try:
            await self.startup()
        except BaseException:
            msg = traceback.format_exc()
            await send({"type": "lifespan.startup.failed", "message": msg})
            raise

        await send({"type": "lifespan.startup.complete"})
        message = await receive()
        assert message["type"] == "lifespan.shutdown"
        await self.shutdown()
        await send({"type": "lifespan.shutdown.complete"})


class Router:
    def __init__(
        self,
        routes: typing.List[BaseRoute] = None,
        redirect_slashes: bool = True,
        default: ASGIApp = None,
        on_startup: typing.List[typing.Callable] = None,
        on_shutdown: typing.List[typing.Callable] = None,
    ) -> None:
        self.routes = [] if routes is None else list(routes)
        self.redirect_slashes = redirect_slashes
        self.default = self.not_found if default is None else default
        self.lifespan = Lifespan(on_startup=on_startup, on_shutdown=on_shutdown)

    def mount(self, path: str, app: ASGIApp, name: str = None) -> None:
        route = Mount(path, app=app, name=name)
        self.routes.append(route)

    def host(self, host: str, app: ASGIApp, name: str = None) -> None:
        route = Host(host, app=app, name=name)
        self.routes.append(route)

    def add_route(
        self,
        path: str,
        endpoint: typing.Callable,
        methods: typing.List[str] = None,
        name: str = None,
        include_in_schema: bool = True,
    ) -> None:
        route = Route(
            path,
            endpoint=endpoint,
            methods=methods,
            name=name,
            include_in_schema=include_in_schema,
        )
        self.routes.append(route)

    def add_websocket_route(
        self, path: str, endpoint: typing.Callable, name: str = None
    ) -> None:
        route = WebSocketRoute(path, endpoint=endpoint, name=name)
        self.routes.append(route)

    def route(
        self,
        path: str,
        methods: typing.List[str] = None,
        name: str = None,
        include_in_schema: bool = True,
    ) -> typing.Callable:
        def decorator(func: typing.Callable) -> typing.Callable:
            self.add_route(
                path,
                func,
                methods=methods,
                name=name,
                include_in_schema=include_in_schema,
            )
            return func

        return decorator

    def websocket_route(self, path: str, name: str = None) -> typing.Callable:
        def decorator(func: typing.Callable) -> typing.Callable:
            self.add_websocket_route(path, func, name=name)
            return func

        return decorator

    async def not_found(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            websocket_close = WebSocketClose()
            await websocket_close(receive, send)
            return

        # If we're running inside a starlette application then raise an
        # exception, so that the configurable exception handler can deal with
        # returning the response. For plain ASGI apps, just return the response.
        if "app" in scope:
            raise HTTPException(status_code=404)
        else:
            response = PlainTextResponse("Not Found", status_code=404)
        await response(scope, receive, send)

    def url_path_for(self, name: str, **path_params: str) -> URLPath:
        for route in self.routes:
            try:
                return route.url_path_for(name, **path_params)
            except NoMatchFound:
                pass
        raise NoMatchFound()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        assert scope["type"] in ("http", "websocket", "lifespan")

        if "router" not in scope:
            scope["router"] = self

        partial = None

        for route in self.routes:
            match, child_scope = route.matches(scope)
            if match == Match.FULL:
                scope.update(child_scope)
                await route(scope, receive, send)
                return
            elif match == Match.PARTIAL and partial is None:
                partial = route
                partial_scope = child_scope

        if partial is not None:
            scope.update(partial_scope)
            await partial(scope, receive, send)
            return

        if scope["type"] == "http" and self.redirect_slashes:
            if scope["path"].endswith("/"):
                redirect_path = scope["path"].rstrip("/")
            else:
                redirect_path = scope["path"] + "/"

            if redirect_path:  # Note that we skip the "/" -> "" case.
                redirect_scope = dict(scope)
                redirect_scope["path"] = redirect_path

                for route in self.routes:
                    match, child_scope = route.matches(redirect_scope)
                    if match != Match.NONE:
                        redirect_url = URL(scope=redirect_scope)
                        response = RedirectResponse(url=str(redirect_url))
                        await response(scope, receive, send)
                        return

        if scope["type"] == "lifespan":
            await self.lifespan(scope, receive, send)
        else:
            await self.default(scope, receive, send)

    def __eq__(self, other: typing.Any) -> bool:
        return isinstance(other, Router) and self.routes == other.routes
