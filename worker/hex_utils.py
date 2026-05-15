from __future__ import annotations

from collections import defaultdict


HEX_DIRECTIONS = [
    (1, 0),
    (1, -1),
    (0, -1),
    (-1, 0),
    (-1, 1),
    (0, 1),
]


def build_hex_cells(radius: int) -> list[dict]:
    cells: list[dict] = []
    territory_id = 1
    for q in range(-(radius - 1), radius):
        r_min = max(-(radius - 1), -q - (radius - 1))
        r_max = min(radius - 1, -q + (radius - 1))
        for r in range(r_min, r_max + 1):
            cells.append(
                {
                    "territory_id": territory_id,
                    "q": q,
                    "r": r,
                    "owner_id": None,
                    "hp": 1,
                    "is_base": False,
                    "base_owner_id": None,
                }
            )
            territory_id += 1
    return cells


def build_adjacency(cells: list[dict]) -> dict[int, list[int]]:
    by_coord = {(cell["q"], cell["r"]): cell["territory_id"] for cell in cells}
    adjacency: dict[int, list[int]] = {}
    for cell in cells:
        neighbors: list[int] = []
        for dq, dr in HEX_DIRECTIONS:
            neighbor_id = by_coord.get((cell["q"] + dq, cell["r"] + dr))
            if neighbor_id is not None:
                neighbors.append(neighbor_id)
        adjacency[cell["territory_id"]] = sorted(neighbors)
    return adjacency


def cells_by_id(cells: list[dict]) -> dict[int, dict]:
    return {cell["territory_id"]: cell for cell in cells}


def owned_neighbor_targets(cells: list[dict], adjacency: dict[int, list[int]], player_id: int, target_mode: str) -> list[int]:
    by_id = cells_by_id(cells)
    valid: set[int] = set()
    for cell in cells:
        if cell["owner_id"] != player_id:
            continue
        for neighbor_id in adjacency.get(cell["territory_id"], []):
            neighbor = by_id[neighbor_id]
            if target_mode == "neutral" and neighbor["owner_id"] is None:
                valid.add(neighbor_id)
            if target_mode == "enemy" and neighbor["owner_id"] not in {None, player_id}:
                valid.add(neighbor_id)
    return sorted(valid)


def territory_counts(cells: list[dict]) -> dict[int, int]:
    counts: dict[int, int] = defaultdict(int)
    for cell in cells:
        if cell["owner_id"] is not None:
            counts[cell["owner_id"]] += 1
    return dict(counts)


def find_player_base(cells: list[dict], player_id: int) -> dict | None:
    for cell in cells:
        if cell["is_base"] and cell["base_owner_id"] == player_id:
            return cell
    return None
