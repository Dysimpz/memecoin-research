import os
import re
import asyncio
from datetime import datetime, timezone
import httpx
from collections import defaultdict
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

mcp = FastMCP(
    name="Memecoin Research - All-in-One DexScreener + RugCheck + Helius",
    instructions=(
        "Read-only Solana token research tools. DexScreener provides market/pair data; "
        "RugCheck provides heuristic security reports; Helius provides indexed token-account "
        "and transaction data. Treat wallet links, same-slot activity, and common funders as "
        "investigative indicators, not proof of common beneficial ownership or a Jito bundle. "
        "Never describe a missing graph or incomplete scan as evidence of safety. "
        "Report API errors, scan limits, and missing data clearly. Never request or use private keys."
    ),
)

DEXSCREENER_BASE = "https://api.dexscreener.com"
GECKOTERMINAL_BASE = "https://api.geckoterminal.com/api/v2"
JUPITER_TOKENS_BASE = "https://api.jup.ag/tokens/v2"
RUGCHECK_BASE = "https://api.rugcheck.xyz"
HELIUS_RPC_BASE = "https://mainnet.helius-rpc.com/"
SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
SYSTEM_PROGRAM_ID = "11111111111111111111111111111111"


def valid_solana_mint(address: str) -> bool:
    return bool(address and SOLANA_MINT_RE.fullmatch(address))


async def get_json(url: str, headers: dict | None = None):
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": "memecoin-research-mcp/1.2", **(headers or {})},
        follow_redirects=True,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


def rugcheck_headers() -> dict:
    headers = {"Accept": "application/json"}
    api_key = os.getenv("RUGCHECK_API_KEY")
    if api_key:
        headers["X-API-KEY"] = api_key
    return headers


async def fetch_rugcheck(path: str) -> dict:
    url = f"{RUGCHECK_BASE}{path}"
    try:
        data = await get_json(url, headers=rugcheck_headers())
        return {
            "source": "RugCheck",
            "api_url": url,
            "data": data,
            "data_freshness_note": (
                "RugCheck may serve cached reports. Check report timestamps where available."
            ),
            "interpretation_note": (
                "RugCheck scores are heuristic screening signals, not guarantees of safety. "
                "Review individual risks and their underlying evidence."
            ),
        }
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        hints = {
            401: "The endpoint may require authentication.",
            403: "The request may be blocked or require a higher access tier.",
            404: "No report may be available for this mint, or the route may have changed.",
            429: "RugCheck rate limit reached. Wait before retrying.",
        }
        return {
            "source": "RugCheck",
            "error": "RugCheck request failed.",
            "http_status": status,
            "hint": hints.get(status, "Check the endpoint and retry later."),
            "details": exc.response.text[:800],
        }
    except (httpx.RequestError, ValueError) as exc:
        return {"source": "RugCheck", "error": str(exc)[:500]}


def helius_key() -> str | None:
    return os.getenv("HELIUS_API_KEY")


async def helius_rpc(method: str, params: list | dict) -> dict:
    api_key = helius_key()
    if not api_key:
        return {
            "error": "HELIUS_API_KEY is not configured.",
            "setup": (
                "Create a Helius API key and add it to Render as the environment variable "
                "HELIUS_API_KEY. Never paste the key into chat or commit it to GitHub."
            ),
        }

    url = f"{HELIUS_RPC_BASE}?api-key={api_key}"
    payload = {"jsonrpc": "2.0", "id": "memecoin-research", "method": method, "params": params}
    timeout = httpx.Timeout(30.0, connect=10.0)

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"Content-Type": "application/json", "User-Agent": "memecoin-research-mcp/1.2"},
        ) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            body = response.json()
    except httpx.HTTPStatusError as exc:
        return {
            "error": "Helius HTTP request failed.",
            "http_status": exc.response.status_code,
            "details": exc.response.text[:800],
        }
    except (httpx.RequestError, ValueError) as exc:
        return {"error": "Helius request failed.", "details": str(exc)[:500]}

    if body.get("error"):
        return {"error": "Helius JSON-RPC returned an error.", "details": body["error"]}
    if "result" not in body:
        return {"error": "Helius response did not contain a result.", "details": body}
    return {"result": body["result"]}


async def fetch_helius_token_asset(mint: str) -> dict:
    """Retrieve Helius DAS metadata and fungible-token information for a mint."""
    response = await helius_rpc(
        "getAsset",
        {"id": mint, "displayOptions": {"showFungible": True}},
    )
    if response.get("error"):
        return {"source": "Helius DAS getAsset", "error": response["error"], "details": response.get("details")}
    result = response.get("result") or {}
    if result and result.get("id") != mint:
        return {"source": "Helius DAS getAsset", "error": "Returned asset ID did not match the requested mint."}
    return {"source": "Helius DAS getAsset", "data": result}


async def fetch_geckoterminal_token_pools(mint: str) -> dict:
    """Fetch public GeckoTerminal Solana pools as a secondary market-data source."""
    url = f"{GECKOTERMINAL_BASE}/networks/solana/tokens/{mint}/pools?page=1&include=base_token,quote_token,dex"
    timeout = httpx.Timeout(16.0, connect=7.0)
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "memecoin-research/2.0"},
            follow_redirects=True,
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.json()
        pools = body.get("data") if isinstance(body, dict) else None
        pools = pools if isinstance(pools, list) else []
        pools.sort(
            key=lambda item: float(((item or {}).get("attributes") or {}).get("reserve_in_usd") or 0),
            reverse=True,
        )
        return {
            "source": "GeckoTerminal public API",
            "api_url": url,
            "pools": pools[:20],
            "included": body.get("included", []) if isinstance(body, dict) else [],
            "status": "available" if pools else "no_pools_found",
        }
    except httpx.HTTPStatusError as exc:
        return {
            "source": "GeckoTerminal public API",
            "api_url": url,
            "error": "Request failed.",
            "http_status": exc.response.status_code,
            "details": exc.response.text[:500],
        }
    except (httpx.RequestError, ValueError) as exc:
        return {"source": "GeckoTerminal public API", "api_url": url, "error": str(exc)[:500]}


