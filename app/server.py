import os
import re
import asyncio
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
        "holder_distribution": holders["top_holders"][:50],
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
    """Run the complete combined Solana memecoin investigation in one call.

    Combines DexScreener market/pair metadata and paid promotion orders, RugCheck's
    full security report and insider graph, and Helius holder distribution plus
    bounded wallet-funding/launch-timing analysis. This is the recommended one-call
    tool for each new token mint.
    """
    if not valid_solana_mint(token_address):
        return {"error": "Invalid Solana mint format. Supply a 32-44 character base58 address."}

    max_wallets = max(3, min(int(max_wallets), 20))
    market, paid_orders, rugcheck_report, insider_graph, wallet_analysis = await asyncio.gather(
        get_solana_token_market_data.fn(token_address),
        get_solana_token_paid_orders.fn(token_address),
        get_rugcheck_token_report.fn(token_address),
        get_rugcheck_insider_graph.fn(token_address),
        analyze_solana_token_wallet_clusters.fn(token_address, max_wallets=max_wallets, max_pages=5),
        return_exceptions=True,
    )

    def safe_result(value, source_name: str):
        if isinstance(value, Exception):
            return {"source": source_name, "error": str(value)[:500]}
        return value

    market = safe_result(market, "DexScreener")
    paid_orders = safe_result(paid_orders, "DexScreener paid orders")
    rugcheck_report = safe_result(rugcheck_report, "RugCheck full report")
    insider_graph = safe_result(insider_graph, "RugCheck insider graph")
    wallet_analysis = safe_result(wallet_analysis, "Helius wallet analysis")

    pairs = market.get("pairs", []) if isinstance(market, dict) else []
    primary_pair = pairs[0] if pairs else {}
    info = primary_pair.get("info") or {}
    volume = primary_pair.get("volume") or {}
    txns = primary_pair.get("txns") or {}
    liquidity = primary_pair.get("liquidity") or {}
    boosts = primary_pair.get("boosts") or {}

    orders = paid_orders.get("orders", []) if isinstance(paid_orders, dict) else []
    active_order_count = sum(
        1 for order in orders
        if isinstance(order, dict) and order.get("status") == "approved"
    ) if isinstance(orders, list) else None

    holder_data = wallet_analysis if isinstance(wallet_analysis, dict) else {}
    cluster_summary = holder_data.get("cluster_candidates", {})
    holder_scan = holder_data.get("scan", {})

    return {
        "report_type": "combined_solana_memecoin_investigation",
        "token_address": token_address,
        "market_and_narrative_inputs": {
            "token_name": (primary_pair.get("baseToken") or {}).get("name"),
            "token_symbol": (primary_pair.get("baseToken") or {}).get("symbol"),
            "pair_url": primary_pair.get("url"),
            "description": info.get("description"),
            "websites": info.get("websites") or [],
            "social_links": info.get("socials") or [],
            "market_cap_usd": primary_pair.get("marketCap") or primary_pair.get("fdv"),
            "fdv_usd": primary_pair.get("fdv"),
            "liquidity_usd": liquidity.get("usd"),
            "volume_24h_usd": volume.get("h24"),
            "buys_24h": (txns.get("h24") or {}).get("buys"),
            "sells_24h": (txns.get("h24") or {}).get("sells"),
            "price_change_24h_pct": (primary_pair.get("priceChange") or {}).get("h24"),
            "pair_created_at_ms": primary_pair.get("pairCreatedAt"),
            "active_dex_boosts": boosts.get("active"),
            "dex_paid_orders": orders,
            "approved_paid_order_count": active_order_count,
            "social_attention_caveat": (
                "DexScreener supplies token description and linked social accounts plus paid-promotion/boost indicators. "
                "It does not provide reliable follower growth, organic engagement, mention velocity or sentiment here. "
                "Those require separate social-platform data or live research; paid promotion is not organic demand."
            ),
        },
        "rugcheck_full_security_report": rugcheck_report,
        "rugcheck_insider_network_graph": insider_graph,
        "helius_holder_and_wallet_analysis": holder_data,
        "quick_status": {
            "dexscreener_pairs_found": len(pairs),
            "rugcheck_report_available": not bool(rugcheck_report.get("error")) if isinstance(rugcheck_report, dict) else False,
            "rugcheck_insider_graph_available": not bool(insider_graph.get("error")) if isinstance(insider_graph, dict) else False,
            "helius_holder_scan_complete": holder_scan.get("scan_complete"),
            "helius_wallets_analysed": holder_data.get("wallets_analysed"),
            "same_slot_candidates": len(cluster_summary.get("same_slot_first_receipt_candidates", [])),
            "shared_material_funder_candidates": len(cluster_summary.get("shared_material_SOL_funder_candidates", [])),
        },
        "interpretation_limits": [
            "Holder percentages are based on scanned token-account balances; pool/AMM vaults must be classified separately.",
            "Helius wallet history is bounded to up to ten pages of 100 transactions for each sampled holder.",
            "A positive token balance change may be a buy, transfer, distribution or other token movement; it is not automatically a purchase.",
            "Same-slot activity is not proof of a Jito bundle. Jito's getBundleStatuses checks known bundle IDs; it does not discover an unknown bundle ID from a token mint.",
            "Common funding wallets are only candidate links, even after filtering dust transfers and anchoring transfers to a pre-acquisition time window.",
            "Narrative strength, organic community quality and social sentiment are not scored from token metadata alone.",
            "Missing data or provider errors must never be interpreted as proof of safety.",
        ],
    }


