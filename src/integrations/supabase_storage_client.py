"""Envio de arquivo para o Supabase Storage.

So o upload mora aqui. A URL publica de leitura continua sendo montada em
`src/utils/storage.py`, que nao precisa de credencial nenhuma — o bucket e
publico para leitura.

O bucket NAO e publico para escrita, e essa e a razao de a imagem passar
pela API em vez de o painel enviar direto: o navegador precisaria da chave
de servico do Supabase para gravar, e essa chave le e escreve o banco
inteiro. Ela fica aqui, no servidor.
"""

import logging
from time import perf_counter

import httpx


logger = logging.getLogger("uvicorn.error")


class SupabaseStorageError(Exception):
    pass


class SupabaseStorageNotConfiguredError(SupabaseStorageError):
    pass


class SupabaseStorageClient:
    def __init__(
        self,
        base_url: str,
        bucket: str,
        service_key: str | None,
        timeout_seconds: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.bucket = bucket.strip("/")
        self.service_key = service_key
        self.timeout_seconds = timeout_seconds

    def upload(self, object_path: str, content: bytes, content_type: str) -> str:
        """Grava o objeto e devolve o caminho dentro do bucket.

        `x-upsert: true` porque o caminho ja inclui um sufixo aleatorio: o
        upsert nao existe para sobrescrever imagem antiga, e para que um
        retry do painel depois de um timeout nao falhe com "ja existe".
        """
        if not self.service_key:
            raise SupabaseStorageNotConfiguredError(
                "SUPABASE_SERVICE_ROLE_KEY nao configurada"
            )

        url = f"{self.base_url}/storage/v1/object/{self.bucket}/{object_path.lstrip('/')}"
        started_at = perf_counter()
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    url,
                    content=content,
                    headers={
                        "Authorization": f"Bearer {self.service_key}",
                        "Content-Type": content_type,
                        "x-upsert": "true",
                    },
                )
        except httpx.HTTPError as exc:
            logger.warning("[Storage] upload_failed path=%s erro=%s", object_path, exc)
            raise SupabaseStorageError(str(exc)) from exc

        elapsed_ms = int((perf_counter() - started_at) * 1000)
        if response.status_code >= 400:
            # O corpo do erro do Storage e curto e diz o motivo real
            # ("Bucket not found", "invalid signature"); sem ele, todo
            # problema de upload viraria o mesmo 502 mudo no log.
            logger.warning(
                "[Storage] upload_rejected path=%s status=%d corpo=%s elapsed_ms=%d",
                object_path,
                response.status_code,
                response.text[:200],
                elapsed_ms,
            )
            raise SupabaseStorageError(f"storage respondeu {response.status_code}")

        logger.info("[Storage] upload_ok path=%s elapsed_ms=%d", object_path, elapsed_ms)
        return object_path
