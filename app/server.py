import os
import re
import httpx
from fastmcp import FastMCP

mcp = FastMCP(
    name="Memecoin Research - DexScreener + RugCheck",
    instructions=(
        "Read-only Solana token research tools. DexScreener provides market/pair data. "
        "RugCheck provides heuristic security reports and may return cached reports on "
        "the free tier. Never treat any score as proof that a token is safe. "
        "Wallet links or clusters are indicators, not proof of common ownership. "
        "Report missing or unavailable fields rather than guessing."
    ),
)

DEXSCREENER_BASE = "https://api.dexscreener.com"
RUGCHECK_BASE = "https://api.rugcheck.xyz"
SOLANA_MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def valid_solana_mint(token_address: str) -> bool:
    """Basic base58-format validation; this does not prove the mint exists."""
    return bool(token_address and SOLANA_MINT_RE.fullmatch(token_address))


async def get_json(url: str, headers: dict | None = None):
    timeout = httpx.Timeout(20.0, connect=8.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": "memecoin-research-mcp/1.1", **(headers or {})},
        follow_redirects=True,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


def rugcheck_headers() -> dict:
    """Use a RugCheck/FluxRPC key only if the owner configures one in Render."""
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
                "RugCheck's free API may serve cached reports. Check timestamps "
                "in the returned data where available."
            ),
            "interpretation_note": (
                "RugCheck scores are heuristic screening signals, not guarantees "
                "of safety. Review the individual risks and underlying evidence."
            ),
        }
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        try:
            details = exc.response.text[:800]
        except Exception:
            details = "No response body available."
        hints = {
            401: "The endpoint may require an API key or authentication.",
            403: "The request may be blocked or the endpoint may require a higher access tier.",
            404: "No RugCheck report may be available for this mint yet, or the route may have changed.",
            429: "RugCheck rate limit reached. Wait before retrying.",
        }
        return {
            "source": "RugCheck",
            "error": "RugCheck request failed.",
            "http_status": status,
            "hint": hints.get(status, "Check the endpoint and try again later."),
            "details": details,
        }
    except httpx.RequestError as exc:
        return {
            "source": "RugCheck",
            "error": "Could not connect to RugCheck.",
            "details": str(exc)[:500],
        }
    except ValueError as exc:
        return {
            "source": "RugCheck",
            "error": "RugCheck returned a response that was not valid JSON.",
            "details": str(exc)[:500],
        }


@mcp.tool()
async def get_solana_token_market_data(token_address: str) -> dict:
    """Fetch DexScreener pools and market metrics for a Solana token mint address."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format. Supply a 32-44 character base58 address."}

    try:
        data = await get_json(
            f"{DEXSCREENER_BASE}/token-pairs/v1/solana/{token_address}"
        )
    except httpx.HTTPStatusError as exc:
        return {
            "source": "DexScreener",
            "error": "DexScreener request failed.",
            "http_status": exc.response.status_code,
            "details": exc.response.text[:500],
        }
    except httpx.RequestError as exc:
        return {"source": "DexScreener", "error": "Could not connect to DexScreener.", "details": str(exc)[:500]}

    if not isinstance(data, list) or not data:
        return {"token_address": token_address, "status": "no_pairs_found", "pairs": []}

    pairs = sorted(
        data,
        key=lambda pair: (pair.get("liquidity") or {}).get("usd") or 0,
        reverse=True,
    )
    return {
        "token_address": token_address,
        "source": "DexScreener public API",
        "pair_count": len(pairs),
        "pairs": pairs[:20],
        "limitations": [
            "DexScreener market data does not establish wallet ownership or insider links.",
            "Bundled-launch analysis requires transaction-level blockchain data.",
            "Liquidity lock/burn status and token authorities require independent verification.",
        ],
    }


@mcp.tool()
async def get_solana_token_paid_orders(token_address: str) -> dict:
    """Check DexScreener paid promotion/order status for a Solana token."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format. Supply a 32-44 character base58 address."}

    try:
        data = await get_json(
            f"{DEXSCREENER_BASE}/orders/v1/solana/{token_address}"
        )
        return {"token_address": token_address, "source": "DexScreener public API", "orders": data}
    except httpx.HTTPStatusError as exc:
        return {
            "source": "DexScreener",
            "error": "Paid-order lookup failed.",
            "http_status": exc.response.status_code,
            "details": exc.response.text[:500],
        }
    except httpx.RequestError as exc:
        return {"source": "DexScreener", "error": "Could not connect to DexScreener.", "details": str(exc)[:500]}


@mcp.tool()
async def get_latest_solana_boosts() -> dict:
    """Fetch the latest DexScreener boosted-token feed and filter to Solana."""
    try:
        data = await get_json(f"{DEXSCREENER_BASE}/token-boosts/latest/v1")
    except httpx.HTTPStatusError as exc:
        return {
            "source": "DexScreener",
            "error": "Boost lookup failed.",
            "http_status": exc.response.status_code,
            "details": exc.response.text[:500],
        }
    except httpx.RequestError as exc:
        return {"source": "DexScreener", "error": "Could not connect to DexScreener.", "details": str(exc)[:500]}

    if not isinstance(data, list):
        return {"source": "DexScreener", "items": []}
    return {
        "source": "DexScreener public API",
        "items": [item for item in data if item.get("chainId") == "solana"][:100],
    }


@mcp.tool()
async def get_rugcheck_token_report(token_address: str) -> dict:
    """Fetch RugCheck's full Solana token security report: risks, authorities, markets and holder data where available."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format. Supply a 32-44 character base58 address."}
    result = await fetch_rugcheck(f"/v1/tokens/{token_address}/report")
    result["token_address"] = token_address
    result["report_type"] = "full"
    return result


@mcp.tool()
async def get_rugcheck_token_report_summary(token_address: str) -> dict:
    """Fetch RugCheck's concise token risk summary, including normalized score and risk flags where available."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format. Supply a 32-44 character base58 address."}
    result = await fetch_rugcheck(f"/v1/tokens/{token_address}/report/summary")
    result["token_address"] = token_address
    result["report_type"] = "summary"
    return result


@mcp.tool()
async def get_rugcheck_insider_graph(token_address: str) -> dict:
    """Fetch RugCheck's detected insider-network graph, including trade/transfer clusters where available."""
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format. Supply a 32-44 character base58 address."}
    result = await fetch_rugcheck(f"/v1/tokens/{token_address}/insiders/graph")
    result["token_address"] = token_address
    result["interpretation_note"] = (
        "Network membership indicates an algorithmically detected relationship, not definitive proof "
        "that the accounts share one owner. Inspect graph nodes, links and transaction evidence."
    )
    return result


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        path="/mcp",
    )