async def fetch_jupiter_token_information(mint: str) -> dict:
    """Fetch Jupiter Tokens V2 data and accept only an exact mint-address match."""
    url = f"{JUPITER_TOKENS_BASE}/search?query={mint}"
    headers = {"Accept": "application/json", "User-Agent": "memecoin-research/2.1"}
    # Optional server-side API key. Keyless requests are supported at a lower rate.
    api_key = os.getenv("JUPITER_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    timeout = httpx.Timeout(16.0, connect=7.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.json()
        candidates = body if isinstance(body, list) else body.get("data", []) if isinstance(body, dict) else []
        if not isinstance(candidates, list):
            candidates = []
        # Search results are untrusted until the returned id exactly matches the mint.
        exact = next((x for x in candidates if isinstance(x, dict) and x.get("id") == mint), None)
        if not exact:
            return {"source": "Jupiter Tokens API V2", "api_url": url, "status": "no_exact_mint_match", "error": "No response item exactly matched the requested mint address."}
        return {"source": "Jupiter Tokens API V2", "api_url": url, "status": "available", "data": exact}
    except httpx.HTTPStatusError as exc:
        return {"source": "Jupiter Tokens API V2", "api_url": url, "status": "error", "http_status": exc.response.status_code, "error": "Request failed.", "details": exc.response.text[:500]}
    except (httpx.RequestError, ValueError) as exc:
        return {"source": "Jupiter Tokens API V2", "api_url": url, "status": "error", "error": str(exc)[:500]}


def _number_or_none(value):
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return number if number == number and abs(number) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _field_from_sources(sources: list[tuple[str, object]]):
    for name, value in sources:
        number = _number_or_none(value)
        if number is not None:
            return number, name
    return None, None


def _compare_numeric_sources(sources: dict[str, object], relative_tolerance_pct: float = 30.0, absolute_tolerance: float | None = None) -> dict:
    """Compare like-for-like provider values without averaging them.

    A conflict does not prove either provider is wrong: providers can index different
    markets or timestamps. The caller can suppress a single headline value and show
    each provider's original reading instead.
    """
    valid = {name: _number_or_none(value) for name, value in sources.items()}
    valid = {name: value for name, value in valid.items() if value is not None}
    values = list(valid.values())
    if not values:
        status, spread_pct, spread_abs = "unavailable", None, None
    elif len(values) == 1:
        status, spread_pct, spread_abs = "single_source", None, None
    else:
        low, high = min(values), max(values)
        spread_abs = high - low
        midpoint = (high + low) / 2
        spread_pct = (spread_abs / abs(midpoint) * 100) if midpoint else (0.0 if spread_abs == 0 else None)
        relative_conflict = spread_pct is not None and spread_pct > relative_tolerance_pct
        absolute_conflict = absolute_tolerance is not None and spread_abs > absolute_tolerance
        status = "disagreement" if (relative_conflict or absolute_conflict) else "providers broadly agree"
    return {
        "status": status,
        "values_by_source": valid,
        "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
        "spread_absolute": round(spread_abs, 8) if spread_abs is not None else None,
        "tolerance_relative_pct": relative_tolerance_pct,
        "tolerance_absolute": absolute_tolerance,
        "note": "Readings are shown separately. Differences can reflect provider indexing, market coverage or timestamps; no average is calculated.",
    }


def build_screening_summary(
    token_address: str,
    market_inputs: dict,
    rugcheck_data: dict,
    insider_graph: dict,
    wallet_analysis: dict,
    provider_status: dict,
) -> dict:
    """Conservative evidence summary. Never labels a token safe or claims a bundle without direct proof."""
    risk_list_available = isinstance(rugcheck_data.get("risks"), list)
    top_holder_list_available = isinstance(rugcheck_data.get("topHolders"), list)
    risks = rugcheck_data.get("risks") if risk_list_available else []
    risks = [x for x in risks if isinstance(x, dict)]
    danger_flags = [x for x in risks if str(x.get("level", "")).lower() == "danger"]
    warning_flags = [x for x in risks if str(x.get("level", "")).lower() in ("warn", "warning", "moderate")]
    top_holder_rows = rugcheck_data.get("topHolders") if top_holder_list_available else []
    top_holder_rows = [x for x in top_holder_rows if isinstance(x, dict)]
    flagged_insiders = [x for x in top_holder_rows if x.get("insider") is True]
    insider_pct = sum(_number_or_none(x.get("pct")) or 0 for x in flagged_insiders)

    jupiter_data = provider_status.get("jupiter_token_data") or {}
    jupiter_audit = jupiter_data.get("audit") if isinstance(jupiter_data.get("audit"), dict) else {}
    # Jupiter documents audit.isSus as a presence-based flag: only flagged tokens
    # receive the field. Preserve the raw value, but do not infer more than that.
    jupiter_suspicious_flag_present = "isSus" in jupiter_audit
    jupiter_suspicious_flag_value = jupiter_audit.get("isSus") if jupiter_suspicious_flag_present else None
    jupiter_verification = jupiter_data.get("isVerified") if isinstance(jupiter_data.get("isVerified"), bool) else None
    jupiter_organic_score = _number_or_none(jupiter_data.get("organicScore"))
    jupiter_holder_count = _number_or_none(jupiter_data.get("holderCount"))
    jupiter_top_holder_pct = _number_or_none(jupiter_audit.get("topHoldersPercentage"))

    # This field is RugCheck's explicit insider count. Graph payload record count is
    # deliberately kept separate and must never be presented as an insider count.
    graph_insider_count = _number_or_none(rugcheck_data.get("graphInsidersDetected"))
    graph_payload = insider_graph.get("data") if isinstance(insider_graph, dict) else None
    if isinstance(graph_payload, list):
        graph_record_count = len(graph_payload)
    elif isinstance(graph_payload, dict) and isinstance(graph_payload.get("nodes"), list):
        graph_record_count = len(graph_payload["nodes"])
    else:
        graph_record_count = None

    known_accounts = rugcheck_data.get("knownAccounts") if isinstance(rugcheck_data.get("knownAccounts"), dict) else {}
    pool_types = {"AMM", "POOL", "VAULT", "MARKET"}
    pool_owners = set()
    for owner, info in known_accounts.items():
        if not isinstance(info, dict):
            continue
        kind = str(info.get("type", "")).upper()
        label = str(info.get("name", "")).lower()
        if kind in pool_types or any(word in label for word in ("amm", "pool", "vault", "liquidity")):
            pool_owners.add(owner)

    holder_rows = wallet_analysis.get("holder_distribution") if isinstance(wallet_analysis.get("holder_distribution"), list) else []
    holder_rows = [x for x in holder_rows if isinstance(x, dict)]
    holder_scan = wallet_analysis.get("scan") if isinstance(wallet_analysis.get("scan"), dict) else {}
    unique_owners = _number_or_none(holder_scan.get("unique_owners_in_scan"))
    holder_scan_complete = holder_scan.get("scan_complete") is True
    # Concentration is not called complete unless the provider scan itself was complete
    # AND every owner in that scan is represented in the returned owner list.
    holder_list_complete = bool(holder_scan_complete and unique_owners is not None and len(holder_rows) >= int(unique_owners))
    non_pool_rows = [x for x in holder_rows if x.get("owner") not in pool_owners]
    non_pool_total = sum(_number_or_none(x.get("raw_amount")) or 0 for x in non_pool_rows)
    non_pool_rows_sorted = sorted(non_pool_rows, key=lambda x: _number_or_none(x.get("raw_amount")) or 0, reverse=True)
    top_non_pool = []
    top1_non_pool_pct = None
    top5_non_pool_pct = None
    if holder_list_complete and non_pool_total > 0:
        for row in non_pool_rows_sorted[:5]:
            amount = _number_or_none(row.get("raw_amount")) or 0
            top_non_pool.append({
                "owner": row.get("owner"),
                "share_of_non_pool_scanned_pct": round(amount / non_pool_total * 100, 4),
                "share_of_total_scanned_pct": row.get("share_of_scanned_balances_pct"),
            })
        amounts = [_number_or_none(x.get("raw_amount")) or 0 for x in non_pool_rows_sorted]
        top1_non_pool_pct = (amounts[0] / non_pool_total * 100) if amounts else None
        top5_non_pool_pct = (sum(amounts[:5]) / non_pool_total * 100) if amounts else None

    creator = rugcheck_data.get("creator")
    creator_holding = next((x for x in holder_rows if creator and x.get("owner") == creator), None)

    cluster_data = wallet_analysis.get("cluster_candidates") if isinstance(wallet_analysis.get("cluster_candidates"), dict) else {}
    same_slot = cluster_data.get("same_slot_first_receipt_candidates", [])
    shared_funders = cluster_data.get("shared_material_SOL_funder_candidates", [])
    same_slot_count = len(same_slot) if isinstance(same_slot, list) else 0
    shared_funder_count = len(shared_funders) if isinstance(shared_funders, list) else 0
    coordination_detected = same_slot_count > 0 or shared_funder_count > 0
    wallet_history_rows = wallet_analysis.get("wallet_analyses") if isinstance(wallet_analysis.get("wallet_analyses"), list) else []
    wallet_history_rows = [x for x in wallet_history_rows if isinstance(x, dict)]
    wallets_with_positive_balance_event = sum(1 for x in wallet_history_rows if isinstance(x.get("first_inbound_token_transaction"), dict))
    wallet_history_errors = sum(1 for x in wallet_history_rows if x.get("token_history_error"))
    wallets_without_positive_event = sum(1 for x in wallet_history_rows if not x.get("first_inbound_token_transaction") and not x.get("token_history_error"))
    acquisition_anchor_count = wallets_with_positive_balance_event

    # Provider-reported liquidity may be a pool reserve (DexScreener/GeckoTerminal)
    # or a token-level aggregate (Jupiter). Do not call every provider value a live pool.
    reported_liquidity = _number_or_none(market_inputs.get("liquidity_usd"))
    reported_liquidity_source = market_inputs.get("liquidity_source")
    low_liquidity = reported_liquidity is not None and reported_liquidity < 5000 and reported_liquidity_source in ("DexScreener", "GeckoTerminal", "Jupiter Tokens API V2 reported liquidity")
    concentrated = holder_list_complete and (
        (top1_non_pool_pct is not None and top1_non_pool_pct >= 20)
        or (top5_non_pool_pct is not None and top5_non_pool_pct >= 50)
    )

    rugcheck_available = provider_status.get("rugcheck") == "available"
    helius_available = provider_status.get("helius_holders") in ("available", "partial")
    market_pool_available = provider_status.get("dexscreener") == "available" or provider_status.get("geckoterminal") == "available"
    missing_market_fields = []
    for field, label in (
        ("market_cap_usd", "market cap"),
        ("fdv_usd", "FDV"),
        ("liquidity_usd", "reported liquidity"),
        ("volume_24h_usd", "24h volume"),
        ("price_usd", "USD token price"),
    ):
        if _number_or_none(market_inputs.get(field)) is None:
            missing_market_fields.append(label)

    coverage_missing = []
    if not rugcheck_available:
        coverage_missing.append("RugCheck full report")
    if not risk_list_available:
        coverage_missing.append("RugCheck risk list")
    if not top_holder_list_available:
        coverage_missing.append("RugCheck top-holder insider sample")
    if provider_status.get("insider_graph") != "available":
        coverage_missing.append("RugCheck insider graph endpoint")
    if not helius_available:
        coverage_missing.append("Helius holder scan")
    elif not holder_scan_complete:
        coverage_missing.append("complete Helius holder scan")
    if not market_pool_available:
        coverage_missing.append("matching market pool/pair data (Jupiter token metrics are not a pool listing)")
    coverage_missing.extend(missing_market_fields)
    metric_quality = market_inputs.get("market_metric_quality") if isinstance(market_inputs.get("market_metric_quality"), dict) else {}
    for metric_name, metric in metric_quality.items():
        if isinstance(metric, dict) and metric.get("status") == "disagreement":
            coverage_missing.append(f"{metric_name.replace('_', ' ')} provider disagreement")
    coverage_status = "complete" if not coverage_missing else "partial" if (rugcheck_available or helius_available or market_pool_available or provider_status.get("jupiter_tokens") == "available") else "insufficient"

    red_flags = []
    for risk in danger_flags:
        red_flags.append({"severity": "high", "title": f"RugCheck danger: {risk.get('name', 'risk flag')}"})
    for risk in warning_flags:
        red_flags.append({"severity": "medium", "title": f"RugCheck warning: {risk.get('name', 'risk flag')}"})
    if flagged_insiders:
        red_flags.append({"severity": "high", "title": f"RugCheck explicitly tagged {len(flagged_insiders)} insider holder(s)"})
    if jupiter_suspicious_flag_present:
        red_flags.append({"severity": "high", "title": "Jupiter Tokens API included its audit.isSus suspicious-token flag"})
    if graph_insider_count is not None and graph_insider_count > 0 and not flagged_insiders:
        red_flags.append({"severity": "high", "title": f"RugCheck graph reports {int(graph_insider_count)} insider(s); inspect provider-linked addresses"})
    if concentrated:
        red_flags.append({"severity": "medium", "title": "High concentration among owners not identified by RugCheck as AMM/pool/vault (unknown program accounts may remain)"})
    if low_liquidity:
        red_flags.append({"severity": "medium", "title": f"Low provider-reported liquidity below $5,000 ({reported_liquidity_source}; dashboard review heuristic)"})
    if coordination_detected:
        red_flags.append({"severity": "medium", "title": "Potential wallet timing/funding links need manual transaction review"})

    if danger_flags or flagged_insiders or jupiter_suspicious_flag_present or (graph_insider_count is not None and graph_insider_count > 0):
        overall = "high_concern"
        headline = "Serious provider risk indicators returned"
        confidence = "Explicit provider warning; not a rug probability"
        explicit_reasons = []
        if danger_flags:
            explicit_reasons.append("RugCheck danger flag(s): " + ", ".join(str(x.get("name", "risk flag")) for x in danger_flags[:3]))
        if flagged_insiders:
            explicit_reasons.append(f"RugCheck explicitly tagged {len(flagged_insiders)} holder(s) as insiders")
        if graph_insider_count is not None and graph_insider_count > 0:
            explicit_reasons.append(f"RugCheck graph reports {int(graph_insider_count)} insider(s)")
        if jupiter_suspicious_flag_present:
            explicit_reasons.append("Jupiter Tokens API included its audit.isSus suspicious-token field")
        executive = "HIGH CONCERN: " + "; ".join(explicit_reasons) + ". These are serious provider warning signals, not a measured rug probability or proof of fraud. Review the underlying risk descriptions and wallet evidence. A provider warning deserves caution even if other fields look normal."
    elif len(red_flags) >= 2:
        overall = "elevated_concern"
        headline = "Multiple caution indicators found"
        confidence = "Several screening leads; check coverage below"
        executive = "MULTIPLE CAUTION INDICATORS: " + "; ".join(x["title"] for x in red_flags[:5]) + ". These are screening leads, not proof of fraud, common ownership or bundled buying. Check the evidence and coverage before making a decision."
    elif red_flags:
        overall = "caution"
        headline = "One or more caution indicators found"
        confidence = "Limited indicators; not conclusive"
        executive = "CAUTION: " + "; ".join(x["title"] for x in red_flags[:4]) + ". Review the source and underlying evidence; a single indicator is not conclusive on its own."
    elif coverage_status != "complete" or not risk_list_available or not top_holder_list_available or not holder_list_complete:
        overall = "insufficient_data"
        headline = "Insufficient data for a meaningful screen"
        confidence = "Coverage incomplete; no low-risk conclusion"
        executive = "The available report is missing one or more important provider fields or complete scans. Missing data and missing flags are unknown—not zero, not clear, and not evidence of safety."
    else:
        overall = "no_major_flags_returned"
        headline = "No major flags returned in the available sample"
        confidence = "Required data returned; blind spots remain"
        executive = "No major danger or warning flags were returned in the provider samples that were available. This is not a low-risk or safe verdict: providers can miss information, wallet histories are bounded, and Jito bundling cannot be confirmed from a mint address alone."


    # Collect authority evidence from each provider independently. Never silently
    # choose one provider if two sources disagree about the same control.
    token_block = rugcheck_data.get("token") if isinstance(rugcheck_data.get("token"), dict) else {}
    asset = provider_status.get("helius_asset_data") or {}
    token_info = asset.get("token_info") if isinstance(asset.get("token_info"), dict) else {}
    jupiter_data = provider_status.get("jupiter_token_data") or {}
    jupiter_audit = jupiter_data.get("audit") if isinstance(jupiter_data.get("audit"), dict) else {}

    def first_present(mapping: dict, keys: tuple[str, ...]):
        for key in keys:
            if key in mapping:
                return True, mapping.get(key)
        return False, None

    def raw_authority_state(value):
        return "revoked" if value is None or value == "" else "active"

    mint_evidence = {}
    freeze_evidence = {}
    present, value = first_present(token_block, ("mintAuthority", "mint_authority"))
    if not present:
        present, value = first_present(rugcheck_data, ("mintAuthority", "mint_authority"))
    if present:
        mint_evidence["RugCheck"] = {"state": raw_authority_state(value), "raw_value": value}
    present, value = first_present(token_info, ("mint_authority",))
    if present:
        mint_evidence["Helius DAS"] = {"state": raw_authority_state(value), "raw_value": value}
    if isinstance(jupiter_audit.get("mintAuthorityDisabled"), bool):
        state = "revoked" if jupiter_audit["mintAuthorityDisabled"] else "active"
        mint_evidence["Jupiter Tokens API V2"] = {"state": state, "raw_value": jupiter_audit["mintAuthorityDisabled"]}

    present, value = first_present(token_block, ("freezeAuthority", "freeze_authority"))
    if not present:
        present, value = first_present(rugcheck_data, ("freezeAuthority", "freeze_authority"))
    if present:
        freeze_evidence["RugCheck"] = {"state": raw_authority_state(value), "raw_value": value}
    present, value = first_present(token_info, ("freeze_authority",))
    if present:
        freeze_evidence["Helius DAS"] = {"state": raw_authority_state(value), "raw_value": value}
    if isinstance(jupiter_audit.get("freezeAuthorityDisabled"), bool):
        state = "revoked" if jupiter_audit["freezeAuthorityDisabled"] else "active"
        freeze_evidence["Jupiter Tokens API V2"] = {"state": state, "raw_value": jupiter_audit["freezeAuthorityDisabled"]}

    def consensus_authority_state(evidence: dict):
        states = {item["state"] for item in evidence.values()}
        if not states:
            return "unknown", "Not returned"
        if len(states) > 1:
            return "conflict", "Conflicting provider reports"
        return next(iter(states)), ", ".join(evidence.keys())

    mint_state, mint_state_source = consensus_authority_state(mint_evidence)
    freeze_state, freeze_state_source = consensus_authority_state(freeze_evidence)
    verified_controls = []
    authority_alerts = []
    if mint_state == "revoked":
        verified_controls.append("Mint authority reported revoked by " + mint_state_source)
    elif mint_state == "active":
        authority_alerts.append({"severity": "medium", "title": f"Mint authority is active ({mint_state_source}); additional supply may be mintable"})
    elif mint_state == "conflict":
        authority_alerts.append({"severity": "medium", "title": "Providers disagree about mint authority; verify directly on-chain"})
    if freeze_state == "revoked":
        verified_controls.append("Freeze authority reported revoked by " + freeze_state_source)
    elif freeze_state == "active":
        authority_alerts.append({"severity": "medium", "title": f"Freeze authority is active ({freeze_state_source}); token accounts may be subject to freezing"})
    elif freeze_state == "conflict":
        authority_alerts.append({"severity": "medium", "title": "Providers disagree about freeze authority; verify directly on-chain"})

    # Jupiter's top-holder concentration is a distinct aggregate indicator, not
    # an insider tag. Use a clearly labelled review heuristic and never infer owners.
    if jupiter_top_holder_pct is not None and jupiter_top_holder_pct >= 50:
        authority_alerts.append({"severity": "medium", "title": f"Jupiter reports top-holder concentration of {jupiter_top_holder_pct:.2f}% (provider concentration signal, not insider ownership)"})

    if authority_alerts:
        red_flags.extend(authority_alerts)
        # Preserve an insufficient-data classification. A real warning is surfaced
        # in the flags and narrative, but it must not hide that key evidence is missing.
        if overall not in ("high_concern", "insufficient_data"):
            overall = "elevated_concern" if len(red_flags) >= 2 else "caution"
            headline = "Authority or concentration indicators require review"
            confidence = "Provider-reported control/concentration signals; not a fraud finding"
            executive = "CAUTION: " + "; ".join(x["title"] for x in red_flags[:5]) + ". Active mint/freeze controls can increase owner discretion; top-holder concentration is not proof of common ownership. Check the data coverage and source fields below."
        elif overall == "insufficient_data":
            executive += " Additional authority/concentration indicators were returned: " + "; ".join(x["title"] for x in authority_alerts[:4]) + "."

    # Unknown or conflicting control status blocks any no-major-flags conclusion.
    authority_unknown_or_conflict = mint_state in ("unknown", "conflict") or freeze_state in ("unknown", "conflict")
    if mint_state == "unknown":
        coverage_missing.append("mint authority status")
    elif mint_state == "conflict":
        coverage_missing.append("mint authority provider disagreement")
    if freeze_state == "unknown":
        coverage_missing.append("freeze authority status")
    elif freeze_state == "conflict":
        coverage_missing.append("freeze authority provider disagreement")
    if authority_unknown_or_conflict:
        if overall == "no_major_flags_returned":
            overall = "insufficient_data"
            headline = "Insufficient or conflicting authority data"
            confidence = "Authority controls not fully verified"
            executive = "The available providers did not return a consistent mint/freeze authority result. Unknown or conflicting authority states are not treated as revoked. No low-risk conclusion is made."
        coverage_status = "partial" if (rugcheck_available or helius_available or market_pool_available or provider_status.get("jupiter_tokens") == "available") else "insufficient"

    insider_graph_available = provider_status.get("insider_graph") == "available"
    if flagged_insiders:
        insider_status = "provider_flagged"
        insider_label = f"RugCheck explicitly tagged {len(flagged_insiders)} holder(s)"
        insider_note = f"Tagged balances total {insider_pct:.2f}% within RugCheck's returned top-holder records. This is a provider label; verify the underlying wallet addresses."
    elif graph_insider_count is not None and graph_insider_count > 0:
        insider_status = "provider_graph_signal"
        insider_label = f"RugCheck graph reports {int(graph_insider_count)} insider(s)"
        insider_note = "This is RugCheck's explicit graph-insider count, not the number of graph response records. Treat it as a provider relationship signal and inspect the linked addresses."
    elif not rugcheck_available or not top_holder_list_available:
        insider_status = "unavailable"
        insider_label = "Not assessable"
        insider_note = "RugCheck top-holder insider-tag data was not available. No conclusion can be drawn."
    else:
        insider_status = "no_explicit_flag_returned"
        insider_label = "No explicit RugCheck insider tag returned"
        insider_note = "RugCheck did not tag any of the sampled top holders as insiders. This is limited to the provider's returned sample; it does not rule out undisclosed, unlabelled or undiscovered related wallets. Large holdings are assessed separately as concentration, not automatically insider ownership."

    if not rugcheck_available or not risk_list_available:
        rug_status = "unavailable"
        rug_label = "Risk list unavailable"
        rug_note = "The full report or its risks array was not returned. This is unknown, not a clean result."
    elif danger_flags:
        rug_status = "high_concern"
        rug_label = "Danger-level warning returned"
        rug_note = "RugCheck labels at least one risk as danger. This is a serious provider warning, not a measured rug probability."
    elif warning_flags:
        rug_status = "caution"
        rug_label = "Warning-level flags returned"
        rug_note = "Review the listed warnings and their descriptions; the score itself is not a probability."
    else:
        rug_status = "no_danger_flags_returned"
        rug_label = "No danger/warning flags returned"
        rug_note = "No danger or warning level was present in the returned risk list. This is not proof of safety and does not replace holder, liquidity or authority checks."

    if coordination_detected:
        bundle_status = "potential_coordination_indicators"
        bundle_label = "Potential coordination leads found — not confirmed bundling"
        bundle_note = f"Potential leads: {same_slot_count} same-slot group(s) and {shared_funder_count} shared material-funder candidate(s). {acquisition_anchor_count} of {len(wallet_history_rows)} sampled wallets had a located positive token-balance-change anchor."
        bundle_note_long = "No Jito bundle ID or direct bundle-status evidence was obtained. Same-slot token balance changes can be transfers/distributions rather than buys; common funders may be exchanges, custodians, relayers or launch services. This is an investigation lead only."
    elif not helius_available or not wallet_history_rows:
        bundle_status = "unavailable"
        bundle_label = "Not assessable — wallet history unavailable"
        bundle_note = "Helius wallet-history data was unavailable or no wallets could be sampled. No bundling conclusion can be drawn."
        bundle_note_long = "A missing wallet-history result is unknown, not evidence that the token was not bundled."
    elif acquisition_anchor_count == 0:
        bundle_status = "inconclusive"
        bundle_label = "Insufficient acquisition history to assess coordination"
        bundle_note = f"No positive token-balance-change anchor was located in {len(wallet_history_rows)} sampled wallets; {wallet_history_errors} history request(s) errored. The shared-funding and same-slot checks therefore have no reliable acquisition anchors."
        bundle_note_long = "This is not a negative bundling finding. Token history is bounded and a missing event may be outside the sampled window or represented through a transfer/associated account path not captured here. Direct Jito bundle IDs are not discovered by this mint-only scan."
    else:
        bundle_status = "not_established"
        bundle_label = "No coordination leads found among assessed wallets — bundling still unconfirmed"
        bundle_note = f"No same-slot or shared material-funder candidate was detected. Positive token-balance-change anchors were located for {acquisition_anchor_count} of {len(wallet_history_rows)} sampled wallets; those changes are not proven buys."
        bundle_note_long = "No candidate in this bounded sample does not rule out coordinated buying or bundling. Direct Jito bundle status cannot be checked without a known bundle ID."

    if coordination_detected:
        executive += " Wallet timing/funding analysis found potential coordination leads, but this is not proof of bundled buying."
    elif bundle_status == "inconclusive":
        executive += " Bundling/coordination is inconclusive because no acquisition anchor was found in the bounded wallet histories."
    else:
        executive += " Bundling is not established; a mint-only scan cannot confirm or rule out Jito bundles."
    if coverage_missing:
        executive += " Data coverage is incomplete or has provider gaps: " + ", ".join(coverage_missing[:6]) + (" and more." if len(coverage_missing) > 6 else ".")

    if holder_list_complete and non_pool_total > 0:
        concentration_note = f"Largest owner not labelled as AMM/pool/vault by RugCheck: {top1_non_pool_pct:.2f}% of these scanned balances; top five: {top5_non_pool_pct:.2f}%. Unknown program accounts may remain in this group. Dashboard review thresholds (20% / 50%) are heuristics, not provider standards or proof of insider control."
    elif holder_rows:
        concentration_note = "A complete holder scan and complete returned owner list are required for aggregate concentration. The scan/list is incomplete, so no aggregate concentration conclusion is made."
    else:
        concentration_note = "No usable holder distribution was returned."

    creator_holding_out = None
    if creator_holding:
        creator_holding_out = {"owner": creator, "share_of_scanned_balances_pct": creator_holding.get("share_of_scanned_balances_pct")}

    return {
        "overall_status": overall,
        "headline": headline,
        "confidence_label": confidence,
        "executive_summary": executive,
        "red_flags": red_flags,
        "verified_controls": verified_controls,
        "rug_risk": {
            "status": rug_status, "label": rug_label, "note": rug_note,
            "flags": [x.get("name", "Risk flag") for x in risks],
            "risk_list_available": risk_list_available,
        },
        "insider_holdings": {
            "status": insider_status, "label": insider_label, "note": insider_note,
            "flagged_wallet_count": len(flagged_insiders),
            "flagged_supply_pct_in_returned_top_holders": round(insider_pct, 4),
            "flagged_wallets": [{"owner": x.get("owner"), "reported_supply_pct": x.get("pct")} for x in flagged_insiders],
            "graph_insider_count": graph_insider_count,
            "graph_record_count": graph_record_count,
            "top_holder_sample_available": top_holder_list_available,
            "creator_holding": creator_holding_out,
            "jupiter_suspicious_flag_present": jupiter_suspicious_flag_present,
            "jupiter_suspicious_flag_value": jupiter_suspicious_flag_value,
            "jupiter_top_holders_percentage": jupiter_top_holder_pct,
        },
        "jupiter_token_signals": {
            "available": provider_status.get("jupiter_tokens") == "available",
            "is_verified": jupiter_verification,
            "organic_score": jupiter_organic_score,
            "organic_score_label": jupiter_data.get("organicScoreLabel"),
            "holder_count": jupiter_holder_count,
            "suspicious_audit_flag_present": jupiter_suspicious_flag_present,
            "suspicious_audit_flag_value": jupiter_suspicious_flag_value,
            "top_holders_percentage": jupiter_top_holder_pct,
            "top_holders_percentage_note": "Raw Jupiter audit.topHoldersPercentage value (0-100 percentage scale per Jupiter example); it is not an insider label or proof of common ownership.",
            "tags": jupiter_data.get("tags") if isinstance(jupiter_data.get("tags"), list) else [],
            "note": "Jupiter verification and Organic Score are separate contextual signals, not a safety guarantee. Organic Score is relative, not a rug probability. audit.isSus is treated as a provider suspicion flag when the field is present.",
        },
        "bundling": {
            "status": bundle_status, "label": bundle_label, "note": bundle_note,
            "detailed_note": bundle_note_long,
            "confirmed_jito_bundle": False,
            "same_slot_group_count": same_slot_count,
            "shared_funder_count": shared_funder_count,
            "wallets_sampled": len(wallet_history_rows),
            "wallets_with_positive_balance_event": wallets_with_positive_balance_event,
            "wallets_without_positive_event": wallets_without_positive_event,
            "wallet_history_errors": wallet_history_errors,
            "confirmation_status": "not_established_no_bundle_id_or_direct_jito_evidence",
        },
        "holder_concentration": {
            "all_scanned_owners_returned": holder_list_complete,
            "top1_non_pool_share_pct": round(top1_non_pool_pct, 4) if top1_non_pool_pct is not None else None,
            "top5_non_pool_share_pct": round(top5_non_pool_pct, 4) if top5_non_pool_pct is not None else None,
            "top_non_pool_holders": top_non_pool,
            "note": concentration_note,
        },
        "authority_controls": {
            "mint_authority": mint_state, "freeze_authority": freeze_state,
            "mint_authority_source": mint_state_source,
            "freeze_authority_source": freeze_state_source,
            "mint_authority_evidence_by_source": mint_evidence,
            "freeze_authority_evidence_by_source": freeze_evidence,
        },
        "creator_history": {
            "creator_address": creator,
            "reported_token_launch_count": len(rugcheck_data.get("creatorTokens")) if isinstance(rugcheck_data.get("creatorTokens"), list) else None,
            "reported_tokens": rugcheck_data.get("creatorTokens") if isinstance(rugcheck_data.get("creatorTokens"), list) else [],
            "note": "Creator launch count is provider-reported context. It does not mean every listed token rugged or that the creator controls those launches today.",
        },
        "data_quality": {
            "providers": [
                {"name": "DexScreener market data", "status": provider_status.get("dexscreener", "unknown"), "detail": provider_status.get("dexscreener_detail", "")},
                {"name": "DexScreener paid orders", "status": provider_status.get("paid_orders", "unknown"), "detail": provider_status.get("paid_orders_detail", "")},
                {"name": "GeckoTerminal", "status": provider_status.get("geckoterminal", "unknown"), "detail": provider_status.get("geckoterminal_detail", "")},
                {"name": "Jupiter Tokens API V2", "status": provider_status.get("jupiter_tokens", "unknown"), "detail": provider_status.get("jupiter_tokens_detail", "")},
                {"name": "RugCheck", "status": provider_status.get("rugcheck", "unknown"), "detail": provider_status.get("rugcheck_detail", "")},
                {"name": "RugCheck insider graph", "status": provider_status.get("insider_graph", "unknown"), "detail": provider_status.get("insider_graph_detail", "")},
                {"name": "Helius DAS / wallet history", "status": provider_status.get("helius_holders", "unknown"), "detail": provider_status.get("helius_holders_detail", "")},
                {"name": "Helius token metadata / cached price", "status": provider_status.get("helius_asset", "unknown"), "detail": provider_status.get("helius_asset_detail", "")},
            ],
            "coverage_status": coverage_status,
            "coverage_missing": coverage_missing,
            "holder_scan_complete": holder_scan_complete,
            "holder_list_complete": holder_list_complete,
            "wallets_analysed": wallet_analysis.get("wallets_analysed"),
            "missing_market_fields": missing_market_fields,
            "rugcheck_risk_list_available": risk_list_available,
            "rugcheck_top_holder_sample_available": top_holder_list_available,
            "identity_metadata_conflict": bool(market_inputs.get("identity_metadata_conflict")),
            "identity_values_by_source": market_inputs.get("identity_values_by_source", {}),
            "market_metric_quality": market_inputs.get("market_metric_quality", {}),
            "same_pool_liquidity_quality": market_inputs.get("same_pool_liquidity_quality", {}),
            "rugcheck_report_mint_verified": provider_status.get("rugcheck") == "available",
        },
        "screening_thresholds_note": "Dashboard heuristics only: reported liquidity below $5,000, one non-pool owner at or above 20%, or top five at or above 50% of a complete owner list. These are review thresholds, not provider ratings, protocol standards or rug probabilities.",
    }


def _raw_amount(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


async def fetch_all_token_accounts(mint: str, max_pages: int = 5) -> dict:
    """Fetch up to max_pages*1000 token accounts using Helius page-based pagination.

    Helius page numbering is 1-indexed. Use page pagination consistently on every
    request; do not mix page and cursor pagination within the same scan.
    """
    all_accounts = []
    reported_total = None
    pages_read = 0
    limit = 1000
    last_indexed_slot = None

    for page_number in range(1, max_pages + 1):
        params = {
            "mint": mint,
            "limit": limit,
            "page": page_number,
            "options": {"showZeroBalance": False},
        }

        response = await helius_rpc("getTokenAccounts", params)
        if response.get("error"):
            return {
                "error": response["error"],
                "details": response.get("details"),
                "accounts": all_accounts,
                "pages_read": pages_read,
                "reported_total": reported_total,
                "complete": False,
            }

        result = response["result"]
        accounts = result.get("token_accounts") or result.get("tokenAccounts") or []
        all_accounts.extend(accounts)
        pages_read += 1
        reported_total = result.get("total", reported_total)
        last_indexed_slot = result.get("last_indexed_slot", last_indexed_slot)

        if not accounts:
            break

        if reported_total is not None:
            try:
                if len(all_accounts) >= int(reported_total):
                    break
            except (TypeError, ValueError):
                pass

    complete = (
        reported_total is not None
        and len(all_accounts) >= int(reported_total)
    ) if reported_total is not None else False

    return {
        "accounts": all_accounts,
        "reported_total": reported_total,
        "pages_read": pages_read,
        "complete": complete,
        "last_indexed_slot": last_indexed_slot,
        "page_limit_reached": pages_read >= max_pages and not complete,
    }


def _account_owner(account: dict) -> str | None:
    owner = account.get("owner")
    if isinstance(owner, dict):
        return owner.get("address") or owner.get("pubkey")
    return owner if isinstance(owner, str) else None


def summarize_holder_accounts(accounts: list[dict]) -> dict:
    by_owner = defaultdict(int)
    by_account = []
    total_raw = 0

    for account in accounts:
        owner = _account_owner(account)
        amount = _raw_amount(account.get("amount"))
        if not owner or amount <= 0 or account.get("burnt") is True:
            continue
        by_owner[owner] += amount
        total_raw += amount
        by_account.append({
            "token_account": account.get("address"),
            "owner": owner,
            "raw_amount": amount,
            "frozen": account.get("frozen"),
            "delegated_amount": account.get("delegated_amount"),
        })

    holders = [
        {
            "owner": owner,
            "raw_amount": amount,
            "share_of_scanned_balances_pct": (amount / total_raw * 100) if total_raw else 0,
        }
        for owner, amount in by_owner.items()
    ]
    holders.sort(key=lambda x: x["raw_amount"], reverse=True)
    return {
        "unique_owners_in_scan": len(holders),
        "total_raw_balances_scanned": str(total_raw),
        "top_holders": holders[:100],
        "token_accounts_scanned": len(accounts),
        "account_level_records": by_account[:100],
        "denominator_note": (
            "Percentages are shares of positive token balances returned by this Helius scan, "
            "not necessarily circulating-supply percentages. They may include AMM/pool vaults. "
            "Check scan completeness and classify known program-owned accounts before treating "
            "a large account as an insider."
        ),
    }


def transaction_mentions_mint(tx_record: dict, mint: str) -> bool:
    """Check parsed pre/post token balances and parsed instruction info for a mint."""
    meta = tx_record.get("meta") or {}
    for field in ("preTokenBalances", "postTokenBalances"):
        for item in meta.get(field) or []:
            if item.get("mint") == mint:
                return True

    transaction = tx_record.get("transaction") or {}
    message = transaction.get("message") or {}
    instruction_groups = [
        message.get("instructions") or [],
        *[
            group.get("instructions") or []
            for group in (meta.get("innerInstructions") or [])
        ],
    ]
    for instructions in instruction_groups:
        for instruction in instructions:
            parsed = instruction.get("parsed")
            if isinstance(parsed, dict):
                info = parsed.get("info") or {}
                if info.get("mint") == mint:
                    return True
    return False


async def helius_wallet_transactions(
    address: str,
    limit: int = 100,
    sort_order: str = "asc",
    mint_filter: str | None = None,
    max_pages: int = 10,
) -> dict:
    """Fetch Helius full transaction history, optionally filtering a mint locally.

    `tokenTransfer` cannot be combined with `tokenAccounts=all`, so this function
    requests full wallet + token-account history and filters by mint after retrieval.
    It preserves the immediately preceding transactions when it finds the first
    mint-related transaction, so funding analysis can be time-anchored to acquisition.
    """
    limit = max(1, min(int(limit), 100))
    max_pages = max(1, min(int(max_pages), 20))
    all_transactions = []
    matching_transactions = []
    prior_to_first_mint = []
    pagination_token = None
    pages_read = 0

    for _ in range(max_pages):
        previous_transactions = all_transactions[-150:]
        filters = {"status": "succeeded", "tokenAccounts": "all"}
        options = {
            "transactionDetails": "full",
            "sortOrder": sort_order,
            "limit": limit,
            "filters": filters,
            "maxSupportedTransactionVersion": 1,
        }
        if pagination_token:
            options["paginationToken"] = pagination_token

        response = await helius_rpc("getTransactionsForAddress", [address, options])
        if response.get("error"):
            return {
                **response,
                "data": matching_transactions if mint_filter else all_transactions,
                "prior_to_first_mint": prior_to_first_mint,
                "pages_read": pages_read,
                "raw_transactions_scanned": len(all_transactions),
            }

        result = response["result"]
        batch = result.get("data") or []
        pages_read += 1

        if mint_filter:
            matching_indexes = [
                index for index, tx in enumerate(batch)
                if transaction_mentions_mint(tx, mint_filter)
            ]
            if matching_indexes:
                first_match_index = matching_indexes[0]
                prior_to_first_mint = (previous_transactions + batch[:first_match_index])[-150:]
                matching_transactions.extend(batch[index] for index in matching_indexes)
                all_transactions.extend(batch)
                pagination_token = result.get("paginationToken")
                break

        all_transactions.extend(batch)
        if not mint_filter:
            matching_transactions.extend(batch)

        pagination_token = result.get("paginationToken")
        if not pagination_token or not batch:
            break

    return {
        "data": matching_transactions if mint_filter else all_transactions,
        "prior_to_first_mint": prior_to_first_mint,
        "pagination_token": pagination_token,
        "pages_read": pages_read,
        "raw_transactions_scanned": len(all_transactions),
        "mint_filter_mode": "client_side_filter_after_tokenAccounts_all" if mint_filter else "not_applied",
        "mint_filter_matches": len(matching_transactions) if mint_filter else None,
        "history_scan_complete": not bool(pagination_token),
    }


def extract_token_balance_increase(tx_record: dict, mint: str, owner: str) -> int:
    meta = tx_record.get("meta") or {}
    before = defaultdict(int)
    after = defaultdict(int)

    for item in meta.get("preTokenBalances") or []:
        if item.get("mint") == mint and item.get("owner") == owner:
            before[item.get("accountIndex")] += _raw_amount(
                (item.get("uiTokenAmount") or {}).get("amount")
            )
    for item in meta.get("postTokenBalances") or []:
        if item.get("mint") == mint and item.get("owner") == owner:
            after[item.get("accountIndex")] += _raw_amount(
                (item.get("uiTokenAmount") or {}).get("amount")
            )

    indexes = set(before) | set(after)
    return sum(after[i] - before[i] for i in indexes)


def b58decode(value: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    lookup = {char: i for i, char in enumerate(alphabet)}
    number = 0
    for char in value:
        if char not in lookup:
            raise ValueError("Invalid base58 data")
        number = number * 58 + lookup[char]
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_zeroes = len(value) - len(value.lstrip("1"))
    return b"\x00" * leading_zeroes + decoded


def _pubkey_list(message: dict) -> list[str]:
    keys = message.get("accountKeys") or []
    result = []
    for key in keys:
        if isinstance(key, str):
            result.append(key)
        elif isinstance(key, dict):
            result.append(key.get("pubkey") or key.get("key") or "")
        else:
            result.append("")
    return result


def extract_incoming_sol_transfers(tx_record: dict, wallet: str) -> list[dict]:
    """Extract directly decoded System Program SOL transfers into a target wallet."""
    transaction = tx_record.get("transaction") or {}
    message = transaction.get("message") or {}
    meta = tx_record.get("meta") or {}
    keys = _pubkey_list(message)
    signature = (transaction.get("signatures") or [None])[0]
    found = []

    def inspect_instruction(ix: dict):
        parsed = ix.get("parsed")
        if isinstance(parsed, dict):
            info = parsed.get("info") or {}
            if parsed.get("type") == "transfer" and info.get("destination") == wallet:
                found.append({
                    "from": info.get("source"),
                    "to": info.get("destination"),
                    "lamports": _raw_amount(info.get("lamports")),
                    "signature": signature,
                    "slot": tx_record.get("slot"),
                    "block_time": tx_record.get("blockTime"),
                    "decode": "parsed_system_transfer",
                })
            return

        program_id = ix.get("programId")
        program_index = ix.get("programIdIndex")
        if not program_id and isinstance(program_index, int) and program_index < len(keys):
            program_id = keys[program_index]
        if program_id != SYSTEM_PROGRAM_ID:
            return

        accounts = ix.get("accounts") or []
        resolved = []
        for account in accounts:
            if isinstance(account, int) and 0 <= account < len(keys):
                resolved.append(keys[account])
            elif isinstance(account, str):
                resolved.append(account)

        if len(resolved) < 2:
            return
        try:
            raw = b58decode(ix.get("data") or "")
            if len(raw) < 12 or int.from_bytes(raw[:4], "little") != 2:
                return
            lamports = int.from_bytes(raw[4:12], "little")
        except (ValueError, OverflowError):
            return

        if resolved[1] == wallet:
            found.append({
                "from": resolved[0],
                "to": resolved[1],
                "lamports": lamports,
                "signature": signature,
                "slot": tx_record.get("slot"),
                "block_time": tx_record.get("blockTime"),
                "decode": "compiled_system_transfer",
            })

    for instruction in message.get("instructions") or []:
        inspect_instruction(instruction)
    for inner_group in meta.get("innerInstructions") or []:
        for instruction in inner_group.get("instructions") or []:
            inspect_instruction(instruction)

    return found


async def scan_one_holder(
    owner: str,
    mint: str,
    amount_raw: int,
    semaphore: asyncio.Semaphore,
    max_tx_pages: int = 10,
    min_funding_lamports: int = 10_000_000,
) -> dict:
    """Inspect earliest bounded token-related history and pre-acquisition funding.

    Only meaningful SOL transfers (>= 0.01 SOL by default) within two hours before
    the earliest positive token balance change are kept as funding candidates.
    This avoids treating dust/spam transfers as evidence of insider funding.
    """
    async with semaphore:
        token_history = await helius_wallet_transactions(
            owner, limit=100, sort_order="asc", mint_filter=mint, max_pages=max_tx_pages
        )

    if token_history.get("error"):
        return {
            "owner": owner,
            "raw_amount": amount_raw,
            "token_history_error": token_history,
            "first_inbound_token_transaction": None,
            "funding_sources": [],
            "history_search_status": "history_error_not_assessed",
        }

    txs = token_history.get("data") or []
    first_acquisition = None
    for tx in txs:
        delta = extract_token_balance_increase(tx, mint, owner)
        if delta > 0:
            transaction = tx.get("transaction") or {}
            first_acquisition = {
                "signature": (transaction.get("signatures") or [None])[0],
                "slot": tx.get("slot"),
                "transaction_index": tx.get("transactionIndex"),
                "block_time": tx.get("blockTime"),
                "owner_token_balance_increase_raw": delta,
                "classification": "positive_token_balance_change_not_proven_buy",
            }
            break

    funding_sources = []
    funding_anchor = None
    if first_acquisition and first_acquisition.get("block_time"):
        funding_anchor = int(first_acquisition["block_time"])
        window_start = funding_anchor - 2 * 60 * 60
        for tx in token_history.get("prior_to_first_mint") or []:
            block_time = tx.get("blockTime")
            if block_time is None or not (window_start <= int(block_time) <= funding_anchor):
                continue
            for transfer in extract_incoming_sol_transfers(tx, owner):
                lamports = int(transfer.get("lamports") or 0)
                if lamports >= min_funding_lamports:
                    transfer["sol_received"] = lamports / 1_000_000_000
                    transfer["minutes_before_first_token_balance_increase"] = round(
                        (funding_anchor - int(block_time)) / 60, 2
                    )
                    funding_sources.append(transfer)

    return {
        "owner": owner,
        "raw_amount": amount_raw,
        "first_inbound_token_transaction": first_acquisition,
        "funding_sources": funding_sources[:10],
        "funding_analysis_status": (
            "time_anchored_to_first_positive_token_balance_change"
            if first_acquisition else "not_assessed_no_positive_token_balance_change_found"
        ),
        "history_search_status": (
            "first_positive_token_balance_change_found; scan_stopped_after_first_mint_event"
            if first_acquisition else (
                "no_mint_event_found_in_bounded_history_sample"
                if token_history.get("pagination_token") else "no_mint_event_found_in_returned_history"
            )
        ),
        "funding_minimum_sol": min_funding_lamports / 1_000_000_000,
        "token_history_matching_transactions_returned": len(txs),
        "token_history_raw_transactions_scanned": token_history.get("raw_transactions_scanned"),
        "token_history_pages_read": token_history.get("pages_read"),
        "token_history_scan_complete": token_history.get("history_scan_complete"),
        "token_history_filter_mode": token_history.get("mint_filter_mode"),
    }


def cluster_early_buys(wallet_analyses: list[dict], holder_lookup: dict[str, dict]) -> dict:
    by_slot = defaultdict(list)
    by_funder = defaultdict(list)

    for analysis in wallet_analyses:
        owner = analysis.get("owner")
        acquisition = analysis.get("first_inbound_token_transaction")
        holder_share = holder_lookup.get(owner, {}).get("share_of_scanned_balances_pct", 0)

        if (
            acquisition
            and acquisition.get("slot") is not None
            and acquisition.get("classification") == "positive_token_balance_change_not_proven_buy"
        ):
            by_slot[str(acquisition["slot"])].append({
                "owner": owner,
                "transaction_index": acquisition.get("transaction_index"),
                "signature": acquisition.get("signature"),
                "share_of_scanned_balances_pct": holder_share,
                "classification": acquisition.get("classification"),
            })

        for funding in analysis.get("funding_sources") or []:
            funder = funding.get("from")
            if funder and (funding.get("lamports") or 0) >= 10_000_000:
                by_funder[funder].append({
                    "recipient_wallet": owner,
                    "sol_received": funding.get("lamports", 0) / 1_000_000_000,
                    "signature": funding.get("signature"),
                    "slot": funding.get("slot"),
                })

    same_slot = []
    for slot, wallets in by_slot.items():
        if len(wallets) >= 2:
            transaction_indexes = [
                int(item["transaction_index"])
                for item in wallets
                if item.get("transaction_index") is not None
            ]
            index_span = (max(transaction_indexes) - min(transaction_indexes)) if len(transaction_indexes) >= 2 else None
            same_slot.append({
                "slot": slot,
                "wallet_count": len(wallets),
                "transaction_index_min": min(transaction_indexes) if transaction_indexes else None,
                "transaction_index_max": max(transaction_indexes) if transaction_indexes else None,
                "transaction_index_span": index_span,
                "combined_share_of_scanned_balances_pct": sum(
                    item["share_of_scanned_balances_pct"] for item in wallets
                ),
                "wallets": wallets,
                "signal_strength": (
                    "tightly_grouped_same_slot_timing_lead"
                    if index_span is not None and index_span <= 5
                    else "same_slot_timing_lead_not_bundle_proof"
                ),
            })
    same_slot.sort(key=lambda x: x["wallet_count"], reverse=True)

    common_funders = []
    for funder, recipients in by_funder.items():
        unique_recipients = {x["recipient_wallet"] for x in recipients}
        if len(unique_recipients) >= 2:
            common_funders.append({
                "funder": funder,
                "recipient_count": len(unique_recipients),
                "recipients": recipients,
                "signal_strength": "funding_link_candidate",
            })
    common_funders.sort(key=lambda x: x["recipient_count"], reverse=True)

    return {
        "same_slot_first_receipt_candidates": same_slot[:20],
        "shared_material_SOL_funder_candidates": common_funders[:20],
        "minimum_transfer_threshold_sol": 0.01,
        "interpretation": (
            "Same-slot positive token balance changes and shared material SOL funders are leads "
            "for manual review, not proof of common ownership or a Jito bundle. Funding transfers "
            "are restricted to the two hours before the first observed positive token balance "
            "change and must be at least 0.01 SOL. Exchanges, custodians, relayers and launchpad "
            "services can still create legitimate shared-funder patterns. A Jito bundle cannot "
            "be confirmed from same-slot evidence alone."
        ),
    }


@mcp.tool()
async def get_solana_token_market_data(token_address: str) -> dict:
    """Fetch DexScreener pools and market metrics for a Solana token mint address."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format. Supply a 32-44 character base58 address."}
    try:
        data = await get_json(f"{DEXSCREENER_BASE}/token-pairs/v1/solana/{token_address}")
    except httpx.HTTPStatusError as exc:
        return {"source": "DexScreener", "error": "Request failed.", "http_status": exc.response.status_code}
    except httpx.RequestError as exc:
        return {"source": "DexScreener", "error": str(exc)[:500]}
    if not isinstance(data, list) or not data:
        return {"token_address": token_address, "status": "no_pairs_found", "pairs": []}
    pairs = sorted(data, key=lambda p: (p.get("liquidity") or {}).get("usd") or 0, reverse=True)
    return {
        "token_address": token_address,
        "source": "DexScreener public API",
        "pair_count": len(pairs),
        "pairs": pairs[:20],
        "limitations": [
            "DexScreener data does not establish wallet ownership or insider links.",
            "Bundled-launch analysis needs transaction-level blockchain data.",
            "Liquidity lock/burn status requires independent verification.",
        ],
    }


@mcp.tool()
async def get_solana_token_paid_orders(token_address: str) -> dict:
    """Check DexScreener paid promotion/order status for a Solana token."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format."}
    try:
        data = await get_json(f"{DEXSCREENER_BASE}/orders/v1/solana/{token_address}")
        return {"token_address": token_address, "source": "DexScreener public API", "orders": data}
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        return {"source": "DexScreener", "error": str(exc)[:500]}


@mcp.tool()
async def get_latest_solana_boosts() -> dict:
    """Fetch the latest DexScreener boosted-token feed and filter to Solana."""
    try:
        data = await get_json(f"{DEXSCREENER_BASE}/token-boosts/latest/v1")
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        return {"source": "DexScreener", "error": str(exc)[:500]}
    if not isinstance(data, list):
        return {"source": "DexScreener", "items": []}
    return {"source": "DexScreener public API", "items": [x for x in data if x.get("chainId") == "solana"][:100]}


@mcp.tool()
async def get_rugcheck_token_report(token_address: str) -> dict:
    """Fetch RugCheck full Solana token security report: risks, authorities, markets and holder data where available."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format."}
    result = await fetch_rugcheck(f"/v1/tokens/{token_address}/report")
    result.update({"token_address": token_address, "report_type": "full"})
    return result


@mcp.tool()
async def get_rugcheck_token_report_summary(token_address: str) -> dict:
    """Fetch RugCheck concise token risk summary and risk flags where available."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format."}
    result = await fetch_rugcheck(f"/v1/tokens/{token_address}/report/summary")
    result.update({"token_address": token_address, "report_type": "summary"})
    return result


@mcp.tool()
async def get_rugcheck_insider_graph(token_address: str) -> dict:
    """Fetch RugCheck detected insider-network graph where available."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format."}
    result = await fetch_rugcheck(f"/v1/tokens/{token_address}/insiders/graph")
    result["token_address"] = token_address
    result["interpretation_note"] = (
        "Graph membership is an algorithmic relationship signal, not definitive proof of common ownership."
    )
    return result


@mcp.tool()
async def helius_connection_check() -> dict:
    """Check whether a Helius API key is configured and whether the Helius RPC endpoint responds."""
    if not helius_key():
        return {
            "configured": False,
            "message": "Add HELIUS_API_KEY to the Render service environment variables.",
            "key_dashboard": "https://dashboard.helius.dev/api-keys",
        }
    result = await helius_rpc("getSlot", [])
    return {
        "configured": True,
        "connected": not bool(result.get("error")),
        "slot_result": result.get("result"),
        "error": result.get("error"),
        "details": result.get("details"),
    }


@mcp.tool()
async def get_helius_token_holder_distribution(token_address: str, max_pages: int = 5) -> dict:
    """Get Solana token accounts from Helius DAS and aggregate balances by owner; max_pages is capped at 5."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format."}
    max_pages = max(1, min(int(max_pages), 5))
    scan = await fetch_all_token_accounts(token_address, max_pages=max_pages)
    if scan.get("error"):
        return {"source": "Helius DAS", "token_address": token_address, **scan}
    summary = summarize_holder_accounts(scan["accounts"])
    return {
        "source": "Helius DAS getTokenAccounts",
        "token_address": token_address,
        "reported_total_token_accounts": scan.get("reported_total"),
        "pages_read": scan.get("pages_read"),
        "scan_complete": scan.get("complete"),
        "last_indexed_slot": scan.get("last_indexed_slot"),
        **summary,
    }


