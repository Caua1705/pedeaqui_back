"""Conversa com a API.

Tres chamadas, nesta ordem, e o motivo de serem tres:

1. `POST /admin/auth/login` — devolve o token de acesso (vale 12h).
2. `POST /admin/orders/stream-ticket` — devolve um ticket de 30 SEGUNDOS.
3. `GET  /admin/orders/stream?ticket=...` — o stream em si.

O passo 2 parece burocracia e nao e. A rota do stream so aceita ticket na
querystring, porque foi desenhada para o `EventSource` do navegador, que nao
manda cabecalho. Mandar o token de 12h na URL o deixaria no log de acesso do
proxy e no Referer; o ticket vale meio minuto e so serve para abrir stream.
O agente segue a mesma porta que o painel — nao ha uma entrada separada para
ele, e nao deveria haver.

Como a conexao e reaberta a cada 15 minutos (o servidor fecha de proposito),
o passo 2 se repete a cada reconexao. O passo 1 so quando o token expira.
"""

import logging
from collections.abc import Iterator

import requests


logger = logging.getLogger(__name__)

# Tempo para estabelecer conexao e para receber CADA pedaco do stream. O de
# leitura precisa ser maior que o heartbeat da API (20s) e menor que uma
# eternidade: e ele que detecta o cabo arrancado, situacao em que o socket
# fica aberto sem nunca mais entregar byte.
CONNECT_TIMEOUT_SECONDS = 10.0
STREAM_READ_TIMEOUT_SECONDS = 60.0
REQUEST_TIMEOUT_SECONDS = 30.0


class ApiError(Exception):
    """Falha de rede ou resposta inesperada da API."""


class AuthError(ApiError):
    """Credencial recusada.

    Separada porque o tratamento e outro: erro de rede se resolve tentando
    de novo, credencial errada nao. Com token estatico vencido, o agente
    para e diz o que fazer.
    """


class ApiClient:
    def __init__(self, base_url: str, token=None, email=None, password=None, session=None):
        self.base_url = base_url.rstrip("/")
        self.email = email
        self.password = password
        # Token do config.ini, quando houver. Vira `None` assim que a API o
        # recusar, para o proximo `_authorize` refazer o login.
        self._token = token
        self.session = session or requests.Session()

    def print_jobs(self, order_id: str) -> list[dict]:
        """As vias de um pedido, ja formatadas pela API."""
        payload = self._request("GET", f"/admin/orders/{order_id}/print-jobs")
        jobs = payload.get("jobs")
        if not isinstance(jobs, list):
            raise ApiError(f"resposta sem lista de tarefas para o pedido {order_id}")
        return jobs

    def stream_ticket(self) -> str:
        payload = self._request("POST", "/admin/orders/stream-ticket")
        ticket = payload.get("ticket")
        if not ticket:
            raise ApiError("resposta de stream-ticket sem ticket")
        return ticket

    def open_stream(self, last_event_id: str | None = None) -> Iterator[str]:
        """Abre o SSE e devolve as linhas, uma a uma.

        `last_event_id` e o cursor da reconexao: com ele o servidor repete o
        que aconteceu enquanto o agente esteve fora. Sem ele, tudo o que
        aconteceu na queda estaria perdido — e uma queda de 40 segundos no
        almoco e um pedido que nao imprimiu.
        """
        ticket = self.stream_ticket()
        headers = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id

        try:
            response = self.session.get(
                f"{self.base_url}/admin/orders/stream",
                params={"ticket": ticket},
                headers=headers,
                stream=True,
                timeout=(CONNECT_TIMEOUT_SECONDS, STREAM_READ_TIMEOUT_SECONDS),
            )
        except requests.RequestException as exc:
            raise ApiError(f"nao foi possivel abrir o stream: {exc}") from exc

        if response.status_code in (401, 403):
            response.close()
            self._token = None
            raise AuthError(f"stream recusado ({response.status_code})")
        if response.status_code >= 400:
            response.close()
            raise ApiError(f"stream respondeu {response.status_code}")

        return _iter_lines(response)

    def _request(self, method: str, path: str, retry_on_auth: bool = True) -> dict:
        headers = {"Authorization": f"Bearer {self._authorize()}"}
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ApiError(f"{method} {path} falhou: {exc}") from exc

        if response.status_code in (401, 403) and retry_on_auth:
            # Token de 12h vencido durante a noite. Descarta e tenta uma vez
            # com credencial nova; e o caminho normal de um agente que roda
            # como servico por semanas.
            logger.info("credencial recusada em %s, refazendo login", path)
            self._token = None
            return self._request(method, path, retry_on_auth=False)
        if response.status_code in (401, 403):
            raise AuthError(f"{method} {path} recusado ({response.status_code})")
        if response.status_code >= 400:
            raise ApiError(f"{method} {path} respondeu {response.status_code}")

        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"{method} {path} respondeu algo que nao e JSON") from exc

    def _authorize(self) -> str:
        if self._token:
            return self._token
        if not (self.email and self.password):
            raise AuthError(
                "token vencido e nao ha email/password no config.ini para "
                "refazer o login. O token de acesso vale 12 horas."
            )
        self._token = self._login()
        return self._token

    def _login(self) -> str:
        try:
            response = self.session.post(
                f"{self.base_url}/admin/auth/login",
                json={"email": self.email, "password": self.password},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise ApiError(f"login falhou: {exc}") from exc

        if response.status_code in (401, 403):
            raise AuthError("email ou senha invalidos no config.ini")
        if response.status_code >= 400:
            raise ApiError(f"login respondeu {response.status_code}")

        try:
            token = response.json().get("access_token")
        except ValueError as exc:
            raise ApiError("login respondeu algo que nao e JSON") from exc
        if not token:
            raise ApiError("login respondeu sem access_token")

        logger.info("login refeito como %s", self.email)
        return token


def _iter_lines(response) -> Iterator[str]:
    """Linhas do stream, com a conexao fechada no fim aconteca o que acontecer.

    O `finally` importa: sem ele, uma excecao no meio do laco deixaria o
    socket aberto e, depois de algumas reconexoes, o agente estaria segurando
    conexoes mortas contra a API.
    """
    try:
        for line in response.iter_lines(decode_unicode=True):
            # `iter_lines` devolve None para a linha em branco quando o
            # decode esta ligado; o parser espera string.
            yield line if line is not None else ""
    except requests.RequestException as exc:
        raise ApiError(f"stream interrompido: {exc}") from exc
    finally:
        response.close()
