"""Agente de impressao da loja.

Roda no computador do restaurante, nao no servidor. Escuta o stream de
pedidos da API, pede as vias ja formatadas quando um pedido e ACEITO e
manda cada uma para a impressora do setor correspondente.

O agente NAO decide nada sobre o conteudo: ele nao quebra linha, nao
alinha, nao escolhe o que entra em cada via e nao sabe o que e um adicional.
Tudo isso vem pronto da API (`src/services/print_layout.py`). O que sobra
aqui e o que so pode ser feito nesta maquina: falar ESC/POS com a impressora
que esta ligada nela.
"""

__version__ = "1.0.0"