@mcp.tool()
async def get_helius_wallet_transaction_history(
    wallet_address: str,
    limit: int = 25,
    sort_order: str = "asc",
    token_mint_filter: str = "",
) -> dict:
    """Fetch Helius full transaction history for a wallet; optionally filter inbound transfers for a token mint."""
    if not valid_solana_mint(wallet_address):
        return {"error": "Invalid Solana wallet address format."}
    if token_mint_filter and not valid_solana_mint(token_mint_filter):
        return {"error": "Invalid token mint filter."}
    if sort_order not in ("asc", "desc"):
        return {"error": "sort_order must be 'asc' or 'desc'."}
    limit = max(1, min(int(limit), 100))
    result = await helius_wallet_transactions(
        wallet_address,
        limit=limit,
        sort_order=sort_order,
        mint_filter=token_mint_filter or None,
        max_pages=3,
    )
    return {
        "source": "Helius getTransactionsForAddress",
        "wallet_address": wallet_address,
        "sort_order": sort_order,
        "limit": limit,
        **result,
    }


@mcp.tool()
async def analyze_solana_token_wallet_clusters(
    token_address: str,
    max_wallets: int = 15,
    max_pages: int = 5,
    exclude_owners: list[str] | None = None,
) -> dict:
    """
    Analyse Helius holder distribution, earliest observed inbound token receipts and early SOL funders
    for the largest non-pool owner wallets. Finds same-slot timing and shared-funder candidates;
    it does not prove a Jito bundle or common wallet ownership.
    """
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format."}
    max_wallets = max(3, min(int(max_wallets), 20))
    max_pages = max(1, min(int(max_pages), 5))

    scan = await fetch_all_token_accounts(token_address, max_pages=max_pages)
    if scan.get("error"):
        return {"source": "Helius", "token_address": token_address, **scan}

    holders = summarize_holder_accounts(scan["accounts"])
    top_holders = holders["top_holders"]

    # Exclude known pump.fun/AMM vault owners from wallet-behaviour sampling.
    pool_like = {
        "Fr8d1NVc8ewAKcE4pwUxETcgieDqi32MvHHiPfDr2GN8",
        "DfEBN79t7mN2Kh7DaGkvvikLE9J7BbBnYZYri6eFQYs3",
    }
    pool_like.update(x for x in (exclude_owners or []) if isinstance(x, str))
    selected = [h for h in top_holders if h["owner"] not in pool_like][:max_wallets]
    share_lookup = {h["owner"]: h for h in top_holders}

    semaphore = asyncio.Semaphore(4)
    wallet_analyses = await asyncio.gather(*[
        scan_one_holder(h["owner"], token_address, h["raw_amount"], semaphore, max_tx_pages=10)
        for h in selected
    ])

    clusters = cluster_early_buys(wallet_analyses, share_lookup)
    return {
        "source": "Helius DAS + getTransactionsForAddress",
        "token_address": token_address,
        "scan": {
            "reported_total_token_accounts": scan.get("reported_total"),
            "token_accounts_scanned": holders["token_accounts_scanned"],
            "unique_owners_in_scan": holders["unique_owners_in_scan"],
            "pages_read": scan.get("pages_read"),
            "scan_complete": scan.get("complete"),
            "last_indexed_slot": scan.get("last_indexed_slot"),
        },
        "holder_distribution": holders["top_holders"][:100],
        "wallets_analysed": len(wallet_analyses),
        "wallet_analyses": wallet_analyses,
        "cluster_candidates": clusters,
        "limitations": [
            "This is a bounded sample of the largest holders, not every holder.",
            "Token-mint filtering is performed locally after fetching full wallet/token-account history; up to ten pages of 100 transactions are sampled per wallet.",
            "If no token-mint transaction appears in the bounded history sample, that is inconclusive and not proof the wallet never acquired the token.",
            "First observed incoming token transfer may be a transfer, not a DEX purchase.",
            "Same-slot activity is only a timing signal; it does not prove a Jito bundle.",
            "Shared SOL funders may be exchanges, custodians, relayers, launchpad services or other common infrastructure; transfers below 0.01 SOL are excluded from funding clusters.",
            "Transaction history may be truncated by the configured per-wallet limit.",
            "A token account owner can be a program/AMM vault rather than an individual trader.",
        ],
    }


