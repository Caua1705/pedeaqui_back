"""Reprecifica um JSON exportado pela bancada com a formula CORRIGIDA.

Existe por um motivo pratico: a cota e de 5 sessoes de voz por cliente por
dia, entao repetir a bateria so para arrumar a conta e caro. Os contadores da
exportacao antiga estao certos — quem estava errado era a formula. Isto le o
que voce ja tem e recalcula.

    python recalcular_custo.py medicao.json

DUAS COISAS QUE ELE FAZ E A BANCADA VELHA NAO FAZIA

1. Separa o cache por TIPO. A exportacao antiga so tem `cached_tokens`
   (o total), porque a bancada nao capturava `cached_tokens_details`. Aqui o
   total e repartido pela mesma queda que a bancada nova usa: texto primeiro,
   ate o limite de `input_text_tokens`, e o resto no audio. E ESTIMATIVA, e
   sai marcada com `~` na tabela.

2. Cobra o lado TEXTO. A bancada tinha `precoTextoVoz=0`, entao todo
   `custo_usd` exportado antes de 24/08/2026 esta subestimado.

As tarifas sao as do `gpt-realtime-mini`. Confira contra a fatura antes de
tratar qualquer numero daqui como dinheiro.
"""

import json
import sys

# US$ por 1M de token.
AUDIO_ENTRADA = 10.00
AUDIO_SAIDA = 20.00
CACHE_AUDIO = 0.30
TEXTO_ENTRADA = 0.60
TEXTO_SAIDA = 2.40
CACHE_TEXTO = 0.06


def repartir_cache(t: dict) -> tuple[int, int, bool]:
    """(cache de audio, cache de texto, e se foi estimado)."""
    detalhe = t.get("cached_tokens_details")
    if detalhe:
        return detalhe.get("audio_tokens", 0), detalhe.get("text_tokens", 0), False
    total = t.get("cached_tokens") or 0
    no_texto = min(total, t.get("input_text_tokens") or 0)
    return total - no_texto, no_texto, total > 0


def custo(t: dict) -> tuple[float, bool]:
    cache_audio, cache_texto, estimado = repartir_cache(t)
    audio_novo = max(0, (t.get("input_audio_tokens") or 0) - cache_audio)
    texto_novo = max(0, (t.get("input_text_tokens") or 0) - cache_texto)
    return (
        audio_novo * AUDIO_ENTRADA
        + cache_audio * CACHE_AUDIO
        + texto_novo * TEXTO_ENTRADA
        + cache_texto * CACHE_TEXTO
        + (t.get("output_audio_tokens") or 0) * AUDIO_SAIDA
        + (t.get("output_text_tokens") or 0) * TEXTO_SAIDA
    ) / 1e6, estimado


def main(caminho: str) -> None:
    pacote = json.load(open(caminho, encoding="utf-8"))
    turnos = [t for t in pacote.get("turnos", []) if t.get("canal") == "voz"]
    if not turnos:
        print("nenhum turno de voz neste arquivo")
        return

    print(f"{'#':>3} {'antes':>9} {'depois':>9} {'x':>6}  cache(audio/texto)   entrada")
    soma_antes = soma_depois = 0.0
    for i, turno in enumerate(turnos, 1):
        antes = turno.get("custo_usd") or 0.0
        depois, estimado = custo(turno.get("tokens") or {})
        cache_audio, cache_texto, _ = repartir_cache(turno.get("tokens") or {})
        soma_antes += antes
        soma_depois += depois
        vezes = f"{depois / antes:.1f}x" if antes else "  —"
        marca = "~" if estimado else " "
        print(
            f"{i:>3} {antes:>9.5f} {depois:>9.5f} {vezes:>6}  "
            f"{marca}{cache_audio:>6}/{cache_texto:<6}  {(turno.get('entrada') or '')[:34]}"
        )
    print(f"{'':>3} {soma_antes:>9.5f} {soma_depois:>9.5f}   TOTAL")
    print("\n~ = cache repartido por estimativa (exportacao sem cached_tokens_details)")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("uso: python recalcular_custo.py <medicao.json>")
    main(sys.argv[1])
