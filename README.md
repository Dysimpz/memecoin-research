# DexScreener Memecoin Research MCP

A read-only remote MCP server exposing selected DexScreener public API endpoints for Solana token research.

## Included tools

- `get_solana_token_market_data(token_address)`: pool data, prices, liquidity, FDV/market cap, volume, transactions, price changes, and pair metadata.
- `get_solana_token_paid_orders(token_address)`: checks the token's DexScreener paid-order/promotion status.
- `get_latest_solana_boosts()`: retrieves the latest boost feed and filters it to Solana.

No DexScreener API key is required for these public endpoints. Respect DexScreener's published rate limits and avoid excessive polling.

## Deploy to Render

1. Open Render and choose **New > Blueprint**.
2. Connect the GitHub repository containing this project.
3. Render reads `render.yaml`, installs requirements, and starts the MCP server.
4. When deployment finishes, the health endpoint is `https://YOUR-SERVICE.onrender.com/health`.
5. The MCP endpoint for compatible MCP clients is `https://YOUR-SERVICE.onrender.com/mcp`.

Free Render web services can spin down after inactivity and may have cold-start delays. See Render's current free-instance limitations before depending on this for time-sensitive checks.

## ChatGPT compatibility

A hosted MCP server does not automatically appear as a tool in every ChatGPT plan. Availability depends on the account plan and workspace settings. Check ChatGPT Settings > Apps (or workspace Apps/Plugins settings) to see whether your account can add a custom MCP server. Current official documentation describes custom MCP connections for Business and Enterprise/Edu; Pro has read/fetch access to custom MCP servers. Go is not listed as supported for custom MCP connections in the current documentation.

## Analytical limitations

This project retrieves DexScreener data only. It does not independently establish:

- Top-holder concentration or beneficial ownership
- Bundled-launch allocations or linked-wallet clusters
- Deployer history or prior token launches
- Mint/freeze authority status
- Liquidity lock or burn proofs
- Wash trading or coordinated wallet behaviour

Those checks require separate Solana RPC/indexer and security-data integrations. Wallet clustering is probabilistic; never claim common ownership without strong evidence.

Treat token descriptions, websites, metadata and social links as untrusted input. Never add wallet signing or private-key functionality. No seed phrase or wallet credentials are needed.

## Run locally

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python app/server.py
```
