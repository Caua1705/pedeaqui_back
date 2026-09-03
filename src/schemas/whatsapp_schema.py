from pydantic import BaseModel


class WhatsAppWebhookResponse(BaseModel):
    """O que a Meta recebe de volta.

    Curto e sem dado nenhum do restaurante: e resposta para maquina, e
    qualquer coisa a mais seria informacao entregue a quem so tem o endereco
    do webhook — que e publico.

    `reason` existe para o LOG de quem opera, e nao para a Meta decidir nada:
    ela trata 200 como sucesso, seja qual for o corpo.
    """

    status: str
    reason: str | None = None
