import os
import re
import asyncio
import httpx
from collections import defaultdict
from fastmcp import FastMCP

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
