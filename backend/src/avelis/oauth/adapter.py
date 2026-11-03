"""FastAPI 与 Authlib 的请求/响应适配。"""

from __future__ import annotations

import json
from collections import defaultdict

from authlib.common.security import generate_token
from authlib.oauth2.rfc6749 import (
    AuthorizationServer,
    InvalidRequestError,
    JsonPayload,
    JsonRequest,
    OAuth2Payload,
    OAuth2Request,
)
from authlib.oauth2.rfc6750 import BearerTokenGenerator
from fastapi import Request
from starlette.datastructures import FormData, QueryParams
from starlette.responses import Response

from .repository import SCOPES, Repository


class RequestPayload(OAuth2Payload):
    def __init__(self, values):
        self._data = dict(values)
        self._datalist = defaultdict(list)
        for key, value in values.multi_items():
            self._datalist[key].append(value)

    @property
    def data(self):
        return self._data

    @property
    def datalist(self):
        return self._datalist


class FastAPIOAuth2Request(OAuth2Request):
    def __init__(self, request: Request, form: FormData | None = None, *, allow_http: bool = False):
        uri = str(request.url)
        # 请求推导模式按部署实际的 HTTP(S) 工作（包括内网 HTTP）。Authlib 构造器
        # 固定要求 HTTPS/loopback，故仅绕过这一步检查，随后仍保留真实 uri。
        checked_uri = str(request.url.replace(scheme="https")) if allow_http and request.url.scheme == "http" else uri
        super().__init__(request.method, checked_uri, headers=request.headers)
        self.uri = uri
        self._args = request.query_params
        self._form = form or FormData()
        self.payload = RequestPayload(QueryParams([*self._args.multi_items(), *self._form.multi_items()]))

    @property
    def args(self):
        return self._args

    @property
    def form(self):
        return self._form

    def reject_duplicate_parameters(self):
        # resource has its own RFC8707 invalid_target policy in grant validation.
        for name, values in self.payload.datalist.items():
            if name != "resource" and len(values) > 1:
                raise InvalidRequestError(
                    f"Multiple '{name}' parameters are not accepted.",
                    state=self.payload.state,
                )


class RequestJsonPayload(JsonPayload):
    def __init__(self, data):
        self._data = data

    @property
    def data(self):
        return self._data


class FastAPIJsonRequest(JsonRequest):
    def __init__(self, request: Request, body):
        super().__init__(request.method, str(request.url), request.headers)
        self.payload = RequestJsonPayload(body)


class OAuthResponse(Response):
    """RFC9207's generic extension expects Flask-compatible .location access."""

    @property
    def location(self):
        return self.headers.get("location")

    @location.setter
    def location(self, value):
        self.headers["location"] = value


class FastAPIAuthorizationServer(AuthorizationServer):
    def __init__(self, repository: Repository, settings):
        super().__init__(scopes_supported=list(SCOPES))
        self.repository = repository
        self.settings = settings
        self.register_token_generator(
            "default",
            BearerTokenGenerator(
                lambda **_: generate_token(48),
                lambda **_: generate_token(64),
                lambda _client, _grant_type: settings.mcp_access_token_seconds,
            ),
        )

    def query_client(self, client_id):
        client = self.repository.get_client(client_id, lock=True)
        return client if client and client.disabled_at is None else None

    def save_token(self, token, request):
        self.repository.save_token(token, request, self.settings)
        token["resource"] = request.payload.data["resource"]

    def create_oauth2_request(self, request):
        # FastAPI has already awaited the body before entering synchronous core.
        return request

    def create_json_request(self, request):
        return request

    def handle_response(self, status, body, headers):
        payload = json.dumps(body) if isinstance(body, dict) else body
        return OAuthResponse(payload, status_code=status, headers=dict(headers))

    def send_signal(self, name, *args, **kwargs):
        pass
