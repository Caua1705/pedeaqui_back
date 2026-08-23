from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.schemas.common_schema import BaseResponse


class CreateErrorReportRequest(BaseModel):
    """O relato como o painel o envia.

    **So o que o lojista sabe.** Restaurante, filial e usuario NAO estao aqui
    e nao podem estar: os tres saem do token. `extra="forbid"` fecha a porta
    de propria — um corpo que mandasse `branch_id` seria recusado no schema,
    antes de qualquer service ter a chance de obedece-lo.
    """

    model_config = ConfigDict(extra="forbid")

    # O que ele estava fazendo. E o unico campo obrigatorio: um relato sem
    # historia e um log sem pergunta.
    description: str = Field(min_length=1, max_length=4000)

    # O que a tela capturou — traceback, corpo da resposta, mensagem do
    # console. Opcional porque o erro que nao gera log nenhum ("o botao nao
    # faz nada") e justamente o mais dificil de reproduzir sem o relato.
    error_log: str | None = Field(default=None, max_length=20000)

    # Onde ele estava. Texto livre e nao enum: uma lista fechada de telas
    # envelheceria a cada tela nova do painel, e a tela que faltasse viraria
    # um relato recusado.
    screen: str | None = Field(default=None, max_length=200)

    # O pedido, quando o erro tem um. E a alternativa ESTRUTURADA a escrever
    # "o pedido do Joao, telefone 91 9..." na descricao — e a razao de a
    # redacao poder deixar o texto livre em paz.
    order_number: int | None = Field(default=None, ge=1)

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        # `min_length=1` conferiu o valor CRU: "   " tem tres caracteres e
        # passa. Sem esta linha o relato em branco chegava ao banco e batia no
        # CHECK, voltando como erro de servidor em vez de erro de campo.
        texto = value.strip()
        if not texto:
            raise ValueError("description nao pode ser so espacos")
        return texto

    @field_validator("error_log", "screen")
    @classmethod
    def normalize_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class ErrorReportResponse(BaseResponse):
    """O comprovante. E o numero que o lojista repete no WhatsApp.

    Devolve o id e a hora, e mais nada — nem o texto de volta. Ecoar o relato
    ja redigido faria a tela mostrar `[redigido]` no lugar do que a pessoa
    acabou de digitar, e ela reescreveria achando que perdeu.
    """

    id: UUID
    created_at: datetime
