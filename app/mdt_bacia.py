from __future__ import annotations

from pathlib import Path
from typing import Iterable, Literal

from .bacia_vetor import nome_ascii

TILE_EXTENSIONS = (".tif", ".tiff")

# "fast": mosaico direto (primeira folha válida vence); "blend": transição suave
# ponderada pela distância à borda de cada folha nas sobreposições. Os dois usam a
# pipeline padrão de app/mdt_pipeline.py.
MergeMode = Literal["fast", "blend"]


def mdt_bacia_filename(ident: int | str, resolution: float) -> str:
    return f"MDT_BACIA_{nome_ascii(str(ident))}_{resolution:g}m.tif"


def resolve_tile_files(names: Iterable[str], mdt_dir: Path) -> tuple[list[Path], list[str]]:
    existing_by_lower = (
        {p.name.lower(): p for p in mdt_dir.iterdir() if p.is_file()} if mdt_dir.is_dir() else {}
    )

    found: list[Path] = []
    missing: list[str] = []
    for name in names:
        match = None
        for ext in TILE_EXTENSIONS:
            candidate = mdt_dir / f"{name}{ext}"
            if candidate.is_file():
                match = candidate
                break
        if match is None:
            for ext in TILE_EXTENSIONS:
                match = existing_by_lower.get(f"{name}{ext}".lower())
                if match is not None:
                    break
        if match is not None:
            found.append(match)
        else:
            missing.append(name)
    return found, missing
