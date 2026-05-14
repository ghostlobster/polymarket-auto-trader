"""
Polymarket API key setup helpers.

Usage:
    python -m polymarket.auth setup

This derives API credentials from your private key and prints them
so you can paste them into .env.
"""

import sys

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON


def derive_api_credentials(private_key: str, chain_id: int = POLYGON) -> dict:
    """Derive L2 API credentials from an Ethereum private key."""
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=private_key,
        chain_id=chain_id,
    )
    creds = client.create_or_derive_api_creds()
    return {
        "POLYMARKET_API_KEY": creds.api_key,
        "POLYMARKET_API_SECRET": creds.api_secret,
        "POLYMARKET_API_PASSPHRASE": creds.api_passphrase,
    }


def setup_cli() -> None:
    """Interactive CLI to generate and print API credentials."""
    print("Polymarket API Credential Setup")
    print("=" * 40)
    private_key = input("Enter your Polygon wallet private key (0x...): ").strip()
    if not private_key.startswith("0x"):
        print("Error: private key must start with 0x")
        sys.exit(1)
    print("\nDeriving credentials from private key...")
    creds = derive_api_credentials(private_key)
    print("\nAdd these to your .env file:")
    print("-" * 40)
    for k, v in creds.items():
        print(f"{k}={v}")
    print(f"POLYMARKET_PRIVATE_KEY={private_key}")
    print("-" * 40)
    print("\nKeep these credentials secret!")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup_cli()
    else:
        print("Usage: python -m polymarket.auth setup")
