"""Self-test for the rules in .gitleaks.toml.

Feeds gitleaks one line per case on stdin and checks which rule flagged it:
each custom rule must fire on a fake credential of its own shape (not merely
"something fired" -- a default rule overlaps several of them), two canaries
prove the default rules are still loaded, and block and transaction hashes
must not be flagged at all. Every credential here is generated with
``secrets`` at run time, never a real key, and gitleaks runs with
``--redact``, so nothing it prints carries a value.

    python .github/gitleaks_selftest.py <path-to-gitleaks>
"""

from __future__ import annotations

import json
import secrets
import string
import subprocess
import sys
from pathlib import Path

CONFIG = Path(__file__).resolve().parent.parent / ".gitleaks.toml"

_hex = secrets.token_hex
_HEX_KEY = "hex-private-key-assignment"
_RPC = "rpc-url-with-key"


def _random(alphabet: str, length: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _alnum(length: int) -> str:
    return _random(string.ascii_letters + string.digits, length)


def cases() -> list[tuple[str, str, str | None]]:
    """``(name, line, rule id that must flag it or None for no finding)``."""
    return [
        ("env agent key", f"HYPERLIQUID_AGENT_KEY_MAINNET=0x{_hex(32)}", _HEX_KEY),
        ("env agent key, no 0x", f"HYPERLIQUID_AGENT_KEY_TESTNET={_hex(32)}", _HEX_KEY),
        ("yaml private_key", f'private_key: "0x{_hex(32)}"', _HEX_KEY),
        ("python signer_key", f'signer_key = "0x{_hex(32)}"', _HEX_KEY),
        ("python annotated signer_key", f'signer_key: str = "0x{_hex(32)}"', _HEX_KEY),
        ("json wallet key", f'"wallet_private_key": "0x{_hex(32)}"', _HEX_KEY),
        ("secret field without 'key'", f"HL_SECRET=0x{_hex(32)}", _HEX_KEY),
        ("openrouter key", f"OPENROUTER_API_KEY=sk-or-v1-{_hex(32)}", "openrouter-api-key"),
        ("alchemy https", f"ETH_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/{_alnum(21)}", _RPC),
        ("alchemy wss", f"ETH_WS_URL=wss://eth-mainnet.g.alchemy.com/v2/{_alnum(21)}", _RPC),
        ("infura https", f'rpc_url: "https://mainnet.infura.io/v3/{_hex(16)}"', _RPC),
        ("infura wss", f"wss://mainnet.infura.io/ws/v3/{_hex(16)}", _RPC),
        # Canaries for `[extend] useDefault`: only gitleaks' own rules know these.
        ("default rules: github token", f"token: ghp_{_alnum(36)}", "github-pat"),
        (
            "default rules: aws key id",
            f"aws_access_key_id = AKIA{_random(string.ascii_uppercase + '234567', 16)}",
            "aws-access-token",
        ),
        ("block hash", f'"hash": "0x{_hex(32)}"', None),
        ("parent hash", f'"parentHash": "0x{_hex(32)}"', None),
        ("tx hash", f'tx_hash = "0x{_hex(32)}"', None),
        ("alchemy placeholder", "https://eth-mainnet.g.alchemy.com/v2/<your-key>", None),
        ("public rpc", "ETH_RPC_URL=https://ethereum-rpc.publicnode.com", None),
    ]


def _rule_ids(gitleaks: str, line: str) -> set[str]:
    """The ids of the rules that flag ``line``; raises if gitleaks did not run."""
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
            "--report-format",
            "json",
            "--report-path",
            "-",
        ],
        input=line,
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit 1 means "leaks found" but also "config did not load"; only a
    # parsable report tells them apart.
    try:
        findings = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"gitleaks exited {result.returncode} without a report: {result.stderr.strip()}"
        ) from None
    return {finding["RuleID"] for finding in findings}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    failures = 0
    for name, line, want in cases():
        got = _rule_ids(argv[1], line)
        ok = want in got if want else not got
        failures += not ok
        print(
            f"{'ok  ' if ok else 'FAIL'} {name}: expected {want or 'no finding'}, got {sorted(got) or 'none'}"
        )
    print(f"{failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
