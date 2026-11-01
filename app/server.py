
import os
import httpx
from fastmcp import FastMCP

mcp = FastMCP(
    name="Memecoin Research - DexScreener",
    instructions=(
        "Read-only DexScreener market data. "
        "Never infer wallet ownership or token safety "
        "from DexScreener alone."
    ),
)

BASE = "https://api.dexscreener.com"


async def get_json(url: str):
    timeout = httpx.Timeout(15.0, connect=8.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


@mcp.tool()
async def get_solana_token_market_data(token_address: str) -> dict:
    """Retrieve DexScreener market data for a Solana token."""

    if not token_address or not (32 <= len(token_address) <= 44):
        return {"error": "Invalid Solana token address."}

    data = await get_json(
        f"{BASE}/token-pairs/v1/solana/{token_address}"
    )

    if not isinstance(data, list) or not data:
        return {
            "token_address": token_address,
            "status": "no_pairs_found",
            "pairs": []
        }

    pairs = sorted(
        data,
        key=lambda p: (p.get("liquidity") or {}).get("usd") or 0,
        reverse=True
    )

    return {
        "token_address": token_address,
        "source": "DexScreener",
        "pair_count": len(pairs),
        "pairs": pairs[:20],
        "limitations": [
            "Wallet ownership cannot be established from DexScreener.",
            "Bundled supply requires transaction analysis.",
            "Liquidity locks require independent verification."
        ]
    }


@mcp.tool()
async def get_solana_token_paid_orders(token_address: str) -> dict:
    """Check paid promotions for a Solana token."""

    if not token_address or not (32 <= len(token_address) <= 44):
        return {"error": "Invalid Solana token address."}

    data = await get_json(
        f"{BASE}/orders/v1/solana/{token_address}"
    )

    return {
        "token_address": token_address,
        "orders": data
    }


@mcp.tool()
async def get_latest_solana_boosts() -> dict:
    """Retrieve recently boosted Solana tokens."""

    data = await get_json(
        f"{BASE}/token-boosts/latest/v1"
    )

    if not isinstance(data, list):
        return {"items": []}

    return {
        "items": [
            item for item in data
            if item.get("chainId") == "solana"
        ][:100]
    }


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        path="/mcp"
    )
