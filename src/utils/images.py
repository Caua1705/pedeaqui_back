"""Reconhecimento do tipo de imagem pelos bytes do arquivo.

O `content_type` que o navegador manda no multipart e escolhido pelo
cliente e nao vale nada como validacao: quem quiser envia um HTML dizendo
`image/png`. O bucket serve os arquivos publicamente, entao um HTML ou um
SVG aceito aqui viraria script executando no dominio do Storage.

Por isso o tipo sai SEMPRE daqui, dos primeiros bytes, e o `content_type`
do upload e o que esta tabela diz — nao o que o cliente declarou.

SVG fica de fora de proposito: e XML, aceita <script> dentro e nao tem
assinatura binaria para conferir.
"""


# Extensao -> content-type gravado no Storage.
IMAGE_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


def detect_image_extension(content: bytes) -> str | None:
    """Extensao da imagem, ou None se os bytes nao forem de uma das aceitas."""
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    # WEBP e um contêiner RIFF: "RIFF" + 4 bytes de tamanho + "WEBP".
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp"
    return None
