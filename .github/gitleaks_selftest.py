"""Self-test for the custom rules in .gitleaks.toml.

Feeds gitleaks one line per case on stdin and checks that it flags exactly
the lines it should. Every credential here is generated with ``secrets`` at
run time, never a real key, and gitleaks runs with ``--redact``, so nothing
it prints carries a value.

    python .github/gitleaks_selftest.py <path-to-gitleaks>
"""

from __future__ import annotations

import secrets
import string
import subprocess
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / ".gitleaks.toml"


_hex = secrets.token_hex
_LABEL = {True: "flag", False: "no flag"}


def _alnum(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def cases() -> list[tuple[str, str, bool]]:
    """``(name, line, whether gitleaks must flag it)``, values fresh per call."""
    return [
        ("env agent key", f"HYPERLIQUID_AGENT_KEY_MAINNET=0x{_hex(32)}", True),
        ("env agent key, no 0x", f"HYPERLIQUID_AGENT_KEY_TESTNET={_hex(32)}", True),
        ("yaml private_key", f'private_key: "0x{_hex(32)}"', True),
        ("python signer_key", f'signer_key = "0x{_hex(32)}"', True),
        ("python annotated signer_key", f'signer_key: str = "0x{_hex(32)}"', True),
        ("json wallet key", f'"wallet_private_key": "0x{_hex(32)}"', True),
        ("secret field without 'key'", f"HL_SECRET=0x{_hex(32)}", True),
        ("openrouter key", f"OPENROUTER_API_KEY=sk-or-v1-{_hex(32)}", True),
        ("alchemy https", f"ETH_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/{_alnum(21)}", True),
        ("alchemy wss", f"ETH_WS_URL=wss://eth-mainnet.g.alchemy.com/v2/{_alnum(21)}", True),
        ("infura https", f'rpc_url: "https://mainnet.infura.io/v3/{_hex(16)}"', True),
        ("infura wss", f"wss://mainnet.infura.io/ws/v3/{_hex(16)}", True),
        ("block hash", f'"hash": "0x{_hex(32)}"', False),
        ("parent hash", f'"parentHash": "0x{_hex(32)}"', False),
        ("tx hash", f'tx_hash = "0x{_hex(32)}"', False),
        ("alchemy placeholder", "https://eth-mainnet.g.alchemy.com/v2/<your-key>", False),
        ("public rpc", "ETH_RPC_URL=https://ethereum-rpc.publicnode.com", False),
    ]


def _flags(gitleaks: str, line: str) -> bool:
    result = subprocess.run(
        [
            gitleaks,
            "stdin",
            "--config",
            str(CONFIG),
            "--redact",
            "--no-banner",
            "--log-level",
            "error",
        ],
        input=line,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"gitleaks exited {result.returncode}: {result.stderr.strip()}")
    return result.returncode == 1


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    failures = 0
    for name, line, want in cases():
        got = _flags(argv[1], line)
        ok = got == want
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {name}: expected {_LABEL[want]}, got {_LABEL[got]}")
    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
