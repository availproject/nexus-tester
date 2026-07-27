"""Deterministic FastBridge scenario and custom-run configuration."""

from dataclasses import dataclass
from typing import Mapping, Optional


_TRUE_VALUES = {"1", "true", "yes", "quote", "review"}
_ROUTE_ENVIRONMENT_KEYS = {
    "FASTBRIDGE_DEST_SLUG": "destination_slug",
    "FASTBRIDGE_BRIDGE_AMOUNT": "amount",
    "FASTBRIDGE_ASSET": "asset_symbol",
    "FASTBRIDGE_SOURCE_CHAIN": "source_chain",
    "FASTBRIDGE_RECEIVE_ASSET": "receive_asset",
    "FASTBRIDGE_RECEIVE_CHAIN": "receive_chain",
    "FASTBRIDGE_EXACT_MODE": "exact_mode",
}


@dataclass(frozen=True)
class RunConfig:
    """The single source of truth for a FastBridge run."""

    scenario_id: Optional[str]
    execution_kind: str
    destination_slug: str
    amount: str
    asset_symbol: str
    source_chain: str
    receive_asset: str
    receive_chain: str
    exact_mode: str
    stop_before_execution: bool

    def is_scenario(self, scenario_id: str) -> bool:
        return self.scenario_id == scenario_id


SCENARIOS = {
    "EXP-U04": {
        "destination_slug": "base",
        "amount": "0.1",
        "asset_symbol": "USDC",
        "source_chain": "",
        "receive_asset": "USDC",
        "receive_chain": "",
        "exact_mode": "in",
    },
    "EXP-U05": {
        "destination_slug": "optimism",
        "amount": "0.0001",
        "asset_symbol": "ETH",
        "source_chain": "Base",
        "receive_asset": "ETH",
        "receive_chain": "Optimism",
        "exact_mode": "in",
    },
    "EXP-U06": {
        "destination_slug": "optimism",
        "amount": "0.1",
        "asset_symbol": "USDC",
        "source_chain": "Base",
        "receive_asset": "USDC",
        "receive_chain": "Optimism",
        "exact_mode": "in",
    },
    "EXP-U07": {
        "destination_slug": "base",
        "amount": "0.1",
        "asset_symbol": "USDC",
        "source_chain": "",
        "receive_asset": "USDC",
        "receive_chain": "Base",
        "exact_mode": "out",
    },
    "EXP-U08": {
        "destination_slug": "base",
        "amount": "0.0001",
        "asset_symbol": "ETH",
        "source_chain": "",
        "receive_asset": "ETH",
        "receive_chain": "Base",
        "exact_mode": "out",
    },
}


def _normalise_exact_mode(value: str) -> str:
    return "out" if value.strip().lower() in {"out", "exact_out", "exact-out"} else "in"


def _normalise(field: str, value: str) -> str:
    value = value.strip()
    if field == "destination_slug":
        return value.strip("/").lower()
    if field in {"asset_symbol", "receive_asset"}:
        return value.upper()
    if field == "exact_mode":
        return _normalise_exact_mode(value)
    if field in {"source_chain", "receive_chain"}:
        return value.lower()
    return value


def _custom_config(environ: Mapping[str, str], stop_before_execution: bool) -> RunConfig:
    asset_symbol = environ.get("FASTBRIDGE_ASSET", "USDC").strip().upper() or "USDC"
    return RunConfig(
        scenario_id=None,
        execution_kind="custom",
        destination_slug=environ.get("FASTBRIDGE_DEST_SLUG", "base").strip().strip("/") or "base",
        amount=environ.get("FASTBRIDGE_BRIDGE_AMOUNT", "0.1").strip() or "0.1",
        asset_symbol=asset_symbol,
        source_chain=environ.get("FASTBRIDGE_SOURCE_CHAIN", "").strip(),
        receive_asset=environ.get("FASTBRIDGE_RECEIVE_ASSET", asset_symbol).strip().upper() or asset_symbol,
        receive_chain=environ.get("FASTBRIDGE_RECEIVE_CHAIN", "").strip(),
        exact_mode=_normalise_exact_mode(environ.get("FASTBRIDGE_EXACT_MODE", "in")),
        stop_before_execution=stop_before_execution,
    )


def resolve_run_config(environ: Mapping[str, str]) -> RunConfig:
    """Resolve a canonical named scenario or an explicitly unclassified custom run.

    Named scenarios are intentionally immutable: route-changing environment variables
    must match their canonical values, otherwise execution stops before a misleading
    scenario report can be produced. Operational settings remain outside this resolver.
    """

    scenario_id = environ.get("FASTBRIDGE_SCENARIO", "").strip().upper()
    stop_before_execution = environ.get("FASTBRIDGE_STOP_BEFORE_EXECUTION", "").strip().lower() in _TRUE_VALUES
    if not scenario_id:
        return _custom_config(environ, stop_before_execution)
    if scenario_id not in SCENARIOS:
        supported = ", ".join(SCENARIOS)
        raise ValueError(f"Unknown FASTBRIDGE_SCENARIO={scenario_id!r}. Supported scenarios: {supported}.")

    scenario = SCENARIOS[scenario_id]
    for environment_key, field in _ROUTE_ENVIRONMENT_KEYS.items():
        supplied = environ.get(environment_key)
        if supplied is None or not supplied.strip():
            continue
        if _normalise(field, supplied) != _normalise(field, scenario[field]):
            raise ValueError(
                f"{environment_key}={supplied!r} conflicts with {scenario_id}'s canonical "
                f"{field}={scenario[field]!r}. Remove the override or run without FASTBRIDGE_SCENARIO."
            )

    return RunConfig(
        scenario_id=scenario_id,
        execution_kind="scenario",
        destination_slug=scenario["destination_slug"],
        amount=scenario["amount"],
        asset_symbol=scenario["asset_symbol"],
        source_chain=scenario["source_chain"],
        receive_asset=scenario["receive_asset"],
        receive_chain=scenario["receive_chain"],
        exact_mode=scenario["exact_mode"],
        stop_before_execution=stop_before_execution or scenario_id == "EXP-U04",
    )