@mcp.tool()
async def analyze_solana_memecoin(token_address: str, max_wallets: int = 15) -> dict:
    """Run one evidence-labelled Solana memecoin investigation across all configured providers."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format. Supply a 32-44 character base58 address."}

    max_wallets = max(3, min(int(max_wallets), 20))
    market, paid_orders, rugcheck_report, insider_graph, helius_asset, gecko, jupiter = await asyncio.gather(
        get_solana_token_market_data.fn(token_address),
        get_solana_token_paid_orders.fn(token_address),
        get_rugcheck_token_report.fn(token_address),
        get_rugcheck_insider_graph.fn(token_address),
        fetch_helius_token_asset(token_address),
        fetch_geckoterminal_token_pools(token_address),
        fetch_jupiter_token_information(token_address),
        return_exceptions=True,
    )

    def safe_result(value, source_name: str):
        if isinstance(value, Exception):
            return {"source": source_name, "error": str(value)[:500]}
        return value if isinstance(value, dict) else {"source": source_name, "error": "Unexpected provider response shape."}

    market = safe_result(market, "DexScreener")
    paid_orders = safe_result(paid_orders, "DexScreener paid orders")
    rugcheck_report = safe_result(rugcheck_report, "RugCheck full report")
    insider_graph = safe_result(insider_graph, "RugCheck insider graph")
    helius_asset = safe_result(helius_asset, "Helius DAS getAsset")
    gecko = safe_result(gecko, "GeckoTerminal")
    jupiter = safe_result(jupiter, "Jupiter Tokens API V2")

    jupiter_data = jupiter.get("data") if isinstance(jupiter.get("data"), dict) else {}
    rugcheck_data = rugcheck_report.get("data") if isinstance(rugcheck_report.get("data"), dict) else {}
    # Fail closed: never use a RugCheck response unless its embedded mint matches
    # the exact address requested. The endpoint is mint-scoped, but the payload is
    # still checked before any risk/holder/creator data is trusted.
    if rugcheck_data and rugcheck_data.get("mint") != token_address:
        rugcheck_report = {
            "source": "RugCheck",
            "error": "RugCheck response did not contain the exact requested mint; report discarded.",
            "requested_mint": token_address,
            "returned_mint": rugcheck_data.get("mint"),
        }
        rugcheck_data = {}
    helius_asset_data = helius_asset.get("data") if isinstance(helius_asset.get("data"), dict) else {}
    pairs = market.get("pairs", []) if isinstance(market.get("pairs"), list) else []
    pairs = [p for p in pairs if isinstance(p, dict)]
    matching_pairs = [
        p for p in pairs
        if (p.get("baseToken") or {}).get("address") == token_address
        or (p.get("quoteToken") or {}).get("address") == token_address
    ]
    matching_pairs.sort(key=lambda p: _number_or_none(((p.get("liquidity") or {}).get("usd"))) or 0, reverse=True)
    primary_pair = matching_pairs[0] if matching_pairs else {}
    base_token = primary_pair.get("baseToken") or {}
    quote_token = primary_pair.get("quoteToken") or {}
    target_is_base = base_token.get("address") == token_address
    target_is_quote = quote_token.get("address") == token_address
    dex_target_token = base_token if target_is_base else quote_token if target_is_quote else {}
    info = primary_pair.get("info") or {}
    dex_volume = primary_pair.get("volume") or {}
    dex_txns = primary_pair.get("txns") or {}
    dex_liquidity = primary_pair.get("liquidity") or {}
    dex_change = primary_pair.get("priceChange") or {}
    dex_boosts = primary_pair.get("boosts") or {}

    gecko_pools = gecko.get("pools", []) if isinstance(gecko.get("pools"), list) else []
    gecko_pool = gecko_pools[0] if gecko_pools else {}
    gecko_attributes = gecko_pool.get("attributes") or {}
    gecko_included = gecko.get("included", []) if isinstance(gecko.get("included"), list) else []
    gecko_token_resources = [
        x for x in gecko_included
        if isinstance(x, dict) and x.get("type") == "token" and isinstance(x.get("attributes"), dict)
    ]
    gecko_target_resources = [
        x for x in gecko_token_resources
        if x.get("id") in {f"solana_{token_address}", token_address}
    ]
    gecko_target_meta = (
        gecko_target_resources[0].get("attributes", {})
        if gecko_target_resources else {}
    )

    helius_metadata = ((helius_asset_data.get("content") or {}).get("metadata") or {})
    rug_token_meta = rugcheck_data.get("tokenMeta") or {}
    rug_token_extension_meta = ((rugcheck_data.get("token_extensions") or {}).get("tokenMetadata") or {})
    rug_file_meta = rugcheck_data.get("fileMeta") or {}

    def first_text(candidates):
        for value, source_name in candidates:
            if isinstance(value, str) and value.strip() and value.strip().lower() not in {"unknown", "n/a", "null", "none"}:
                return value.strip(), source_name
        return None, None

    token_name, name_source = first_text([
        (helius_metadata.get("name"), "Helius DAS on-chain metadata"),
        (jupiter_data.get("name"), "Jupiter Tokens API V2"),
        (rug_token_meta.get("name"), "RugCheck token metadata"),
        (rug_token_extension_meta.get("name"), "RugCheck Token-2022 metadata"),
        (rug_file_meta.get("name"), "RugCheck file metadata"),
        (dex_target_token.get("name"), "DexScreener pair metadata"),
        (gecko_target_meta.get("name"), "GeckoTerminal token metadata"),
    ])
    token_symbol, symbol_source = first_text([
        (helius_asset_data.get("token_info", {}).get("symbol") if isinstance(helius_asset_data.get("token_info"), dict) else None, "Helius DAS token info"),
        (jupiter_data.get("symbol"), "Jupiter Tokens API V2"),
        (helius_metadata.get("symbol"), "Helius DAS on-chain metadata"),
        (rug_token_meta.get("symbol"), "RugCheck token metadata"),
        (rug_token_extension_meta.get("symbol"), "RugCheck Token-2022 metadata"),
        (rug_file_meta.get("symbol"), "RugCheck file metadata"),
        (dex_target_token.get("symbol"), "DexScreener pair metadata"),
        (gecko_target_meta.get("symbol"), "GeckoTerminal token metadata"),
    ])

    ds_market_cap = primary_pair.get("marketCap") if target_is_base else None
    ds_fdv = primary_pair.get("fdv") if target_is_base else None
    ds_liquidity = dex_liquidity.get("usd")
    ds_volume = dex_volume.get("h24")
    ds_buys = (dex_txns.get("h24") or {}).get("buys")
    ds_sells = (dex_txns.get("h24") or {}).get("sells")
    ds_change = dex_change.get("h24")
    ds_price = primary_pair.get("priceUsd") if target_is_base else None

    gecko_market_cap = gecko_attributes.get("market_cap_usd")
    gecko_fdv = gecko_attributes.get("fdv_usd")
    gecko_liquidity = gecko_attributes.get("reserve_in_usd")
    gecko_volume = (gecko_attributes.get("volume_usd") or {}).get("h24")
    gecko_txns = gecko_attributes.get("transactions") or {}
    gecko_buys = (gecko_txns.get("h24") or {}).get("buys")
    gecko_sells = (gecko_txns.get("h24") or {}).get("sells")
    gecko_change = (gecko_attributes.get("price_change_percentage") or {}).get("h24")
    gecko_price = gecko_attributes.get("token_price_usd")
    jupiter_stats_24h = jupiter_data.get("stats24h") if isinstance(jupiter_data.get("stats24h"), dict) else {}
    jupiter_buy_volume = _number_or_none(jupiter_stats_24h.get("buyVolume"))
    jupiter_sell_volume = _number_or_none(jupiter_stats_24h.get("sellVolume"))
    jupiter_total_volume = (jupiter_buy_volume + jupiter_sell_volume) if jupiter_buy_volume is not None and jupiter_sell_volume is not None else None
    jupiter_price_change_pct = _number_or_none(jupiter_stats_24h.get("priceChange"))
    jupiter_buys = _number_or_none(jupiter_stats_24h.get("numBuys"))
    jupiter_sells = _number_or_none(jupiter_stats_24h.get("numSells"))
    jupiter_market_cap = jupiter_data.get("mcap")
    jupiter_fdv = jupiter_data.get("fdv")
    jupiter_liquidity = jupiter_data.get("liquidity")
    jupiter_price = jupiter_data.get("usdPrice")
    rugcheck_liquidity = rugcheck_data.get("totalMarketLiquidity")
    helius_token_info = helius_asset_data.get("token_info") if isinstance(helius_asset_data.get("token_info"), dict) else {}
    helius_price_info = helius_token_info.get("price_info") if isinstance(helius_token_info.get("price_info"), dict) else {}
    helius_price_currency = str(helius_price_info.get("currency") or "").upper()
    helius_price_usd = _number_or_none(helius_price_info.get("price_per_token")) if helius_price_currency == "USD" else None
    helius_supply_raw = _number_or_none(helius_token_info.get("supply"))
    helius_decimals = _number_or_none(helius_token_info.get("decimals"))
    # Preserve Helius supply exactly as returned. Do not decimal-adjust or derive FDV
    # until the specific response schema guarantees whether this field is raw or UI units.
    helius_supply_ui = None
    helius_derived_fdv = None

    market_cap_source_values = {
        "DexScreener": ds_market_cap,
        "GeckoTerminal": gecko_market_cap,
        "Jupiter Tokens API V2": jupiter_market_cap,
    }
    fdv_source_values = {
        "DexScreener": ds_fdv,
        "GeckoTerminal": gecko_fdv,
        "Jupiter Tokens API V2": jupiter_fdv,
    }
    price_change_source_values = {
        "DexScreener": ds_change,
        "GeckoTerminal": gecko_change,
        "Jupiter Tokens API V2": jupiter_price_change_pct,
    }
    market_cap_quality = _compare_numeric_sources(market_cap_source_values, relative_tolerance_pct=30.0)
    fdv_quality = _compare_numeric_sources(fdv_source_values, relative_tolerance_pct=30.0)
    price_change_quality = _compare_numeric_sources(price_change_source_values, relative_tolerance_pct=100.0, absolute_tolerance=20.0)
    market_cap, market_cap_source = _field_from_sources(list(market_cap_source_values.items()))
    fdv, fdv_source = _field_from_sources(list(fdv_source_values.items()))
    if market_cap_quality["status"] == "disagreement":
        market_cap, market_cap_source = None, "Provider disagreement — inspect individual readings"
    if fdv_quality["status"] == "disagreement":
        fdv, fdv_source = None, "Provider disagreement — inspect individual readings"
    liquidity, liquidity_source = _field_from_sources([
        ("DexScreener", ds_liquidity), ("GeckoTerminal", gecko_liquidity), ("Jupiter Tokens API V2 reported liquidity", jupiter_liquidity)
    ])
    # Compare DexScreener and GeckoTerminal pool reserves only when both providers
    # identify the exact same pool. Different pool addresses are not like-for-like.
    ds_pool_address = primary_pair.get("pairAddress")
    gecko_pool_address = gecko_attributes.get("address")
    same_pool_identity = bool(ds_pool_address and gecko_pool_address and str(ds_pool_address) == str(gecko_pool_address))
    if same_pool_identity:
        pool_liquidity_quality = _compare_numeric_sources(
            {"DexScreener": ds_liquidity, "GeckoTerminal": gecko_liquidity},
            relative_tolerance_pct=50.0,
        )
        if pool_liquidity_quality["status"] == "disagreement":
            liquidity, liquidity_source = None, "Same-pool provider disagreement — inspect individual readings"
    else:
        pool_liquidity_quality = {
            "status": "different_or_unknown_pool_identity",
            "note": "Pool reserve values are not compared because exact pool identity was not established.",
            "dexscreener_pool": ds_pool_address,
            "geckoterminal_pool": gecko_pool_address,
        }
    volume_source_values = {
        "DexScreener": ds_volume,
        "GeckoTerminal": gecko_volume,
        "Jupiter Tokens API V2 buyVolume + sellVolume (derived; venue coverage may differ)": jupiter_total_volume,
    }
    volume_24h, volume_source = _field_from_sources([
        ("DexScreener", ds_volume), ("GeckoTerminal", gecko_volume),
        ("Jupiter Tokens API V2 buyVolume + sellVolume (derived)", jupiter_total_volume)
    ])
    buys_24h, buys_source = _field_from_sources([
        ("DexScreener", ds_buys), ("GeckoTerminal", gecko_buys), ("Jupiter Tokens API V2", jupiter_buys)
    ])
    sells_24h, sells_source = _field_from_sources([
        ("DexScreener", ds_sells), ("GeckoTerminal", gecko_sells), ("Jupiter Tokens API V2", jupiter_sells)
    ])
    price_change_24h, change_source = _field_from_sources(list(price_change_source_values.items()))
    if price_change_quality["status"] == "disagreement":
        price_change_24h, change_source = None, "Provider disagreement — inspect individual readings"
    price_source_values = {
        "DexScreener": ds_price,
        "GeckoTerminal": gecko_price,
        "Jupiter Tokens API V2 cached USD price": jupiter_price,
        "Helius cached price (USD; may lag)": helius_price_usd,
    }
    price_usd_quality = _compare_numeric_sources(price_source_values, relative_tolerance_pct=20.0)
    price_usd, price_source = _field_from_sources(list(price_source_values.items()))
    if price_usd_quality["status"] == "disagreement":
        price_usd, price_source = None, "Provider disagreement — inspect individual readings"

    paid_orders_available = isinstance(paid_orders.get("orders"), list) and not bool(paid_orders.get("error"))
    orders = paid_orders.get("orders", []) if paid_orders_available else []
    active_order_count = (
        sum(1 for order in orders if isinstance(order, dict) and str(order.get("status", "")).lower() == "approved")
        if paid_orders_available else None
    )

    helius_content = helius_asset_data.get("content") if isinstance(helius_asset_data.get("content"), dict) else {}
    helius_links = helius_content.get("links") if isinstance(helius_content.get("links"), dict) else {}
    websites = info.get("websites") if isinstance(info.get("websites"), list) else []
    if not websites:
        website_url = helius_links.get("website") or helius_metadata.get("external_url")
        websites = [{"label": "Website", "url": website_url}] if isinstance(website_url, str) and website_url.startswith("https://") else []
    socials = info.get("socials") if isinstance(info.get("socials"), list) else []
    if not socials and helius_links:
        socials = [{"platform": str(k), "handle": str(v)} for k, v in helius_links.items() if str(k).lower() in {"twitter", "x", "telegram", "discord", "website"} and isinstance(v, str) and v.strip()]
    known_accounts = rugcheck_data.get("knownAccounts") if isinstance(rugcheck_data.get("knownAccounts"), dict) else {}
    exclude_owners = []
    for owner, account_info in known_accounts.items():
        if not isinstance(account_info, dict):
            continue
        kind = str(account_info.get("type", "")).upper()
        label = str(account_info.get("name", "")).lower()
        if kind in {"AMM", "POOL", "VAULT", "MARKET"} or any(x in label for x in ("amm", "pool", "vault", "liquidity")):
            exclude_owners.append(owner)

    wallet_analysis = await analyze_solana_token_wallet_clusters.fn(
        token_address,
        max_wallets=max_wallets,
        max_pages=5,
        exclude_owners=exclude_owners,
    )
    wallet_analysis = safe_result(wallet_analysis, "Helius wallet analysis")
    holder_scan = wallet_analysis.get("scan", {}) if isinstance(wallet_analysis, dict) else {}

    market_inputs = {
        "token_name": token_name,
        "token_symbol": token_symbol,
        "identity_source": ", ".join(sorted(set(x for x in (name_source, symbol_source) if x))) or None,
        "name_source": name_source,
        "symbol_source": symbol_source,
        "pair_url": primary_pair.get("url"),
        "pair_address": primary_pair.get("pairAddress"),
        "gecko_pool_address": gecko_attributes.get("address"),
        "description": info.get("description") or helius_metadata.get("description") or rug_file_meta.get("description") or None,
        "websites": websites,
        "social_links": socials,
        "market_cap_usd": market_cap,
        "market_cap_source": market_cap_source,
        "market_cap_values_by_source": {k: _number_or_none(v) for k, v in market_cap_source_values.items() if _number_or_none(v) is not None},
        "fdv_usd": fdv,
        "fdv_source": fdv_source,
        "fdv_values_by_source": {k: _number_or_none(v) for k, v in fdv_source_values.items() if _number_or_none(v) is not None},
        "price_change_values_by_source": {k: _number_or_none(v) for k, v in price_change_source_values.items() if _number_or_none(v) is not None},
        "market_metric_quality": {
            "market_cap": market_cap_quality,
            "fdv": fdv_quality,
            "price_change_24h": price_change_quality,
            "price_usd": price_usd_quality,
        },
        "liquidity_usd": liquidity,
        "liquidity_source": liquidity_source,
        "rugcheck_reported_liquidity_usd": _number_or_none(rugcheck_liquidity),
        "liquidity_sources": {
            "DexScreener live pool liquidity": _number_or_none(ds_liquidity),
            "GeckoTerminal selected pool reserve": _number_or_none(gecko_liquidity),
            "Jupiter Tokens API V2 reported liquidity (not a specific-pool quote)": _number_or_none(jupiter_liquidity),
            "RugCheck cached/report aggregate (not used as live pool liquidity)": _number_or_none(rugcheck_liquidity),
        },
        "same_pool_liquidity_quality": pool_liquidity_quality,
        "volume_24h_usd": volume_24h,
        "volume_source": volume_source,
        "volume_24h_values_by_source": {k: _number_or_none(v) for k, v in volume_source_values.items() if _number_or_none(v) is not None},
        "buys_24h": buys_24h,
        "sells_24h": sells_24h,
        "transactions_source": buys_source if buys_source == sells_source else f"Buys: {buys_source or 'not returned'}; sells: {sells_source or 'not returned'}",
        "price_change_24h_pct": price_change_24h,
        "price_change_source": change_source,
        "price_usd": price_usd,
        "price_source": price_source,
        "price_usd_values_by_source": {k: _number_or_none(v) for k, v in price_source_values.items() if _number_or_none(v) is not None},
        "helius_cached_price_usd": helius_price_usd,
        "helius_cached_price_value": _number_or_none(helius_price_info.get("price_per_token")),
        "helius_price_currency": helius_price_currency or None,
        "helius_total_supply_tokens": None,
        "helius_supply_value_as_returned": helius_supply_raw,
        "pair_created_at_ms": primary_pair.get("pairCreatedAt"),
        "gecko_pool_created_at": gecko_attributes.get("pool_created_at"),
        "active_dex_boosts": (dex_boosts.get("active") if primary_pair else None),
        "dex_paid_orders": orders,
        "approved_paid_order_count": active_order_count,
        "social_attention_caveat": (
            "Social links and paid-promotion fields are metadata only. They do not establish follower growth, "
            "organic engagement, mention velocity or sentiment."
        ),
        "market_data_source": "DexScreener" if primary_pair else ("GeckoTerminal" if gecko_pool else ("Jupiter Tokens API V2" if jupiter_data else None)),
        "market_data_unavailable_reason": None if (primary_pair or gecko_pool or jupiter_data) else "No matching market/token record was returned by DexScreener, GeckoTerminal or Jupiter Tokens API V2.",
        "jupiter_is_verified": jupiter_data.get("isVerified") if isinstance(jupiter_data.get("isVerified"), bool) else None,
        "jupiter_organic_score": _number_or_none(jupiter_data.get("organicScore")),
        "jupiter_organic_score_label": jupiter_data.get("organicScoreLabel"),
        "jupiter_holder_count": _number_or_none(jupiter_data.get("holderCount")),
        "jupiter_audit": jupiter_data.get("audit") if isinstance(jupiter_data.get("audit"), dict) else {},
        "jupiter_updated_at": jupiter_data.get("updatedAt"),
        "rugcheck_detected_at": rugcheck_data.get("detectedAt"),
        "helius_last_indexed_slot": holder_scan.get("last_indexed_slot"),
    }

    dex_available = bool(primary_pair)
    gecko_available = bool(gecko_pools)
    jupiter_available = bool(jupiter_data) and jupiter.get("status") == "available"
    rug_available = bool(rugcheck_data) and not bool(rugcheck_report.get("error"))
    insider_graph_available = not bool(insider_graph.get("error"))
    helius_holder_status = "available" if holder_scan.get("scan_complete") is True else "partial" if wallet_analysis.get("wallets_analysed") else "error"
    helius_asset_available = bool(helius_asset_data)
    provider_status = {
        "dexscreener": "available" if dex_available else "no_data" if not market.get("error") else "error",
        "dexscreener_detail": f"{len(matching_pairs)} matching pool(s) returned." if matching_pairs else (market.get("error") or "No matching pair returned."),
        "geckoterminal": "available" if gecko_available else "no_data" if not gecko.get("error") else "error",
        "geckoterminal_detail": f"{len(gecko_pools)} pool(s) returned." if gecko_pools else (gecko.get("error") or gecko.get("details") or "No pool returned."),
        "jupiter_tokens": "available" if jupiter_available else "error" if jupiter.get("status") == "error" else "no_data",
        "jupiter_tokens_detail": ("Exact mint match returned; identity was verified against the requested mint." if jupiter_available else jupiter.get("error") or "No exact mint record returned."),
        "rugcheck": "available" if rug_available else "error",
        "rugcheck_detail": "Full report returned; embedded mint exactly matched the requested address." if rug_available else (rugcheck_report.get("error") or rugcheck_report.get("details") or "No usable report returned."),
        "insider_graph": "available" if insider_graph_available else "error",
        "insider_graph_detail": "Endpoint responded; inspect explicit RugCheck insider fields separately." if insider_graph_available else (insider_graph.get("error") or "Graph request failed."),
        "helius_holders": helius_holder_status,
        "helius_holders_detail": f"{holder_scan.get('token_accounts_scanned', 0)} token account(s) scanned; {wallet_analysis.get('wallets_analysed', 0)} wallet histories sampled." if helius_holder_status != "error" else (wallet_analysis.get("error") or "Holder scan unavailable."),
        "helius_asset": "available" if helius_asset_available else "error",
        "helius_asset_detail": ("Mint metadata returned." + (f" Cached price returned in {helius_price_currency}; it may lag and is not converted across currencies." if _number_or_none(helius_price_info.get("price_per_token")) is not None and helius_price_currency else " No cached price returned.")) if helius_asset_available else (helius_asset.get("error") or "Metadata not returned."),
        "paid_orders": "available" if paid_orders_available else "error" if paid_orders.get("error") else "no_data",
        "paid_orders_detail": "Paid-order response returned." if paid_orders_available else (paid_orders.get("error") or "No paid-order array returned."),
        "helius_asset_data": helius_asset_data,
        "jupiter_token_data": jupiter_data,
    }

    # Record conflicting identity metadata instead of silently treating sources as identical.
    identity_values = {}
    for source_name, candidate in [
        ("Helius DAS", helius_metadata.get("name")),
        ("Jupiter Tokens API V2", jupiter_data.get("name")),
        ("RugCheck", rug_token_meta.get("name") or rug_file_meta.get("name")),
        ("DexScreener", dex_target_token.get("name")),
    ]:
        if isinstance(candidate, str) and candidate.strip():
            identity_values[source_name] = candidate.strip()
    identity_conflict = len(set(identity_values.values())) > 1
    market_inputs["identity_values_by_source"] = identity_values
    market_inputs["identity_metadata_conflict"] = identity_conflict

    screening_summary = build_screening_summary(
        token_address,
        market_inputs,
        rugcheck_data,
        insider_graph,
        wallet_analysis,
        provider_status,
    )

    # Keep source payloads unchanged; derived authority states live only in screening_summary.

    return {
        "report_type": "combined_solana_memecoin_investigation_v2",
        "token_address": token_address,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "market_and_narrative_inputs": market_inputs,
        "market_data_sources": {
            "dexscreener": market,
            "geckoterminal": gecko,
            "helius_asset": helius_asset,
            "jupiter_tokens": jupiter,
        },
        "rugcheck_full_security_report": rugcheck_report,
        "rugcheck_insider_network_graph": insider_graph,
        "helius_holder_and_wallet_analysis": wallet_analysis,
        "screening_summary": screening_summary,
        "quick_status": {
            "dexscreener_pairs_found": len(matching_pairs),
            "geckoterminal_pools_found": len(gecko_pools),
            "jupiter_token_record_available": jupiter_available,
            "rugcheck_report_available": rug_available,
            "rugcheck_insider_graph_available": insider_graph_available,
            "helius_holder_scan_complete": holder_scan.get("scan_complete"),
            "helius_wallets_analysed": wallet_analysis.get("wallets_analysed"),
            "same_slot_candidates": screening_summary["bundling"]["same_slot_group_count"],
            "shared_material_funder_candidates": screening_summary["bundling"]["shared_funder_count"],
        },
        "interpretation_limits": [
            "No analysis can prove that a token is safe or predict a rug with certainty.",
            "RugCheck risk levels are provider screening signals, not rug probabilities.",
            "RugCheck insider tags apply only to wallets explicitly marked by the provider; untagged wallets may still be related.",
            "Holder concentration uses Helius token-account owners and excludes only provider-labelled AMM/pool/vault owners; unknown program wallets may remain.",
            "Holder percentages are relative to balances returned in the Helius scan, not necessarily circulating supply.",
            "Wallet transaction histories are bounded samples. A positive token balance change may be a purchase, transfer, distribution or other movement.",
            "Same-slot balance changes and shared funding wallets are leads, not proof of common ownership or a Jito bundle.",
            "This service does not discover Jito bundle IDs from a token mint; Jito status verification requires a known bundle ID.",
            "Market cap is not substituted with FDV. Every displayed market field includes its provider source.",
            "DexScreener, GeckoTerminal and RugCheck may index different pools or timestamps; compare provider values rather than assuming they are identical.",
            "A missing value or an empty risk/insider response is unknown or not detected, not a clean bill of health.",
        ],
    }


# Evidence-labelled dashboard served by this Render service.
DASHBOARD_HTML = '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<title>Solana Memecoin Research</title>\n<style>\n:root{color-scheme:dark;--bg:#090e16;--panel:#111b29;--panel2:#0d1622;--line:#26364b;--muted:#91a3ba;--text:#edf4fc;--green:#7de0b2;--red:#ff818b;--yellow:#f2ce79;--blue:#91c2ff;--purple:#c4adff}\n*{box-sizing:border-box}body{margin:0;background:radial-gradient(950px 520px at 80% -12%,#1a3150 0%,transparent 61%),var(--bg);color:var(--text);font:14px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}\n.wrap{max-width:1180px;margin:0 auto;padding:30px 22px 64px}.top{display:flex;align-items:center;gap:12px;margin-bottom:35px}.mark{width:42px;height:42px;border-radius:13px;background:linear-gradient(135deg,#7ab6ff,#8c7af7);display:grid;place-items:center;color:#07111e;font-weight:900;font-size:20px}.eyebrow{color:var(--blue);font-size:10px;font-weight:850;letter-spacing:.16em;text-transform:uppercase}.top h1{font-size:19px;margin:0;letter-spacing:-.02em}.top .sub{font-size:12px;color:var(--muted)}\n.hero{max-width:780px;margin:0 auto 27px;text-align:center}.hero h2{font-size:clamp(28px,5vw,43px);line-height:1.12;letter-spacing:-.045em;margin:0 0 12px}.hero p{color:var(--muted);margin:0 auto 22px;max-width:650px}.search{display:flex;gap:10px;background:#111a28;border:1px solid #2b3d55;padding:9px;border-radius:16px;box-shadow:0 18px 50px #0005}.search input{flex:1;min-width:0;background:transparent;border:0;outline:0;color:var(--text);font:inherit;padding:12px}.search button,.action{border:0;border-radius:11px;padding:12px 19px;background:#8abaff;color:#081321;font-weight:800;font:inherit;cursor:pointer;white-space:nowrap}.search button:hover,.action:hover{filter:brightness(1.08)}button:disabled{opacity:.55;cursor:wait}.hint{color:var(--muted);font-size:12px;margin-top:10px}\n#status{display:none;margin:22px auto;max-width:780px;border:1px solid var(--line);border-radius:12px;padding:13px 16px;color:var(--muted);background:var(--panel)}.spinner{display:inline-block;width:13px;height:13px;border:2px solid #41536b;border-top-color:var(--blue);border-radius:50%;animation:spin .8s linear infinite;vertical-align:-2px;margin-right:8px}@keyframes spin{to{transform:rotate(360deg)}}#error{display:none;max-width:780px;margin:18px auto;background:#351b25;border:1px solid #74404c;color:#ffdce0;padding:14px;border-radius:12px}\n#results{display:none;margin-top:34px}.reporthead{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin:0 0 17px}.reporthead h2{font-size:27px;line-height:1.15;margin:0 0 6px}.muted{color:var(--muted)}.tiny{font-size:11px}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}.statuspill{border:1px solid var(--line);border-radius:999px;padding:7px 12px;font-size:11px;font-weight:800;color:#d3dfed;white-space:normal;max-width:300px;text-align:center}.statuspill.danger{border-color:#81434d;background:#351b25;color:#ffc0c6}.statuspill.warn{border-color:#7b6337;background:#302817;color:#f7dda0}.statuspill.neutral{border-color:#465a73;background:#172333;color:#d3e3f6}.statuspill.good{border-color:#396e58;background:#14291f;color:#b0e5c9}\n.panel,.metric,.summarycard{background:linear-gradient(180deg,#131e2d,#101925);border:1px solid var(--line);border-radius:15px}.summarycard{padding:20px;margin:16px 0 18px;border-left:4px solid #657b97}.summarycard.danger{border-left-color:var(--red)}.summarycard.warn{border-left-color:var(--yellow)}.summarycard.neutral{border-left-color:var(--blue)}.summarytop{display:flex;justify-content:space-between;align-items:flex-start;gap:18px}.summarytop h3{font-size:17px;margin:0 0 5px}.summarytext{color:#d8e3f1;max-width:900px;margin:10px 0 14px}.summarygrid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.summaryitem{border:1px solid #28394f;background:#0d1723;border-radius:11px;padding:12px}.summaryitem .label{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.09em;margin-bottom:5px}.summaryitem strong{display:block;font-size:13px}.flags{display:flex;gap:7px;flex-wrap:wrap;margin-top:12px}.flag{border:1px solid #45566b;background:#172436;color:#d7e3f2;border-radius:8px;padding:6px 9px;font-size:11px}.flag.red{border-color:#74404b;background:#331d27;color:#ffc1c8}.flag.yellow{border-color:#705a34;background:#302719;color:#f4d99a}.flag.blue{border-color:#3e5d83;background:#16283f;color:#c6defe}\n.metrics{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin:15px 0 18px}.metric{padding:14px;min-width:0}.metric .label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.metric .value{font-size:20px;font-weight:800;letter-spacing:-.03em;margin-top:5px;overflow-wrap:anywhere}.metric .source{font-size:10px;color:var(--muted);margin-top:5px}.pos{color:var(--green)}.neg{color:var(--red)}\n.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.panel{padding:18px;margin-bottom:14px;min-width:0}.panel h3{font-size:15px;margin:0 0 13px}.panel p{margin:7px 0}.rowline{display:flex;justify-content:space-between;gap:14px;padding:9px 0;border-bottom:1px solid #223044}.rowline:last-child{border-bottom:0}.rowline span:first-child{color:var(--muted)}.rowline strong{text-align:right;overflow-wrap:anywhere}.tag{display:inline-block;padding:3px 8px;margin:2px 4px 2px 0;border-radius:999px;background:#1b2a3e;color:#bdd8ff;font-size:10px}.tag.red{background:#3b2029;color:#ffb8bf}.tag.yellow{background:#392f1b;color:#f5d68d}.tag.green{background:#153327;color:#98e9c3}.tag.purple{background:#2b2340;color:#d6c6ff}\n.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}th,td{text-align:left;padding:10px 9px;border-bottom:1px solid #263449;vertical-align:top}th{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:700}td code{font-size:10px;color:#bdd8ff;overflow-wrap:anywhere}a{color:#9bc5ff;text-decoration:none}a:hover{text-decoration:underline}.empty{color:var(--muted);font-size:12px}.sectionnote{border-top:1px solid var(--line);padding-top:11px;margin-top:12px;color:var(--muted);font-size:11px}.sourcegrid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:9px}.sourcebox{border:1px solid var(--line);background:#0e1723;border-radius:10px;padding:11px}.sourcebox strong{font-size:12px}.sourcebox p{font-size:11px;color:var(--muted);margin:5px 0 0}.riskline{border-left:3px solid var(--red);padding-left:11px;margin:10px 0}.riskline.warn{border-left-color:var(--yellow)}.riskline.info{border-left-color:var(--blue)}.actions{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}.action.secondary{background:#1b2a3e;color:#d9e8fb;border:1px solid #334761}.accordion{margin-top:14px}.accordion summary{cursor:pointer;color:#aecfff;font-size:12px}.accordion pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:520px;overflow:auto;background:#090e16;border:1px solid var(--line);border-radius:10px;padding:14px;color:#bfd0e5;font-size:10px}\nfooter{margin-top:36px;color:#71839b;font-size:10px;text-align:center}\n@media(max-width:950px){.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:760px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.grid{grid-template-columns:1fr}.summarygrid,.sourcegrid{grid-template-columns:1fr}.reporthead,.summarytop{flex-direction:column}.search{flex-direction:column}.search button{width:100%}}\n</style>\n</head>\n<body><main class="wrap">\n<header class="top"><div class="mark">M</div><div><div class="eyebrow">Research terminal</div><h1>Solana Memecoin Research</h1><div class="sub">DexScreener · GeckoTerminal · RugCheck · Helius</div></div></header>\n<section class="hero"><h2>Investigate a token.<br>Separate evidence from guesswork.</h2><p>Enter a Solana mint address to run market, security, holder and wallet-history checks. Missing data is shown as unknown—not treated as a clean result.</p>\n<form id="form" class="search"><input id="mint" autocomplete="off" spellcheck="false" placeholder="Paste Solana token mint address" aria-label="Solana token mint address" required><button id="run" type="submit">Analyse token <span aria-hidden="true">→</span></button></form><div class="hint">Read-only research. Never enter a seed phrase or private key.</div></section>\n<div id="status" role="status"><span class="spinner"></span><span id="statusText">Running investigation…</span></div><div id="error" role="alert"></div>\n<section id="results" aria-live="polite"></section>\n<footer>Screening indicators are not guarantees. A missing flag is not proof of safety. Verify transaction evidence before acting.</footer>\n</main>\n<script>\n\nconst $=id=>document.getElementById(id);const mintPattern=/^[1-9A-HJ-NP-Za-km-z]{32,44}$/;\nconst esc=v=>String(v??\'—\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst finite=v=>v!==null&&v!==undefined&&v!==\'\'&&Number.isFinite(Number(v));\nconst money=v=>finite(v)?\'$\'+Number(v).toLocaleString(undefined,{maximumFractionDigits:2}):\'Not returned\';\nconst count=v=>finite(v)?Number(v).toLocaleString():\'Not returned\';\nconst pct=v=>finite(v)?Number(v).toFixed(2)+\'%\':\'Not returned\';\nconst short=v=>v?String(v).slice(0,6)+\'…\'+String(v).slice(-5):\'—\';\nconst date=v=>finite(v)?new Date(Number(v)*1000).toLocaleString():\'Not located in the sampled history\';\nlet busyMessages=[\'Fetching DexScreener and GeckoTerminal pool data…\',\'Checking RugCheck security and creator signals…\',\'Scanning Helius holders and wallet transaction histories…\',\'Comparing source coverage and preparing the report…\'];let progressTimer=null;\n\nfunction section(title, body) {\n  return `<section class="panel"><h3>${title}</h3>${body}</section>`;\n}\nfunction tag(text, kind = \'\') {\n  return `<span class="tag ${kind}">${esc(text)}</span>`;\n}\nfunction fieldSource(source) {\n  return source ? `<div class="source">Source: ${esc(source)}</div>` : \'<div class="source">Source: not returned</div>\';\n}\nfunction priceFmt(value) {\n  if (!finite(value)) return \'Not returned\';\n  const n = Number(value);\n  if (n === 0) return \'$0\';\n  return \'$\' + Number(n.toPrecision(7)).toLocaleString(undefined, {maximumFractionDigits: 12});\n}\nfunction statusClass(status) {\n  if (status === \'high_concern\') return \'danger\';\n  if (status === \'elevated_concern\' || status === \'caution\') return \'warn\';\n  return \'neutral\';\n}\nfunction statusLabel(status) {\n  return ({\n    high_concern: \'HIGH CONCERN — PROVIDER RISK FLAGS\',\n    elevated_concern: \'ELEVATED CONCERN\',\n    caution: \'CAUTION — REVIEW INDICATORS\',\n    insufficient_data: \'INSUFFICIENT DATA\',\n    no_major_flags_returned: \'NO MAJOR FLAGS RETURNED — NOT A SAFETY CLEARANCE\'\n  })[status] || \'ASSESSMENT UNAVAILABLE\';\n}\nfunction socialText(items) {\n  if (!Array.isArray(items) || !items.length) return \'<span class="empty">No social links returned by the available metadata sources.</span>\';\n  return items.map(x => {\n    const platform = x.platform || x.type || x.label || \'Social\';\n    const handle = x.handle || x.url || \'\';\n    return `<span class="tag">${esc(platform)}: ${esc(handle)}</span>`;\n  }).join(\' \');\n}\nfunction render(d) {\n  const m = d.market_and_narrative_inputs || {};\n  const q = d.quick_status || {};\n  const s = d.screening_summary || {};\n  const r = (d.rugcheck_full_security_report || {}).data || {};\n  const h = d.helius_holder_and_wallet_analysis || {};\n  const cluster = h.cluster_candidates || {};\n  const risks = Array.isArray(r.risks) ? r.risks : [];\n  const holders = Array.isArray(h.holder_distribution) ? h.holder_distribution : [];\n  const wallets = Array.isArray(h.wallet_analyses) ? h.wallet_analyses : [];\n  const known = r.knownAccounts || {};\n  const creator = r.creator;\n  const bundle = s.bundling || {};\n  const insider = s.insider_holdings || {};\n  const concentration = s.holder_concentration || {};\n  const rug = s.rug_risk || {};\n  const quality = s.data_quality || {};\n  const jupiter = s.jupiter_token_signals || {};\n  const summaryClass = statusClass(s.overall_status);\n  const pairUrl = m.pair_url && /^https:\\/\\//.test(m.pair_url) ? m.pair_url : null;\n  const solscan = `https://solscan.io/token/${encodeURIComponent(d.token_address || \'\')}`;\n  const tokenName = m.token_name || \'Token name not returned\';\n  const tokenSymbol = m.token_symbol || \'\';\n  const identitySource = m.identity_source || \'Not returned\';\n  const riskListAvailable = quality.rugcheck_risk_list_available === true;\n  const holderSampleAvailable = insider.top_holder_sample_available === true;\n\n  const riskHtml = !riskListAvailable\n    ? \'<p class="empty">RugCheck did not return a usable risk list. No conclusion can be made from missing data.</p>\'\n    : risks.length\n      ? risks.map(x => {\n          const level = String(x.level || \'unknown\').toLowerCase();\n          const kind = level === \'danger\' ? \'red\' : [\'warn\', \'warning\', \'moderate\'].includes(level) ? \'yellow\' : \'\';\n          const lineClass = level === \'danger\' ? \'\' : [\'warn\', \'warning\', \'moderate\'].includes(level) ? \'warn\' : \'info\';\n          return `<div class="riskline ${lineClass}"><strong>${esc(x.name || \'Provider risk flag\')} ${tag(level.toUpperCase(), kind)}</strong><p class="muted tiny">${esc(x.description || \'No description returned.\')}</p></div>`;\n        }).join(\'\')\n      : \'<p class="empty">RugCheck returned an empty risk list. This means no entries were supplied in this response; it is not proof of safety.</p>\';\n\n  const providerRows = (quality.providers || []).map(x => {\n    const status = String(x.status || \'unknown\').toLowerCase();\n    const kind = status === \'available\' ? \'green\' : status === \'error\' ? \'red\' : \'yellow\';\n    return `<div class="sourcebox"><strong>${esc(x.name)}</strong>${tag(status, kind)}<p>${esc(x.detail || \'No additional detail returned.\')}</p></div>`;\n  }).join(\'\');\n\n  const holderRows = holders.slice(0, 20).map(x => {\n    const meta = known[x.owner] || {};\n    const rc = (r.topHolders || []).find(y => y && y.owner === x.owner);\n    const isInsider = rc?.insider === true;\n    const name = meta.name || (x.owner === creator ? \'Creator wallet\' : \'Wallet\');\n    const type = String(meta.type || \'\').toUpperCase();\n    const isPool = [\'AMM\', \'POOL\', \'VAULT\', \'MARKET\'].includes(type) || /amm|pool|vault|liquidity/i.test(String(meta.name || \'\'));\n    const kind = isPool ? \'yellow\' : isInsider ? \'red\' : x.owner === creator ? \'purple\' : \'\';\n    const role = isPool ? \'POOL / VAULT\' : isInsider ? \'RUGCHECK INSIDER TAG\' : x.owner === creator ? \'CREATOR\' : \'\';\n    return `<tr><td>${esc(name)} ${role ? tag(role, kind) : \'\'}<br><code title="${esc(x.owner)}">${esc(short(x.owner))}</code></td><td>${pct(x.share_of_scanned_balances_pct)}</td></tr>`;\n  }).join(\'\');\n\n  const insiderFlags = (insider.flagged_wallets || []).map(x =>\n    `<div class="rowline"><span><code title="${esc(x.owner)}">${esc(short(x.owner))}</code></span><strong>${pct(x.reported_supply_pct)}</strong></div>`\n  ).join(\'\');\n  const graphInsiderLine = finite(insider.graph_insider_count)\n    ? `<div class="rowline"><span>RugCheck graph-reported insider count</span><strong>${count(insider.graph_insider_count)}</strong></div>`\n    : \'<div class="rowline"><span>RugCheck graph-reported insider count</span><strong>Not returned</strong></div>\';\n  const graphRecordLine = finite(insider.graph_record_count)\n    ? `<div class="rowline"><span>Graph response records (not an insider count)</span><strong>${count(insider.graph_record_count)}</strong></div>`\n    : \'<div class="rowline"><span>Graph response records</span><strong>Not returned</strong></div>\';\n  const insiderTagText = !holderSampleAvailable\n    ? \'<p class="empty">RugCheck top-holder insider tags were not returned. Insider holdings are not assessable.</p>\'\n    : insiderFlags || \'<p class="empty">No explicit insider tag was returned in the sampled RugCheck top holders. This does not rule out hidden, unlabelled or undiscovered related wallets.</p>\';\n\n  const topNonPool = (concentration.top_non_pool_holders || []).map(x =>\n    `<div class="rowline"><span><code title="${esc(x.owner)}">${esc(short(x.owner))}</code></span><strong>${pct(x.share_of_non_pool_scanned_pct)}</strong></div>`\n  ).join(\'\');\n  const concentrationView = concentration.all_scanned_owners_returned === true\n    ? (topNonPool || \'<p class="empty">No non-pool owner balances were available for calculation.</p>\')\n    : \'<p class="empty">Not calculated: the Helius holder scan and returned owner list must both be complete.</p>\';\n\n  const creatorView = insider.creator_holding\n    ? `${pct(insider.creator_holding.share_of_scanned_balances_pct)} of total scanned balances`\n    : creator && concentration.all_scanned_owners_returned === true\n      ? \'Creator was not present in the complete returned owner list\'\n      : \'Not established — returned owner list is not proven complete\';\n\n  const walletRows = wallets.map(x => {\n    const acquisition = x.first_inbound_token_transaction;\n    const funding = (x.funding_sources || []).filter(y => finite(y.sol_received) && Number(y.sol_received) >= 0.01);\n    let acquisitionCell;\n    if (x.token_history_error) acquisitionCell = \'History error — not assessed\';\n    else if (acquisition) acquisitionCell = `<a target="_blank" rel="noopener" href="https://solscan.io/tx/${encodeURIComponent(acquisition.signature || \'\')}">${esc(date(acquisition.block_time))} ↗</a><div class="tiny muted">Positive token balance change; not proven buy</div>`;\n    else acquisitionCell = esc(x.history_search_status || \'No event found in bounded history; inconclusive\');\n    let fundingCell;\n    if (x.funding_analysis_status === \'not_assessed_no_positive_token_balance_change_found\') fundingCell = \'Not assessed — no anchor event located\';\n    else if (x.token_history_error) fundingCell = \'Not assessed — history error\';\n    else if (funding.length) fundingCell = funding.map(y => `${Number(y.sol_received).toFixed(3)} SOL from <code title="${esc(y.from)}">${esc(short(y.from))}</code> (${esc(y.minutes_before_first_token_balance_increase ?? \'?\')} min before)`).join(\'<br>\');\n    else fundingCell = \'No qualifying transfer found in the sampled window\';\n    return `<tr><td><code title="${esc(x.owner)}">${esc(short(x.owner))}</code></td><td>${acquisitionCell}</td><td>${fundingCell}</td></tr>`;\n  }).join(\'\');\n\n  const funds = (cluster.shared_material_SOL_funder_candidates || []).map(x => {\n    const recipients = (x.recipients || []).map(y => `<div class="tiny muted">${esc(short(y.recipient_wallet))} · ${finite(y.sol_received) ? Number(y.sol_received).toFixed(3) + \' SOL\' : \'amount not returned\'} · slot ${esc(y.slot ?? \'unknown\')}</div>`).join(\'\');\n    return `<div class="rowline"><span><code title="${esc(x.funder)}">${esc(short(x.funder))}</code></span><strong>${count(x.recipient_count)} recipient wallets</strong></div>${recipients}`;\n  }).join(\'\');\n  const sameSlot = (cluster.same_slot_first_receipt_candidates || []).map(x => {\n    const span = finite(x.transaction_index_span) ? ` · transaction-index span ${count(x.transaction_index_span)}` : \'\';\n    const walletsInGroup = (x.wallets || []).map(y => `<code title="${esc(y.owner)}">${esc(short(y.owner))}</code>`).join(\', \');\n    return `<div class="riskline warn"><strong>Slot ${esc(x.slot)} · ${count(x.wallet_count)} wallets${span}</strong><p class="tiny muted">${esc(x.signal_strength || \'same-slot timing lead\')} — ${walletsInGroup || \'Wallets not returned\'}</p></div>`;\n  }).join(\'\');\n\n  const flags = (s.red_flags || []).map(x =>\n    `<span class="flag ${x.severity === \'high\' ? \'red\' : x.severity === \'medium\' ? \'yellow\' : \'blue\'}">${esc(x.title)}</span>`\n  ).join(\'\');\n  const liquidityValues = m.liquidity_sources || {};\n  const liquidityComparisons = Object.entries(liquidityValues)\n    .filter(([_, value]) => finite(value))\n    .map(([source, value]) => `<div class="rowline"><span>${esc(source)}</span><strong>${money(value)}</strong></div>`)\n    .join(\'\');\n  const metricQuality = m.market_metric_quality || quality.market_metric_quality || {};\n  const metricSourceRows = [\n    [\'Market cap\', \'market_cap\', m.market_cap_values_by_source || {}],\n    [\'FDV\', \'fdv\', m.fdv_values_by_source || {}],\n    [\'Token price (USD)\', \'price_usd\', m.price_usd_values_by_source || {}],\n    [\'24h price change\', \'price_change_24h\', m.price_change_values_by_source || {}],\n    [\'24h volume (provider/venue coverage can differ)\', null, m.volume_24h_values_by_source || {}]\n  ].map(([metric, qualityKey, values]) => {\n    const rows = Object.entries(values || {}).filter(([_, value]) => finite(value));\n    const qualityRow = qualityKey ? (metricQuality[qualityKey] || {}) : {};\n    const display = rows.length ? rows.map(([source, value]) => `${esc(source)}: ${metric === \'24h price change\' ? pct(value) : metric === \'Token price (USD)\' ? priceFmt(value) : money(value)}`).join(\' · \') : \'No source value returned\';\n    const conflict = qualityRow.status === \'disagreement\';\n    return `<div class="rowline"><span>${esc(metric)} provider readings ${conflict ? tag(\'DISAGREEMENT\', \'yellow\') : \'\'}</span><strong>${display}</strong></div>`;\n  }).join(\'\');\n  const creatorHistory = s.creator_history || {};\n  const creatorLaunchCount = finite(creatorHistory.reported_token_launch_count) ? count(creatorHistory.reported_token_launch_count) : \'Not returned\';\n  const creatorHistoryRows = (creatorHistory.reported_tokens || []).slice(0, 10).map(x => `<div class="rowline"><span><code>${esc(short(x.mint))}</code></span><strong>${x.createdAt ? esc(new Date(x.createdAt).toLocaleDateString()) : \'Date not returned\'}</strong></div>`).join(\'\');\n  const authority = s.authority_controls || {};\n  const authorityText = key => authority[key] === \'revoked\' ? `Revoked / none (${authority[key + \'_source\'] || \'source not returned\'})` : authority[key] === \'active\' ? `Active — authority remains (${authority[key + \'_source\'] || \'source not returned\'})` : authority[key] === \'conflict\' ? `CONFLICTING PROVIDER REPORTS (${authority[key + \'_source\'] || \'sources differ\'}) — verify on-chain` : \'Unknown / not returned\';\n  const authorityEvidenceRows = (kind, evidence) => Object.entries(evidence || {}).map(([source, item]) => `<div class="rowline"><span>${esc(source)}</span><strong>${esc(item.state || \'unknown\')}</strong></div>`).join(\'\') || \'<div class="rowline"><span>No provider returned this authority field</span><strong>Unknown</strong></div>\';\n  const sourceStatus = quality.coverage_status || \'unknown\';\n  const coverageText = sourceStatus === \'complete\' ? \'Core sources and required fields returned; sampled history still has limits.\' : sourceStatus === \'partial\' ? \'Some evidence returned, but one or more important sources or fields are missing.\' : \'Core provider coverage is unavailable or insufficient.\';\n  const riskFlagsText = (rug.flags || []).length ? rug.flags.join(\'; \') : riskListAvailable ? \'No risk entries returned in this RugCheck response\' : \'Risk list unavailable\';\n  const copiedSummary = [\n    `${tokenName} ${tokenSymbol ? \'(\' + tokenSymbol + \')\' : \'\'}`,\n    `Mint: ${d.token_address}`,\n    `Overall screen: ${statusLabel(s.overall_status)}`,\n    `Summary: ${s.executive_summary || \'No reliable conclusion.\'}`,\n    `RugCheck risk: ${rug.label || \'Not assessed\'} — ${riskFlagsText}`,\n    `Insider evidence: ${insider.label || \'Not assessed\'} — ${insider.note || \'\'}`,\n    `Bundling: ${bundle.label || \'Not assessed\'} — ${bundle.note || \'\'}`,\n    `Jito bundle evidence: ${bundle.confirmed_jito_bundle === true ? \'Direct evidence recorded\' : \'No direct Jito bundle evidence; mint-only scan cannot confirm or rule out bundling\'}`,\n    `Holder concentration: ${concentration.note || \'Not assessed\'}`,\n    `Creator launch history: ${creatorLaunchCount} launches reported by RugCheck; count is not proof those launches rugged.`,\n    `Jupiter verification: ${jupiter.is_verified === true ? \'Verified by Jupiter\' : jupiter.is_verified === false ? \'Not verified by Jupiter (not a scam finding)\' : \'Not returned\'}`,\n    `Jupiter Organic Score: ${finite(jupiter.organic_score) ? Number(jupiter.organic_score).toFixed(1) + \'/100 (relative score, not a safety score)\' : \'Not returned\'}`,\n    `Jupiter suspicious audit field: ${jupiter.suspicious_audit_flag_present === true ? \'PRESENT — provider flag; inspect raw value\' : jupiter.available ? \'Field not present in returned response\' : \'Not assessed\'}`,\n    `Market cap: ${money(m.market_cap_usd)} (${m.market_cap_source || \'source not returned\'})`,\n    `FDV: ${money(m.fdv_usd)} (${m.fdv_source || \'source not returned\'})`,\n    `Reported liquidity: ${money(m.liquidity_usd)} (${m.liquidity_source || \'source not returned\'})`,\n    `Token price: ${priceFmt(m.price_usd)} (${m.price_source || \'source not returned\'})`,\n    `Liquidity readings by source: ${Object.entries(m.liquidity_sources || {}).filter(([_, v]) => finite(v)).map(([k, v]) => k + \': \' + money(v)).join(\'; \') || \'No provider liquidity values returned\'}`,\n    `24h volume readings by source: ${Object.entries(m.volume_24h_values_by_source || {}).filter(([_, v]) => finite(v)).map(([k, v]) => k + \': \' + money(v)).join(\'; \') || \'No provider volume values returned\'}`,\n    `RugCheck reported aggregate liquidity (not live pool reserve): ${money(m.rugcheck_reported_liquidity_usd)}`,\n    `Data coverage: ${sourceStatus} — ${(quality.coverage_missing || []).join(\', \') || \'No required coverage gaps listed\'}`,\n    `Providers: ${(quality.providers || []).map(x => x.name + \': \' + x.status).join(\', \')}`,\n    `RugCheck report detected at: ${m.rugcheck_detected_at || \'not returned\'}`,\n    `Jupiter updated at: ${m.jupiter_updated_at || \'not returned\'}`,\n    `Helius last indexed slot: ${count(h.scan?.last_indexed_slot)}`,\n    `Checked: ${d.checked_at || \'timestamp not returned\'}`,\n    \'This is a screening report, not proof of safety, a rug probability or confirmation of bundled buying.\'\n  ].join(\'\\n\');\n\n  const bundleLabel = bundle.label || \'Not assessable\';\n  const bundleDisplayNote = `Confirmed Jito bundle: not established. ${bundle.note || \'No bundle conclusion can be drawn.\'}`;\n  const plainLanguageSummary = [\n    `Rug risk: ${rug.label || \'not assessable\'}. ${rug.note || \'\'}`,\n    `Insider evidence: ${insider.label || \'not assessable\'}. ${insider.note || \'\'}`,\n    `Bundling: ${bundle.label || \'not assessable\'}. A confirmed bundle has not been established.`,\n    `Data coverage: ${sourceStatus}. Missing or conflicting data is not treated as a clean result.`\n  ].join(\' \');\n  const graphCountDisclaimer = \'Graph response record count and provider insider count are separate fields; a record is not automatically an insider wallet.\';\n  const riskHeaderText = rug.label || \'Not assessed\';\n  const holderScanText = h.scan?.scan_complete === true ? \'Complete token-account scan\' : h.scan?.scan_complete === false ? \'Partial / bounded token-account scan\' : \'Scan status unknown\';\n  const jupiterVerificationText = jupiter.is_verified === true ? \'Verified by Jupiter\' : jupiter.is_verified === false ? \'Not verified by Jupiter (not a scam finding)\' : \'Not returned\';\n  const jupiterOrganicText = finite(jupiter.organic_score) ? `${Number(jupiter.organic_score).toFixed(1)} / 100 (${esc(jupiter.organic_score_label || \'label not returned\')})` : \'Not returned\';\n  const jupiterSusText = jupiter.suspicious_audit_flag_present === true ? `FIELD PRESENT — raw value: ${esc(JSON.stringify(jupiter.suspicious_audit_flag_value))}` : jupiter.available ? \'Field not present in Jupiter response\' : \'Not assessed — Jupiter data unavailable\';\n  const jupiterUpdatedText = m.jupiter_updated_at ? new Date(m.jupiter_updated_at).toLocaleString() : \'Not returned\';\n\n  $(\'results\').innerHTML = `<div class="reporthead"><div><div class="eyebrow">Investigation result</div><h2>${esc(tokenName)} <span class="muted">${esc(tokenSymbol)}</span></h2><div class="tiny muted">Mint: <code class="mono">${esc(d.token_address)}</code> · Identity source: ${esc(identitySource)}${m.identity_metadata_conflict ? \'<br><span class="tag yellow">Metadata names differ across providers</span>\' : \'\'}<br><a target="_blank" rel="noopener" href="${esc(solscan)}">Open Solscan ↗</a>${pairUrl ? \' · <a target="_blank" rel="noopener" href="\' + esc(pairUrl) + \'">Open DexScreener ↗</a>\' : \'\'}</div></div><span class="statuspill ${summaryClass}">${esc(statusLabel(s.overall_status))}</span></div>\n    <section class="summarycard ${summaryClass}"><div class="summarytop"><div><div class="eyebrow">Executive screening summary</div><h3>${esc(s.headline || statusLabel(s.overall_status))}</h3></div>${tag(s.confidence_label || \'Evidence-limited screen\', \'blue\')}</div><p class="summarytext">${esc(s.executive_summary || \'The available data does not support a reliable conclusion.\')}</p><p class="tiny muted"><strong>Quick read:</strong> ${esc(plainLanguageSummary)}</p><div class="summarygrid"><div class="summaryitem"><div class="label">Rug-risk indicators</div><strong>${esc(riskHeaderText)}</strong><div class="tiny muted">${esc(rug.note || \'\')}</div></div><div class="summaryitem"><div class="label">Insider evidence</div><strong>${esc(insider.label || \'Not assessed\')}</strong><div class="tiny muted">${esc(insider.note || \'\')}</div></div><div class="summaryitem"><div class="label">Bundling / coordination</div><strong>${esc(bundleLabel)}</strong><div class="tiny muted">${esc(bundleDisplayNote)}</div></div><div class="summaryitem"><div class="label">Data coverage</div><strong>${esc(sourceStatus.toUpperCase())}</strong><div class="tiny muted">${esc(coverageText)}</div></div></div>${flags ? `<div class="flags">${flags}</div>` : \'\'}<div class="sectionnote">A confirmed bundle is never inferred from same-slot activity alone. An empty risk/insider response is not a safety clearance. Verify high-impact findings on-chain before acting.</div></section>\n    <div class="metrics"><div class="metric"><div class="label">Market cap</div><div class="value">${money(m.market_cap_usd)}</div>${fieldSource(m.market_cap_source)}</div><div class="metric"><div class="label">FDV</div><div class="value">${money(m.fdv_usd)}</div>${fieldSource(m.fdv_source)}</div><div class="metric"><div class="label">Reported liquidity</div><div class="value">${money(m.liquidity_usd)}</div>${fieldSource(m.liquidity_source)}</div><div class="metric"><div class="label">24h volume</div><div class="value">${money(m.volume_24h_usd)}</div>${fieldSource(m.volume_source)}</div><div class="metric"><div class="label">24h price change</div><div class="value ${finite(m.price_change_24h_pct) && Number(m.price_change_24h_pct) >= 0 ? \'pos\' : finite(m.price_change_24h_pct) ? \'neg\' : \'\'}">${pct(m.price_change_24h_pct)}</div>${fieldSource(m.price_change_source)}</div><div class="metric"><div class="label">24h buys / sells</div><div class="value">${count(m.buys_24h)} / ${count(m.sells_24h)}</div>${fieldSource(m.transactions_source)}</div></div>\n    <div class="grid">${section(\'Market data and source checks\', `${liquidityComparisons || \'<p class="empty">No provider returned a liquidity/reserve figure.</p>\'}<div class="rowline"><span>Same-pool liquidity comparison</span><strong>${esc((m.same_pool_liquidity_quality || {}).status || \'Not assessed\')}</strong></div>${metricSourceRows}<div class="rowline"><span>RugCheck reported aggregate liquidity (cached/report figure)</span><strong>${money(m.rugcheck_reported_liquidity_usd)}</strong></div><div class="rowline"><span>Token price</span><strong>${priceFmt(m.price_usd)} ${fieldSource(m.price_source)}</strong></div><div class="rowline"><span>Helius cached price</span><strong>${finite(m.helius_cached_price_value) ? `${esc(m.helius_cached_price_value)} ${esc(m.helius_price_currency || \'currency not returned\')}` : \'Not returned\'}</strong></div><div class="rowline"><span>Helius token_info.supply (raw API field; not normalized)</span><strong>${finite(m.helius_supply_value_as_returned) ? count(m.helius_supply_value_as_returned) : \'Not returned\'}</strong></div><div class="rowline"><span>DexScreener matching pools</span><strong>${count(q.dexscreener_pairs_found)}</strong></div><div class="rowline"><span>GeckoTerminal pools</span><strong>${count(q.geckoterminal_pools_found)}</strong></div><div class="rowline"><span>Jupiter exact-mint record</span><strong>${jupiter.available ? \'Returned\' : \'Not returned\'}</strong></div><div class="rowline"><span>Jupiter holder count (separate provider figure)</span><strong>${count(jupiter.holder_count)}</strong></div><div class="rowline"><span>Selected DexScreener pool</span><strong><code>${esc(short(m.pair_address))}</code></strong></div><div class="rowline"><span>Selected GeckoTerminal pool</span><strong><code>${esc(short(m.gecko_pool_address))}</code></strong></div><div class="sectionnote">Market cap is never filled using FDV. Helius cached price is displayed in its returned currency and is not converted or treated as USD unless the provider explicitly says USD. Helius supply is shown as returned without decimal adjustment, so it is not used to calculate FDV. RugCheck aggregate liquidity and Jupiter token-level liquidity are not silently treated as a specific live-pool reserve. Different pool addresses are not treated as the same market. Volume sources may cover different venues and are shown separately, not averaged.</div>`)}\n    ${section(\'RugCheck security and creator risks\', `<div class="rowline"><span>RugCheck normalised score</span><strong>${finite(r.score_normalised) ? esc(r.score_normalised) : \'Not returned\'} ${tag(\'provider score; not a probability\', \'blue\')}</strong></div><div class="rowline"><span>Mint authority</span><strong>${esc(authorityText(\'mint_authority\'))}</strong></div><div class="rowline"><span>Freeze authority</span><strong>${esc(authorityText(\'freeze_authority\'))}</strong></div><div class="rowline"><span>Creator address</span><strong><code>${esc(short(creator))}</code></strong></div><div class="rowline"><span>Creator balance in holder scan</span><strong>${esc(creatorView)}</strong></div><div class="rowline"><span>Creator tokens listed by RugCheck</span><strong>${creatorLaunchCount}</strong></div>${creatorHistoryRows}<div class="tiny muted">${esc(creatorHistory.note || \'Creator token history is contextual provider data, not proof every listed token rugged.\')}</div><h4>Authority evidence by provider</h4><div class="tiny muted">Authority state is only called revoked/active when the provider returned the relevant field. Provider conflicts are not silently resolved.</div><div class="rowline"><span>Mint authority evidence</span><strong>${esc(authority.mint_authority || \'unknown\')}</strong></div>${authorityEvidenceRows(\'mint_authority\', authority.mint_authority_evidence_by_source)}<div class="rowline"><span>Freeze authority evidence</span><strong>${esc(authority.freeze_authority || \'unknown\')}</strong></div>${authorityEvidenceRows(\'freeze_authority\', authority.freeze_authority_evidence_by_source)}<h4>Jupiter token signals</h4><div class="rowline"><span>Jupiter verification</span><strong>${esc(jupiterVerificationText)}</strong></div><div class="rowline"><span>Jupiter Organic Score</span><strong>${jupiterOrganicText}</strong></div><div class="rowline"><span>Jupiter audit.isSus</span><strong>${jupiterSusText}</strong></div><div class="rowline"><span>Jupiter audit topHoldersPercentage</span><strong>${finite(jupiter.top_holders_percentage) ? pct(jupiter.top_holders_percentage) : \'Not returned\'}</strong></div><div class="rowline"><span>Jupiter data updated</span><strong>${esc(jupiterUpdatedText)}</strong></div><div class="tiny muted">Jupiter verification and Organic Score are contextual signals, not a safety certificate. Organic Score is relative to the ecosystem, not a rug probability. A not-verified result alone is not a scam finding. The audit.isSus field is shown separately from RugCheck insider tags.</div>${riskHtml}<div class="sectionnote">A danger-level flag is a serious RugCheck warning, not a calculated rug probability. If a risk list was not returned, the token is not cleared.</div>`)}\n    ${section(\'Insider tags and holder concentration\', `<div class="rowline"><span>RugCheck top holders tagged insider</span><strong>${holderSampleAvailable ? count(insider.flagged_wallet_count) : \'Not assessable\'}</strong></div>${graphInsiderLine}${graphRecordLine}<p class="tiny muted">${esc(graphCountDisclaimer)}</p>${insiderTagText}<h4>Largest holders not labelled by RugCheck as AMM / pool / vault</h4>${concentrationView}<div class="sectionnote">A wallet is called a provider-tagged insider only when RugCheck explicitly flags it. A large holder is a concentration signal, not automatically an insider. Concentration among owners not identified by RugCheck as pools is shown only for a complete Helius scan and complete owner list; unlabelled program accounts may remain. ${esc(concentration.note || \'\')}</div><div class="tablewrap"><table><thead><tr><th>Largest scanned owner</th><th>Share of total scanned balances</th></tr></thead><tbody>${holderRows || \'<tr><td colspan="2">No holder rows returned.</td></tr>\'}</tbody></table></div>`)}\n    ${section(\'Bundling and coordinated-launch checks\', `<div class="rowline"><span>Confirmed Jito bundle</span><strong>${bundle.confirmed_jito_bundle === true ? \'Direct bundle evidence returned\' : \'NOT ESTABLISHED\'}</strong></div><div class="rowline"><span>Same-slot first balance-change groups</span><strong>${count(bundle.same_slot_group_count)}</strong></div><div class="rowline"><span>Shared material SOL-funder candidates</span><strong>${count(bundle.shared_funder_count)}</strong></div><div class="rowline"><span>Sampled wallets with positive balance-change anchors</span><strong>${count(bundle.wallets_with_positive_balance_event)} / ${count(bundle.wallets_sampled)}</strong></div><div class="rowline"><span>Wallet history errors</span><strong>${count(bundle.wallet_history_errors)}</strong></div><p class="tiny muted">${esc(bundle.detailed_note || \'History is bounded; no definitive bundle conclusion is available.\')}</p>${sameSlot || \'<p class="empty">No same-slot group was found in the sampled wallet histories.</p>\'}${funds || \'<p class="empty">No shared material-funder candidate was found in the sampled wallet histories.</p>\'}<div class="sectionnote">This tool does not discover Jito bundle IDs from a mint. Same-slot changes and shared funders are leads only; they do not prove bundled buying or common ownership. “No candidate found” does not mean “not bundled.”</div>`)}\n    ${section(\'Wallet transaction history\', `<div class="rowline"><span>Wallets analysed</span><strong>${count(q.helius_wallets_analysed)}</strong></div><div class="rowline"><span>Holder scan</span><strong>${esc(holderScanText)}</strong></div><div class="tablewrap"><table><thead><tr><th>Wallet</th><th>First detected positive token balance change</th><th>Pre-event funding ≥ 0.01 SOL</th></tr></thead><tbody>${walletRows || \'<tr><td colspan="3">No wallet history returned.</td></tr>\'}</tbody></table></div><div class="sectionnote">A positive token-balance change may be a buy, transfer, distribution or other movement. The wallet-history search is bounded; a missing event or missing funding transfer is inconclusive, not proof none occurred.</div>`)}\n    ${section(\'Social links, narrative and paid promotion\', `<div>${socialText(m.social_links)}</div>${(m.websites || []).length ? `<p>${m.websites.map(x => x.url && /^https:\\/\\//.test(x.url) ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.label || x.url)} ↗</a>` : esc(x.label || \'Website\')).join(\' · \')}</p>` : \'<p class="empty">No website link returned.</p>\'}<div class="rowline"><span>Approved DexScreener paid orders</span><strong>${count(m.approved_paid_order_count)}</strong></div><div class="rowline"><span>Active boosts</span><strong>${count(m.active_dex_boosts)}</strong></div><div class="sectionnote">Promotion, social links, volume and price movement do not establish organic community interest, follower growth or sentiment.</div>`)}\n    ${section(\'Data quality and coverage\', `<div class="rowline"><span>Coverage status</span><strong>${esc(sourceStatus.toUpperCase())}</strong></div><p class="muted tiny">${esc(coverageText)}</p><div class="sourcegrid">${providerRows || \'<p class="empty">Provider status not returned.</p>\'}</div><div class="rowline"><span>Report generated</span><strong>${esc(d.checked_at || \'Timestamp unavailable\')}</strong></div><div class="rowline"><span>RugCheck report detected at</span><strong>${esc(m.rugcheck_detected_at || \'Not returned\')}</strong></div><div class="rowline"><span>Jupiter data updated at</span><strong>${esc(jupiterUpdatedText)}</strong></div><div class="rowline"><span>Token identity source</span><strong>${esc(identitySource)}</strong></div><div class="rowline"><span>Holder scan last indexed slot</span><strong>${count(h.scan?.last_indexed_slot)}</strong></div><div class="rowline"><span>Missing market fields</span><strong>${(quality.missing_market_fields || []).length ? quality.missing_market_fields.map(esc).join(\', \') : \'No missing fields listed\'}</strong></div><div class="rowline"><span>Other coverage gaps</span><strong>${(quality.coverage_missing || []).length ? quality.coverage_missing.map(esc).join(\', \') : \'No coverage gaps listed\'}</strong></div>${m.identity_metadata_conflict ? `<div class="riskline warn"><strong>Token metadata differs between providers</strong><p class="tiny muted">${esc(JSON.stringify(m.identity_values_by_source || {}))}</p></div>` : \'\'}<div class="sectionnote">Values are shown with their provider source. Missing data is unknown. Different providers may index different pools or timestamps. No blank field is converted to zero or treated as a clean result.</div>`)}\n    <div class="actions"><button class="action" id="copySummary" type="button">Copy summary</button><button class="action secondary" id="newScan" type="button">Analyse another token</button></div><details class="accordion"><summary>Show raw technical response (for debugging)</summary><pre id="raw"></pre></details>`;\n  $(\'raw\').textContent = JSON.stringify(d, null, 2);\n  $(\'results\').style.display = \'block\';\n  $(\'copySummary\').onclick = async () => {\n    try {\n      await navigator.clipboard.writeText(copiedSummary);\n      $(\'copySummary\').textContent = \'Summary copied\';\n      setTimeout(() => $(\'copySummary\').textContent = \'Copy summary\', 1800);\n    } catch (error) {\n      $(\'copySummary\').textContent = \'Copy unavailable\';\n    }\n  };\n  $(\'newScan\').onclick = () => { $(\'mint\').focus(); window.scrollTo({top: 0, behavior: \'smooth\'}); };\n}\n\n$(\'form\').addEventListener(\'submit\',async e=>{e.preventDefault();$(\'error\').style.display=\'none\';$(\'results\').style.display=\'none\';const mint=$(\'mint\').value.trim();if(!mintPattern.test(mint)){$(\'error\').textContent=\'That does not look like a valid Solana mint address. Check the address and try again.\';$(\'error\').style.display=\'block\';return}if($(\'run\').disabled)return;$(\'run\').disabled=true;$(\'run\').textContent=\'Analysing…\';$(\'status\').style.display=\'block\';let i=0;$(\'statusText\').textContent=busyMessages[0];progressTimer=setInterval(()=>{$(\'statusText\').textContent=busyMessages[++i%busyMessages.length]},5500);try{const response=await fetch(\'/api/analyze\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({token_address:mint}),signal:AbortSignal.timeout(300000)});const data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);if(data.error&&!data.report_type)throw new Error(data.error);render(data);$(\'results\').scrollIntoView({behavior:\'smooth\',block:\'start\'})}catch(err){$(\'error\').textContent=(err.name===\'TimeoutError\'?\'The analysis took too long. Please try again.\':err.message||\'The analysis failed. Please retry.\');$(\'error\').style.display=\'block\'}finally{clearInterval(progressTimer);$(\'status\').style.display=\'none\';$(\'run\').disabled=false;$(\'run\').innerHTML=\'Analyse token <span aria-hidden="true">→</span>\'}});\n\n</script></body></html>\n'
_analysis_lock = asyncio.Lock()


@mcp.custom_route("/", methods=["GET"])
async def dashboard_home(request: Request):
    return HTMLResponse(DASHBOARD_HTML)


@mcp.custom_route("/api/analyze", methods=["POST"])
async def dashboard_analyze(request: Request):
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > 4096:
        return JSONResponse({"error": "Request too large."}, status_code=413)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Send a JSON body containing token_address."}, status_code=400)
    token_address = str(payload.get("token_address", "")).strip() if isinstance(payload, dict) else ""
    if not valid_solana_mint(token_address):
        return JSONResponse({"error": "Invalid Solana mint format. Supply a 32-44 character base58 address."}, status_code=400)
    if _analysis_lock.locked():
        return JSONResponse({"error": "An analysis is already running. Please wait for it to finish."}, status_code=429)
    async with _analysis_lock:
        try:
            result = await analyze_solana_memecoin.fn(token_address, max_wallets=15)
            return JSONResponse(result)
        except Exception as exc:
            return JSONResponse({"error": "The analysis could not be completed.", "details": str(exc)[:500]}, status_code=500)


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        path="/mcp",
    )
