from __future__ import annotations

import re

# A segunda letra do prefixo (ex.: o "F" em "SF-22-") pode variar; quem define o EPSG
# é apenas o número (22 ou 23), então o padrão aceita qualquer letra nessa posição.
GRID_PREFIX_PATTERN = re.compile(r"^S[A-Z]-(22|23)-")
GRID_NUMBER_SRS = {
    "22": "EPSG:31982",
    "23": "EPSG:31983",
}


def infer_srs_from_name(name: str) -> str | None:
    match = GRID_PREFIX_PATTERN.match(name.upper())
    if not match:
        return None
    return GRID_NUMBER_SRS[match.group(1)]
