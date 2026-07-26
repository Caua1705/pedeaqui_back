import logging

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send


logger = logging.getLogger("uvicorn.error")


class _BodyTooLargeError(BaseException):
    """Sinaliza estouro do limite durante a leitura do corpo.

    Herda de BaseException de proposito: o FastAPI envolve qualquer Exception
    levantada ao ler o corpo em um 400 "error parsing the body", o que
    esconderia o 413. Ela e capturada explicitamente no __call__ abaixo.
    """


class BodySizeLimitMiddleware:
    """Recusa requisicoes com corpo acima do limite configurado.

    Middleware ASGI puro (e nao BaseHTTPMiddleware) para conseguir cortar o
    corpo enquanto ele ainda esta sendo recebido, antes que o FastAPI o
    carregue inteiro em memoria para desserializar o JSON.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self._declared_length(scope) > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _BodyTooLargeError
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _BodyTooLargeError:
            if response_started:
                raise
            await self._reject(scope, receive, guarded_send)

    def _declared_length(self, scope: Scope) -> int:
        raw = Headers(scope=scope).get("content-length")
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        logger.warning(
            "[Body limit] request_rejected path=%s max_bytes=%d",
            scope.get("path", ""),
            self.max_body_bytes,
        )
        response = JSONResponse(
            status_code=413,
            content={"detail": "Corpo da requisição excede o tamanho máximo permitido"},
        )
        await response(scope, receive, send)
