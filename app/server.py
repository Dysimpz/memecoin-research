import os
import re
import asyncio
import httpx
from collections import defaultdict
from fastmcp import FastMCP

mcp = FastMCP(
    name="Memecoin Research - DexScreener + RugCheck + Helius",
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
    limit: int = 25,
    sort_order: str = "asc",
    mint_filter: str | None = None,
    max_pages: int = 1,
) -> dict:
    """
    Fetch Helius full transaction history.

    Important: Helius rejects combining tokenTransfer with tokenAccounts='all'.
    This implementation requests wallet + token-account history with tokenAccounts='all'
    and, when a mint is requested, filters the returned full transactions locally.
    """
    limit = max(1, min(int(limit), 100))
    max_pages = max(1, min(int(max_pages), 10))
    all_transactions = []
    matching_transactions = []
    pagination_token = None
    pages_read = 0

    for _ in range(max_pages):
        filters = {"status": "succeeded", "tokenAccounts": "all"}
        options = {
            "transactionDetails": "full",
            "sortOrder": sort_order,
            "limit": limit,
            "filters": filters,
            "maxSupportedTransactionVersion": 0,
        }
        if pagination_token:
            options["paginationToken"] = pagination_token

        response = await helius_rpc(
            "getTransactionsForAddress", [address, options]
        )
        if response.get("error"):
            return {
                **response,
                "data": matching_transactions if mint_filter else all_transactions,
                "pages_read": pages_read,
                "raw_transactions_scanned": len(all_transactions),
            }

        result = response["result"]
        batch = result.get("data") or []
        pages_read += 1
        all_transactions.extend(batch)

        if mint_filter:
            matching = [
                tx for tx in batch
                if transaction_mentions_mint(tx, mint_filter)
            ]
            matching_transactions.extend(matching)
            # Ascending order: the first page with the mint contains its earliest
            # observed occurrence within the scanned history window.
            if matching:
                pagination_token = result.get("paginationToken")
                break
        else:
            matching_transactions.extend(batch)

        pagination_token = result.get("paginationToken")
        if not pagination_token or not batch:
            break

    return {
        "data": matching_transactions if mint_filter else all_transactions,
        "pagination_token": pagination_token,
        "pages_read": pages_read,
        "raw_transactions_scanned": len(all_transactions),
        "mint_filter_mode": (
            "client_side_filter_after_tokenAccounts_all"
            if mint_filter else "not_applied"
        ),
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


async def scan_one_holder(owner: str, mint: str, amount_raw: int, semaphore: asyncio.Semaphore) -> dict:
    async with semaphore:
        token_history = await helius_wallet_transactions(
            owner, limit=100, sort_order="asc", mint_filter=mint, max_pages=5
        )
    if token_history.get("error"):
        return {
            "owner": owner,
            "raw_amount": amount_raw,
            "token_history_error": token_history,
            "first_inbound_token_transaction": None,
            "funding_sources": [],
        }

    txs = token_history.get("data") or []
    first_buy = None
    for tx in txs:
        delta = extract_token_balance_increase(tx, mint, owner)
        # If metadata omits owner-level token balance rows, retain the first filtered
        # transaction as an observed inbound token event, but do not call it a buy.
        if delta > 0 or first_buy is None:
            transaction = tx.get("transaction") or {}
            first_buy = {
                "signature": (transaction.get("signatures") or [None])[0],
                "slot": tx.get("slot"),
                "transaction_index": tx.get("transactionIndex"),
                "block_time": tx.get("blockTime"),
                "owner_token_balance_increase_raw": delta if delta > 0 else None,
                "classification": (
                    "confirmed_positive_balance_change"
                    if delta > 0
                    else "inbound_token_transfer_record_unverified_as_buy"
                ),
            }
            break

    # Query a small oldest-first window for initial SOL funders.
    async with semaphore:
        funding_history = await helius_wallet_transactions(
            owner, limit=25, sort_order="asc", mint_filter=None
        )

    funders = []
    if not funding_history.get("error"):
        for tx in funding_history.get("data") or []:
            funders.extend(extract_incoming_sol_transfers(tx, owner))
            if len(funders) >= 5:
                break

    return {
        "owner": owner,
        "raw_amount": amount_raw,
        "first_inbound_token_transaction": first_buy,
        "funding_sources": funders[:5],
        "token_history_matching_transactions_returned": len(txs),
        "token_history_raw_transactions_scanned": token_history.get("raw_transactions_scanned"),
        "token_history_pages_read": token_history.get("pages_read"),
        "token_history_scan_complete": token_history.get("history_scan_complete"),
        "token_history_filter_mode": token_history.get("mint_filter_mode"),
        "funding_history_transactions_returned": len(funding_history.get("data") or []),
        "funding_history_error": funding_history.get("error"),
    }


def cluster_early_buys(wallet_analyses: list[dict], holder_lookup: dict[str, dict]) -> dict:
    by_slot = defaultdict(list)
    by_funder = defaultdict(list)

    for analysis in wallet_analyses:
        owner = analysis.get("owner")
        acquisition = analysis.get("first_inbound_token_transaction")
        holder_share = holder_lookup.get(owner, {}).get("share_of_scanned_balances_pct", 0)

        if acquisition and acquisition.get("slot") is not None:
            by_slot[str(acquisition["slot"])].append({
                "owner": owner,
                "transaction_index": acquisition.get("transaction_index"),
                "signature": acquisition.get("signature"),
                "share_of_scanned_balances_pct": holder_share,
                "classification": acquisition.get("classification"),
            })

        for funding in analysis.get("funding_sources") or []:
            funder = funding.get("from")
            if funder:
                by_funder[funder].append({
                    "recipient_wallet": owner,
                    "sol_received": funding.get("lamports", 0) / 1_000_000_000,
                    "signature": funding.get("signature"),
                    "slot": funding.get("slot"),
                })

    same_slot = []
    for slot, wallets in by_slot.items():
        if len(wallets) >= 2:
            same_slot.append({
                "slot": slot,
                "wallet_count": len(wallets),
                "combined_share_of_scanned_balances_pct": sum(
                    item["share_of_scanned_balances_pct"] for item in wallets
                ),
                "wallets": wallets,
                "signal_strength": "weak_to_moderate_timing_signal",
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
        "shared_initial_SOL_funder_candidates": common_funders[:20],
        "interpretation": (
            "Same-slot token receipts and shared SOL funding sources are leads for manual "
            "transaction review, not proof of bundling or common ownership. Exchanges, market "
            "makers, relayers, launchpad programs and custodians can legitimately fund many "
            "wallets. A Jito bundle requires bundle/transaction evidence beyond same-slot timing."
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
    selected = [h for h in top_holders if h["owner"] not in pool_like][:max_wallets]
    share_lookup = {h["owner"]: h for h in top_holders}

    semaphore = asyncio.Semaphore(4)
    wallet_analyses = await asyncio.gather(*[
        scan_one_holder(h["owner"], token_address, h["raw_amount"], semaphore)
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
        "holder_distribution": holders["top_holders"][:50],
        "wallets_analysed": len(wallet_analyses),
        "wallet_analyses": wallet_analyses,
        "cluster_candidates": clusters,
        "limitations": [
            "This is a bounded sample of the largest holders, not every holder.",
            "Token-mint filtering is performed locally after fetching full wallet/token-account history; up to five pages of 100 transactions are sampled per wallet.",
            "If no token-mint transaction appears in the bounded history sample, that is inconclusive and not proof the wallet never acquired the token.",
            "First observed incoming token transfer may be a transfer, not a DEX purchase.",
            "Same-slot activity is only a timing signal; it does not prove a Jito bundle.",
            "Shared SOL funders may be exchanges, custodians, relayers, launchpad services or other common infrastructure.",
            "Transaction history may be truncated by the configured per-wallet limit.",
            "A token account owner can be a program/AMM vault rather than an individual trader.",
        ],
    }


if __name__ == "__main__":
    mcp.run(
        transport="http",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        path="/mcp",
    )