# Simple browser dashboard served by the same Render service.
DASHBOARD_HTML = '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">\n<title>Solana Memecoin Research</title>\n<style>\n:root{color-scheme:dark;--bg:#0a0f18;--panel:#111a28;--panel2:#0e1724;--line:#243247;--muted:#92a2b8;--text:#edf3fb;--green:#55d6a0;--red:#ff737d;--yellow:#f4c66b;--blue:#83b8ff}\n*{box-sizing:border-box}body{margin:0;background:radial-gradient(900px 500px at 85% -10%,#1b3150 0%,transparent 60%),var(--bg);color:var(--text);font:15px/1.55 Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif}\n.wrap{max-width:1120px;margin:0 auto;padding:36px 22px 70px}.top{display:flex;align-items:center;gap:12px;margin-bottom:34px}.mark{width:42px;height:42px;border-radius:13px;background:linear-gradient(135deg,#7ab6ff,#7b75f7);display:grid;place-items:center;color:#07111e;font-weight:900;font-size:20px}.eyebrow{color:var(--blue);font-size:11px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}.top h1{font-size:19px;margin:0;letter-spacing:-.02em}.top .sub{font-size:12px;color:var(--muted)}\n.hero{max-width:760px;margin:0 auto 26px;text-align:center}.hero h2{font-size:clamp(28px,5vw,42px);line-height:1.12;letter-spacing:-.045em;margin:0 0 12px}.hero p{color:var(--muted);margin:0 auto 24px;max-width:620px}\n.search{display:flex;gap:10px;background:#111a28;border:1px solid #2a3b54;padding:9px;border-radius:16px;box-shadow:0 18px 50px #0005}.search input{flex:1;min-width:0;background:transparent;border:0;outline:0;color:var(--text);font:inherit;padding:12px}.search button,.action{border:0;border-radius:11px;padding:12px 19px;background:#8abaff;color:#081321;font-weight:800;font:inherit;cursor:pointer;white-space:nowrap}.search button:hover,.action:hover{filter:brightness(1.08)}button:disabled{opacity:.55;cursor:wait}.hint{color:var(--muted);font-size:12px;margin-top:10px}\n#status{display:none;margin:22px auto;max-width:760px;border:1px solid var(--line);border-radius:12px;padding:13px 16px;color:var(--muted);background:#111a28}.spinner{display:inline-block;width:13px;height:13px;border:2px solid #41536b;border-top-color:var(--blue);border-radius:50%;animation:spin .8s linear infinite;vertical-align:-2px;margin-right:8px}@keyframes spin{to{transform:rotate(360deg)}}\n#error{display:none;max-width:760px;margin:18px auto;background:#351b25;border:1px solid #74404c;color:#ffdce0;padding:14px;border-radius:12px}\n#results{display:none;margin-top:34px}.reporthead{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin:0 0 18px}.reporthead h2{font-size:26px;line-height:1.15;margin:0 0 6px}.muted{color:var(--muted)}.tiny{font-size:12px}.statuspill{border:1px solid var(--line);border-radius:999px;padding:6px 11px;font-size:12px;color:var(--muted);white-space:nowrap}.statuspill.danger{border-color:#793f49;background:#351b25;color:#ffb6bd}.statuspill.warn{border-color:#705b32;background:#2c2518;color:#f6d58d}.statuspill.good{border-color:#27664e;background:#122b23;color:#8be7bd}\n.metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:16px 0 24px}.metric,.panel{background:linear-gradient(180deg,#131e2d,#101925);border:1px solid var(--line);border-radius:15px}.metric{padding:15px}.metric .label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}.metric .value{font-size:22px;font-weight:780;letter-spacing:-.03em;margin-top:5px;overflow-wrap:anywhere}.pos{color:var(--green)}.neg{color:var(--red)}\n.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.panel{padding:18px;margin-bottom:14px;min-width:0}.panel h3{font-size:15px;margin:0 0 14px}.panel p{margin:7px 0}.risk{border-left:3px solid var(--red)}.risk.warn{border-left-color:var(--yellow)}.risk.good{border-left-color:var(--green)}.rowline{display:flex;justify-content:space-between;gap:14px;padding:9px 0;border-bottom:1px solid #223044}.rowline:last-child{border-bottom:0}.rowline span:first-child{color:var(--muted)}.rowline strong{text-align:right;overflow-wrap:anywhere}.tag{display:inline-block;padding:3px 8px;margin:3px 4px 3px 0;border-radius:999px;background:#1b2a3e;color:#bdd8ff;font-size:11px}.tag.red{background:#3b2029;color:#ffb8bf}.tag.yellow{background:#392f1b;color:#f5d68d}.tag.green{background:#153327;color:#98e9c3}\n.tablewrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:10px 9px;border-bottom:1px solid #263449;vertical-align:top}th{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);font-weight:700}td code{font-size:11px;color:#bdd8ff;overflow-wrap:anywhere}a{color:#9bc5ff;text-decoration:none}a:hover{text-decoration:underline}.empty{color:var(--muted);font-size:13px}.sectionnote{border-top:1px solid var(--line);padding-top:12px;margin-top:12px;color:var(--muted);font-size:12px}.actions{display:flex;gap:8px;flex-wrap:wrap}.action.secondary{background:#1b2a3e;color:#d9e8fb;border:1px solid #334761}.accordion{margin-top:14px}.accordion summary{cursor:pointer;color:#aecfff;font-size:13px}.accordion pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:520px;overflow:auto;background:#090e16;border:1px solid var(--line);border-radius:10px;padding:14px;color:#bfd0e5;font-size:11px}\nfooter{margin-top:38px;color:#71839b;font-size:11px;text-align:center}\n@media(max-width:800px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.grid{grid-template-columns:1fr}.reporthead{flex-direction:column}.search{flex-direction:column}.search button{width:100%}}\n</style>\n</head>\n<body><main class="wrap">\n<header class="top"><div class="mark">M</div><div><div class="eyebrow">Research terminal</div><h1>Solana Memecoin Research</h1><div class="sub">DexScreener · RugCheck · Helius</div></div></header>\n<section class="hero"><h2>Investigate a token.<br>See the signals clearly.</h2><p>Enter a Solana mint address to run the combined market, security, holder and wallet-history analysis.</p>\n<form id="form" class="search"><input id="mint" autocomplete="off" spellcheck="false" placeholder="Paste Solana token mint address" aria-label="Solana token mint address" required><button id="run" type="submit">Analyse token <span aria-hidden="true">→</span></button></form><div class="hint">Read-only research. Never enter a seed phrase or private key.</div></section>\n<div id="status" role="status"><span class="spinner"></span><span id="statusText">Running investigation…</span></div><div id="error" role="alert"></div>\n<section id="results" aria-live="polite"></section>\n<footer>Independent screening signals, not financial advice. Missing or incomplete data is not proof of safety.</footer>\n</main>\n<script>\nconst $=id=>document.getElementById(id);const mintPattern=/^[1-9A-HJ-NP-Za-km-z]{32,44}$/;\nconst esc=v=>String(v??\'—\').replace(/[&<>"\']/g,c=>({\'&\':\'&amp;\',\'<\':\'&lt;\',\'>\':\'&gt;\',\'"\':\'&quot;\',"\'":\'&#39;\'}[c]));\nconst money=v=>v!==null&&v!==undefined&&v!==\'\'&&Number.isFinite(Number(v))?\'$\'+Number(v).toLocaleString(undefined,{maximumFractionDigits:2}):\'Not available\';\nconst count=v=>v!==null&&v!==undefined&&v!==\'\'&&Number.isFinite(Number(v))?Number(v).toLocaleString():\'—\';\nconst pct=v=>v!==null&&v!==undefined&&v!==\'\'&&Number.isFinite(Number(v))?Number(v).toFixed(2)+\'%\':\'—\';\nconst short=v=>v?String(v).slice(0,6)+\'…\'+String(v).slice(-5):\'—\';\nconst date=v=>v?new Date(Number(v)*1000).toLocaleString():\'Not found in sampled history\';\nlet busyMessages=[\'Fetching DexScreener market data…\',\'Checking RugCheck security signals…\',\'Scanning token holders and wallet histories…\',\'Preparing the investigation report…\'];let progressTimer=null;\nfunction section(title,body,extra=\'\'){return `<section class="panel ${extra}"><h3>${title}</h3>${body}</section>`}\nfunction statusTag(text,kind=\'\'){return `<span class="tag ${kind}">${esc(text)}</span>`}\nfunction render(d){\n const m=d.market_and_narrative_inputs||{},q=d.quick_status||{},r=(d.rugcheck_full_security_report||{}).data||{},h=d.helius_holder_and_wallet_analysis||{},cluster=h.cluster_candidates||{},risks=r.risks||[],holders=h.holder_distribution||[],wallets=h.wallet_analyses||[],known=r.knownAccounts||{},creator=r.creator;\n const mainRisk=risks.some(x=>String(x.level).toLowerCase()===\'danger\');const liquidity=m.liquidity_usd??r.totalMarketLiquidity;const lowOrUnknownLiquidity=!Number.isFinite(Number(liquidity))||Number(liquidity)<1000;const riskTitle=mainRisk?\'High caution — RugCheck danger flag\':(lowOrUnknownLiquidity?\'Caution — liquidity concern\':\'Review required\');\n const insiderNodes=(d.rugcheck_insider_network_graph||{}).data||[];\n const social=[...(m.websites||[]).map(x=>({label:x.label||\'Website\',url:x.url})),...(m.social_links||[]).map(x=>({label:x.type||\'Social\',url:x.url}))].filter(x=>typeof x.url===\'string\'&&/^https:\\/\\//.test(x.url));\n const riskHtml=risks.length?risks.map(x=>`<div class="rowline"><span>${esc(x.name)}</span><strong>${statusTag(String(x.level||\'flag\').toUpperCase(),String(x.level).toLowerCase()===\'danger\'?\'red\':\'yellow\')}</strong></div><p class="muted tiny">${esc(x.description||\'\')}</p>`).join(\'\'):\'<p class="empty">No RugCheck risk flags were returned. This does not establish that the token is safe.</p>\';\n const holderRows=holders.slice(0,12).map((x)=>{const meta=known[x.owner]||{};let label=meta.name|| (x.owner===creator?\'Creator\':\'Wallet\');let kind=String(meta.type||\'\').toUpperCase()===\'AMM\'?\'yellow\':x.owner===creator?\'red\':\'\';return `<tr><td>${esc(label)} ${kind?statusTag(kind===\'yellow\'?\'POOL / AMM\':\'CREATOR\',kind):\'\'}<br><code title="${esc(x.owner)}">${esc(short(x.owner))}</code></td><td>${pct(x.share_of_scanned_balances_pct)}</td></tr>`}).join(\'\');\n const walletRows=wallets.map(x=>{const arrival=x.first_inbound_token_transaction;const f=(x.funding_sources||[]).filter(y=>Number(y.sol_received||0)>=.01);return `<tr><td><code title="${esc(x.owner)}">${esc(short(x.owner))}</code></td><td>${arrival?esc(date(arrival.block_time)):\'Not located\'}</td><td>${f.length?f.map(y=>`${esc(y.sol_received.toFixed(3))} SOL from <code>${esc(short(y.from))}</code>`).join(\'<br>\'):\'None found\'}</td></tr>`}).join(\'\');\n const funding=(cluster.shared_material_SOL_funder_candidates||[]).map(x=>`<div class="rowline"><span><code>${esc(short(x.funder))}</code></span><strong>${count(x.recipient_count)} recipients</strong></div>`).join(\'\');\n const pairUrl=m.pair_url&&/^https:\\/\\//.test(m.pair_url)?m.pair_url:null;\n const orders=m.dex_paid_orders?.orders||[];const orderText=orders.length?orders.map(o=>`${esc(o.type||\'Promotion\')}: ${esc(o.status||\'status unknown\')}`).join(\'<br>\'):\'No paid orders returned\';\n const summary=`${m.token_name||\'Unknown token\'} (${m.token_symbol||\'?\'})\\nMint: ${d.token_address}\\nMarket cap: ${money(m.market_cap_usd)}\\nLiquidity: ${money(liquidity)}\\n24h volume: ${money(m.volume_24h_usd)}\\n24h change: ${pct(m.price_change_24h_pct)}\\nRugCheck normalised score: ${r.score_normalised??\'—\'}\\nRugCheck risks: ${risks.map(x=>x.name+\' (\'+x.level+\')\').join(\'; \')||\'None returned\'}\\nHolder scan: ${q.helius_holder_scan_complete?\'complete\':\'incomplete\'} (${q.helius_wallets_analysed||0} wallets sampled)\\nSame-slot candidates: ${q.same_slot_candidates??\'—\'}\\nShared material funders: ${q.shared_material_funder_candidates??\'—\'}\\nVerdict: ${riskTitle}\\nNote: screening data is incomplete and not proof of safety.`;\n $(\'results\').innerHTML=`<div class="reporthead"><div><div class="eyebrow">Investigation result</div><h2>${esc(m.token_name||\'Unknown token\')} <span class="muted">${esc(m.token_symbol||\'\')}</span></h2><div class="tiny muted">Mint: <code>${esc(d.token_address)}</code>${pairUrl?\' · <a target="_blank" rel="noopener" href="\'+esc(pairUrl)+\'">Open DexScreener ↗</a>\':\'\'}</div></div><span class="statuspill ${mainRisk?\'danger\':liquidity===null||Number(liquidity)<1000?\'warn\':\'good\'}">${esc(riskTitle)}</span></div>\n <div class="metrics"><div class="metric"><div class="label">Market cap / FDV</div><div class="value">${money(m.market_cap_usd)}</div></div><div class="metric"><div class="label">Reported liquidity</div><div class="value">${money(liquidity)}</div></div><div class="metric"><div class="label">24h volume</div><div class="value">${money(m.volume_24h_usd)}</div></div><div class="metric"><div class="label">24h price change</div><div class="value ${Number(m.price_change_24h_pct)>=0?\'pos\':\'neg\'}">${pct(m.price_change_24h_pct)}</div></div><div class="metric"><div class="label">24h buys / sells</div><div class="value">${count(m.buys_24h)} / ${count(m.sells_24h)}</div></div></div>\n <div class="grid">${section(\'Security and creator risks\',`<div class="rowline"><span>RugCheck normalised score</span><strong>${r.score_normalised??\'Not returned\'}</strong></div><div class="rowline"><span>Mint authority</span><strong>${r.mintAuthority===null?\'Revoked / none\':\'Check report\'}</strong></div><div class="rowline"><span>Freeze authority</span><strong>${r.freezeAuthority===null?\'Revoked / none\':\'Check report\'}</strong></div><div class="rowline"><span>Creator balance</span><strong>${r.creatorBalance===0?\'0 tokens\':esc(r.creatorBalance??\'Not returned\')}</strong></div><div class="rowline"><span>RugCheck insider graph entries</span><strong>${count(insiderNodes.length)}</strong></div>${riskHtml}<div class="sectionnote">An empty insider graph does not prove that no related wallets exist.</div>`,\'risk\')}\n ${section(\'Holder concentration\',`<div class="rowline"><span>Token accounts scanned</span><strong>${count(h.scan?.token_accounts_scanned)}</strong></div><div class="rowline"><span>Holder scan complete</span><strong>${h.scan?.scan_complete?\'Yes\':\'No\'}</strong></div><div class="tablewrap"><table><thead><tr><th>Owner</th><th>Share of scanned balance</th></tr></thead><tbody>${holderRows||\'<tr><td colspan="2">No holder data</td></tr>\'}</tbody></table></div><div class="sectionnote">Pool / AMM balances are labelled separately. Percentages reflect the returned account scan, not necessarily circulating supply.</div>`)}\n ${section(\'Wallet history and funding signals\',`<div class="rowline"><span>Wallets analysed</span><strong>${count(q.helius_wallets_analysed)}</strong></div><div class="rowline"><span>Same-slot first receipts</span><strong>${count(q.same_slot_candidates)}</strong></div><div class="rowline"><span>Shared material funder candidates</span><strong>${count(q.shared_material_funder_candidates)}</strong></div><div class="tablewrap"><table><thead><tr><th>Wallet</th><th>First detected token balance increase</th><th>Pre-arrival funding ≥ 0.01 SOL</th></tr></thead><tbody>${walletRows||\'<tr><td colspan="3">No wallet histories</td></tr>\'}</tbody></table></div><div class="sectionnote">A positive balance change is not automatically a buy. Same-slot timing or shared funding is a lead for review, not proof of bundled buying or common ownership.</div>`)}\n ${section(\'Social links and paid promotion\',`${social.length?social.map(x=>`<a target="_blank" rel="noopener" href="${esc(x.url)}">${esc(x.label)} ↗</a>`).join(\' · \'):\'<p class="empty">No website or social links returned.</p>\'}<div class="rowline"><span>Approved paid orders</span><strong>${m.approved_paid_order_count??\'Not returned\'}</strong></div><div class="rowline"><span>Active DEX boosts</span><strong>${m.active_dex_boosts??\'Not returned\'}</strong></div><div>${orderText}</div><div class="sectionnote">Social links, promotions and trading volume do not establish organic community interest or positive sentiment.</div>`)}\n ${section(\'Cluster candidates\',`${funding||\'<p class="empty">No shared material-funder candidates found in the sampled data.</p>\'}<div class="sectionnote">No same-slot first-receipt clusters were detected. A negative result is limited by the sampled history and does not prove that no coordinated wallets exist.</div>`)}\n ${section(\'Important limitations\',`<ul>${(d.interpretation_limits||[]).map(x=>`<li>${esc(x)}</li>`).join(\'\')}</ul>`)}\n <div class="actions"><button class="action" id="copySummary" type="button">Copy summary</button><button class="action secondary" id="newScan" type="button">Analyse another token</button></div><details class="accordion"><summary>Show raw technical response</summary><pre id="raw"></pre></details>`;\n $(\'raw\').textContent=JSON.stringify(d,null,2);$(\'results\').style.display=\'block\';$(\'copySummary\').onclick=async()=>{try{await navigator.clipboard.writeText(summary);$(\'copySummary\').textContent=\'Copied\';setTimeout(()=>$(\'copySummary\').textContent=\'Copy summary\',1600)}catch(e){$(\'copySummary\').textContent=\'Copy unavailable\'}};$(\'newScan\').onclick=()=>{$(\'mint\').focus();window.scrollTo({top:0,behavior:\'smooth\'})};\n}\n$(\'form\').addEventListener(\'submit\',async e=>{e.preventDefault();$(\'error\').style.display=\'none\';$(\'results\').style.display=\'none\';const mint=$(\'mint\').value.trim();if(!mintPattern.test(mint)){$(\'error\').textContent=\'That does not look like a valid Solana mint address. Check the address and try again.\';$(\'error\').style.display=\'block\';return}if($(\'run\').disabled)return;$(\'run\').disabled=true;$(\'run\').textContent=\'Analysing…\';$(\'status\').style.display=\'block\';let i=0;$(\'statusText\').textContent=busyMessages[0];progressTimer=setInterval(()=>{$(\'statusText\').textContent=busyMessages[++i%busyMessages.length]},5000);try{const response=await fetch(\'/api/analyze\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({token_address:mint}),signal:AbortSignal.timeout(240000)});const data=await response.json();if(!response.ok)throw new Error(data.error||`Request failed (${response.status})`);if(data.error&&!data.report_type)throw new Error(data.error);render(data);$(\'results\').scrollIntoView({behavior:\'smooth\',block:\'start\'})}catch(err){$(\'error\').textContent=(err.name===\'TimeoutError\'?\'The analysis took too long. Please try again.\':err.message||\'The analysis failed. Please retry.\');$(\'error\').style.display=\'block\'}finally{clearInterval(progressTimer);$(\'status\').style.display=\'none\';$(\'run\').disabled=false;$(\'run\').innerHTML=\'Analyse token <span aria-hidden="true">→</span>\'}});\n</script></body></html>\n'

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
