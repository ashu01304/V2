"""Generate and safely maintain the rolling contract expression universe."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

from market_data.config import DATABASE_PATH, DATA_DIR


DEFAULT_CONFIG_PATH = DATA_DIR / "contract_universe_config.json"
DEFAULT_EXPIRY_PATH = DATA_DIR / "expiry_dates.csv"
CONTRACT_PATTERN = re.compile(r"^[FGHJKMNQUVXZ]\d{2}$")
MONTH_NUMBERS = {
    "F": 1, "G": 2, "H": 3, "J": 4, "K": 5, "M": 6,
    "N": 7, "Q": 8, "U": 9, "V": 10, "X": 11, "Z": 12,
}


@dataclass(frozen=True)
class UniverseConfig:
    symbol: str
    nearby_months: int
    deferred_months: tuple[str, ...]
    years_ahead: int
    keep_expiry_day: bool
    custom_structures: tuple[tuple[str, tuple[float, ...], tuple[int, ...]], ...]


def load_config(path=DEFAULT_CONFIG_PATH) -> UniverseConfig:
    with Path(path).open(encoding="utf-8") as file:
        raw = json.load(file)
    deferred = tuple(str(month).upper() for month in raw["deferred_months"])
    unknown = set(deferred) - set(MONTH_NUMBERS)
    if unknown:
        raise ValueError(f"Unknown deferred contract months: {sorted(unknown)}")
    custom_structures = []
    for structure in raw.get("custom_structures", []):
        name = str(structure["name"]).strip()
        coefficients = tuple(float(value) for value in structure["coefficients"])
        raw_steps = tuple(structure.get("steps", [1]))
        try:
            numeric_steps = tuple(float(value) for value in raw_steps)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Custom structure {name!r} steps must be integers"
            ) from error
        if any(not value.is_integer() for value in numeric_steps):
            raise ValueError(f"Custom structure {name!r} steps must be integers")
        steps = tuple(int(value) for value in numeric_steps)
        if not name or not coefficients or all(value == 0 for value in coefficients):
            raise ValueError("Custom structures need a name and non-zero coefficients")
        if any(step < 1 for step in steps) or len(set(steps)) != len(steps):
            raise ValueError(f"Invalid steps for custom structure {name!r}")
        if any(not value.is_integer() for value in coefficients):
            raise ValueError(f"Custom structure {name!r} coefficients must be integers")
        if abs(sum(coefficients)) > 1e-10:
            raise ValueError(f"Custom structure {name!r} must be zero-sum")
        custom_structures.append((name, coefficients, steps))
    config = UniverseConfig(
        symbol=str(raw["symbol"]).upper(),
        nearby_months=int(raw["nearby_months"]),
        deferred_months=deferred,
        years_ahead=int(raw["years_ahead"]),
        keep_expiry_day=bool(raw.get("keep_expiry_day", True)),
        custom_structures=tuple(custom_structures),
    )
    if config.nearby_months < 1 or config.years_ahead < 1:
        raise ValueError("nearby_months and years_ahead must be positive")
    return config


def latest_market_date(database_path=DATABASE_PATH) -> date | None:
    """Return the latest settlement date without modifying the database."""
    import duckdb

    path = Path(database_path)
    if not path.exists():
        return None
    connection = duckdb.connect(str(path), read_only=True)
    try:
        row = connection.execute(
            "SELECT MAX(trading_date) FROM seac_settlements"
        ).fetchone()
        return row[0] if row and row[0] else None
    finally:
        connection.close()


def load_expiries(symbol, path=DEFAULT_EXPIRY_PATH) -> list[tuple[str, date]]:
    frame = pd.read_csv(path, dtype={"contract_code": str})
    required = {"symbol", "contract_code", "expiry_date"}
    if not required.issubset(frame.columns):
        raise ValueError(f"Expiry file must contain {sorted(required)}")
    frame = frame[frame["symbol"].str.upper() == symbol.upper()].copy()
    frame["contract_code"] = frame["contract_code"].str.upper()
    frame["expiry_date"] = pd.to_datetime(frame["expiry_date"], errors="raise")
    frame = frame[frame["contract_code"].str.fullmatch(CONTRACT_PATTERN)]
    frame.sort_values("expiry_date", inplace=True)
    if frame.empty:
        raise ValueError(f"No expiry dates found for {symbol!r}")
    if frame["contract_code"].duplicated().any():
        duplicates = frame.loc[frame["contract_code"].duplicated(), "contract_code"]
        raise ValueError(f"Duplicate expiry dates for {duplicates.tolist()}")
    return [(row.contract_code, row.expiry_date.date())
            for row in frame.itertuples(index=False)]


def select_anchors(expiries, config: UniverseConfig, as_of: date) -> list[str]:
    """Select live nearby contracts followed by configured deferred months."""
    live = [
        (code, expiry)
        for code, expiry in expiries
        if expiry >= as_of if config.keep_expiry_day
    ] if config.keep_expiry_day else [
        (code, expiry) for code, expiry in expiries if expiry > as_of
    ]
    if len(live) < config.nearby_months:
        raise ValueError(
            f"Only {len(live)} live expiries are available; "
            f"{config.nearby_months} nearby contracts were requested"
        )
    nearby = live[:config.nearby_months]
    horizon_year = as_of.year + config.years_ahead
    selected = list(nearby)
    selected_codes = {code for code, _ in selected}
    for code, expiry in live[config.nearby_months:]:
        if expiry.year > horizon_year:
            break
        if code[0] in config.deferred_months and code not in selected_codes:
            selected.append((code, expiry))
            selected_codes.add(code)
    return [code for code, _ in selected]


def _expression(sequence, start, step, coefficients):
    positions = [start + step * index for index in range(len(coefficients))]
    if positions[-1] >= len(sequence):
        raise ValueError(
            f"Not enough expiry dates to build structure from {sequence[start]}"
        )
    parts = []
    for coefficient, position in zip(coefficients, positions):
        if coefficient == 0:
            continue
        code = sequence[position]
        sign = "-" if coefficient < 0 else "+"
        magnitude = abs(coefficient)
        term = code if magnitude == 1 else f"{magnitude}*{code}"
        parts.append(term if not parts else f"{sign}{term}")
    return "".join(parts)


def build_universe(expiries, anchors, custom_structures=()):
    sequence = [code for code, _ in expiries]
    position = {code: index for index, code in enumerate(sequence)}
    families = [
        ("MS", (1, -1), (1, 2, 3, 6, 12)),
        ("MF", (1, -2, 1), (1, 2, 3, 6, 12)),
        ("MDF", (1, -3, 3, -1), (1, 2, 3, 6, 12)),
        ("MDDF", (1, -4, 6, -4, 1), (1, 2, 3)),
        *custom_structures,
    ]
    columns = [f"{step}{suffix}" for suffix, _, steps in families for step in steps]
    contracts = {}
    for anchor in anchors:
        if anchor not in position:
            raise ValueError(f"Anchor {anchor!r} has no expiry entry")
        expressions = {}
        for suffix, coefficients, steps in families:
            for step in steps:
                expressions[f"{step}{suffix}"] = _expression(
                    sequence, position[anchor], step, coefficients
                )
        contracts[anchor] = expressions
    universe = {"columns": columns, "contracts": contracts}
    validate_universe(universe)
    return universe


def validate_universe(universe):
    columns = universe.get("columns")
    contracts = universe.get("contracts")
    if not isinstance(columns, list) or not columns:
        raise ValueError("Universe columns must be a non-empty list")
    if not isinstance(contracts, dict) or not contracts:
        raise ValueError("Universe contracts must be a non-empty object")
    for anchor, expressions in contracts.items():
        if not CONTRACT_PATTERN.fullmatch(anchor):
            raise ValueError(f"Invalid anchor contract {anchor!r}")
        if list(expressions) != columns:
            raise ValueError(f"{anchor} does not contain the configured columns in order")
        for name, expression in expressions.items():
            if not expression or not isinstance(expression, str):
                raise ValueError(f"Invalid {anchor}/{name} expression")
    return universe


def generate_universe(config_path=DEFAULT_CONFIG_PATH,
                      expiry_path=DEFAULT_EXPIRY_PATH, as_of=None):
    config = load_config(config_path)
    effective_date = as_of or latest_market_date() or date.today()
    if isinstance(effective_date, str):
        effective_date = date.fromisoformat(effective_date)
    expiries = load_expiries(config.symbol, expiry_path)
    anchors = select_anchors(expiries, config, effective_date)
    return build_universe(expiries, anchors, config.custom_structures), config, effective_date


def load_universe(path=None):
    """Build the current universe, optionally reading a legacy snapshot."""
    if path is not None:
        with Path(path).open(encoding="utf-8") as file:
            universe = json.load(file)
        validate_universe(universe)
        return universe
    return generate_universe()[0]


def write_universe(universe, path):
    """Atomically replace the generated JSON after full validation."""
    validate_universe(universe)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as file:
            json.dump(universe, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_name, target)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def refresh_universe(config_path=DEFAULT_CONFIG_PATH,
                     expiry_path=DEFAULT_EXPIRY_PATH,
                     output_path=None, as_of=None):
    """Generate and atomically publish a universe, returning update details."""
    old_anchors = []
    output_path = Path(output_path) if output_path else None
    if output_path and output_path.exists():
        with output_path.open(encoding="utf-8") as file:
            old_anchors = list(json.load(file).get("contracts", {}))
    universe, config, effective_date = generate_universe(
        config_path, expiry_path, as_of
    )
    new_anchors = list(universe["contracts"])
    if output_path:
        write_universe(universe, output_path)
    return {
        "symbol": config.symbol,
        "as_of": effective_date,
        "removed": [code for code in old_anchors if code not in new_anchors],
        "added": [code for code in new_anchors if code not in old_anchors],
        "contracts": len(new_anchors),
    }
