"""Distancia geografica em linha reta.

Existe para uma coisa so: **descartar filial que esta provadamente fora do
raio de entrega sem gastar uma chamada ao Google.**

A propriedade que sustenta esse uso e que a linha reta e SEMPRE menor ou
igual ao caminho dirigido — nao existe rua que encurte a geodesica. Entao:

    haversine > delivery_max_distance_km  =>  a rota tambem esta fora

O contrario NAO vale, e e por isso que este modulo nao serve para calcular
taxa: dois pontos a 3 km em linha reta podem estar a 4 ou a 12 km de carro,
dependendo do rio, da via expressa e da mao da rua. Numero aproximado que
chega ao cliente como preco vira reclamacao no checkout, quando o valor
exato aparecer diferente.

Portanto: **linha reta so ELIMINA, nunca ESTIMA.**
"""

import math


# Raio medio da Terra. O erro do modelo esferico contra o elipsoide fica em
# ~0,5%, e ele nao importa aqui: esta funcao so e usada para comparar com um
# raio de entrega que o lojista digitou em quilometros inteiros.
RAIO_DA_TERRA_KM = 6371.0088


def haversine_km(
    latitude_origem: float,
    longitude_origem: float,
    latitude_destino: float,
    longitude_destino: float,
) -> float:
    """Distancia em linha reta entre dois pontos, em quilometros."""
    lat_origem = math.radians(latitude_origem)
    lat_destino = math.radians(latitude_destino)
    delta_lat = math.radians(latitude_destino - latitude_origem)
    delta_lon = math.radians(longitude_destino - longitude_origem)

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_origem) * math.cos(lat_destino) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * RAIO_DA_TERRA_KM * math.asin(math.sqrt(a))
