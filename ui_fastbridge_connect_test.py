import base64
import getpass
import html
import json
import mimetypes
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
from web3 import Web3


WORKSPACE = Path(__file__).resolve().parent
ARTIFACTS_DIR = WORKSPACE / "fastbridge-report-artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
REPORT_BUNDLE_DIR = ARTIFACTS_DIR / f"fastbridge-report-{RUN_ID}"
REPORT_BUNDLE_DIR.mkdir(exist_ok=True)
ASSETS_DIR = REPORT_BUNDLE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)
JSON_REPORT_PATH = REPORT_BUNDLE_DIR / "report.json"
MD_REPORT_PATH = REPORT_BUNDLE_DIR / "report.md"
HTML_REPORT_PATH = REPORT_BUNDLE_DIR / "index.html"
EXPECTATIONS_HTML_PATH = REPORT_BUNDLE_DIR / "expectations.html"
DESTINATION_SLUG = os.environ.get("FASTBRIDGE_DEST_SLUG", "base").strip().strip("/")
BASE_URL = f"https://fastbridge.availproject.org/{DESTINATION_SLUG}/"
BRIDGE_AMOUNT = os.environ.get("FASTBRIDGE_BRIDGE_AMOUNT", "0.1").strip()
STOP_BEFORE_EXECUTION = os.environ.get("FASTBRIDGE_STOP_BEFORE_EXECUTION", "").strip().lower() in {"1", "true", "yes", "quote", "review"}
QUOTE_READY_TIMEOUT_MS = int(os.environ.get("FASTBRIDGE_QUOTE_READY_TIMEOUT_MS", "15000"))
NAVIGATION_TIMEOUT_MS = int(os.environ.get("FASTBRIDGE_NAVIGATION_TIMEOUT_MS", "45000"))
POST_LOAD_NETWORK_IDLE_TIMEOUT_MS = int(os.environ.get("FASTBRIDGE_POST_LOAD_NETWORK_IDLE_TIMEOUT_MS", "10000"))

CHAIN_CONFIG = {
    1: {"name": "Ethereum", "rpc": "https://1rpc.io/eth"},
    10: {"name": "Optimism", "rpc": "https://1rpc.io/op"},
    56: {"name": "BNB Smart Chain", "rpc": "https://1rpc.io/bnb"},
    137: {"name": "Polygon", "rpc": "https://1rpc.io/matic"},
    8453: {"name": "Base", "rpc": "https://1rpc.io/base"},
    42161: {"name": "Arbitrum", "rpc": "https://1rpc.io/arb"},
    43114: {"name": "Avalanche", "rpc": "https://1rpc.io/avax/c"},
    534352: {"name": "Scroll", "rpc": "https://1rpc.io/scroll"},
}

EXPECTATION_CATALOG = [
    {
        "id": "EXP-T01",
        "name": "Execution completes within 30s",
        "category": "Timing",
        "description": "Once the user approves execution, the bridge should reach a terminal success state within 30 seconds.",
    },
    {
        "id": "EXP-T02",
        "name": "Page load within 5s",
        "category": "Timing",
        "description": "The bridge page should become usable within 5 seconds of navigation.",
    },
    {
        "id": "EXP-T03",
        "name": "Balances refresh after completion",
        "category": "Timing",
        "description": "After a successful bridge, the balance UI should refresh promptly to reflect updated source and destination balances.",
    },
    {
        "id": "EXP-T04",
        "name": "Quote appears within 5s",
        "category": "Timing",
        "description": "After entering an amount, a valid quote should appear within 5 seconds.",
    },
    {
        "id": "EXP-T05",
        "name": "Change in entered amount",
        "category": "Timing",
        "description": "If the entered amount is changed, it should not freeze UI",
    },
    {
        "id": "EXP-T06",
        "name": "Change in entered amount and fetching of quote",
        "category": "Timing",
        "description": "While fetching the quote, if the entered amount is changed, fetching process should be started again and Entered amount UI should not be disabled",
    },
    {
        "id": "EXP-D01",
        "name": "Delivered output matches quoted output",
        "category": "Data",
        "description": "If the bridge completes, the delivered destination amount should match the quoted output within the accepted tolerance.",
    },
    {
        "id": "EXP-D02",
        "name": "Actual fees do not materially exceed quoted fees",
        "category": "Data",
        "description": "If the bridge completes, the effective fees should not materially exceed the quoted fees for the same route.",
    },
    {
        "id": "EXP-D03",
        "name": "Unified balance and breakdown are visible",
        "category": "Data",
        "description": "The route should show a unified balance and a per-chain breakdown when balance details are expanded.",
    },
    {
        "id": "EXP-U01",
        "name": "Spend, receive, and fee details shown",
        "category": "UI",
        "description": "Before confirmation, the route should clearly show spend amount, receive amount, and total fees.",
    },
    {
        "id": "EXP-U02",
        "name": "Source chain visible before confirm",
        "category": "UI",
        "description": "Before the user accepts the quote, the app should make the chosen source chain or source summary visible.",
    },
    {
        "id": "EXP-U03",
        "name": "Error messages are user-readable",
        "category": "UI",
        "description": "If a user-facing action fails or the journey is blocked, the app should surface a clear, human-readable explanation and next step.",
    },
]

INIT_SCRIPT = r"""
(() => {
  const listeners = {};
  const providerInfo = {
    uuid: "codex-metamask-test-wallet",
    name: "MetaMask",
    icon: "data:image/svg+xml;base64,PHN2Zy8+",
    rdns: "io.metamask"
  };
  const emit = (event, payload) => {
    (listeners[event] || []).forEach((cb) => {
      try { cb(payload); } catch (error) { console.error(error); }
    });
  };

  class CodexProvider {
    constructor() {
      this.isMetaMask = true;
      this.isConnected = () => true;
      this._metamask = { isUnlocked: async () => true };
      this._address = "%ADDRESS%";
      this.chainId = "%CHAIN_ID%";
      this.selectedAddress = this._address;
      this.networkVersion = String(parseInt(this.chainId, 16));
    }

    async request(args) {
      const result = await window.pyProviderRequest(args);
      if (args.method === "wallet_switchEthereumChain" && result?.chainId) {
        this.chainId = result.chainId;
        this.networkVersion = String(parseInt(this.chainId, 16));
        emit("chainChanged", this.chainId);
      }
      if (args.method === "wallet_addEthereumChain" && result?.chainId) {
        this.chainId = result.chainId;
        this.networkVersion = String(parseInt(this.chainId, 16));
        emit("chainChanged", this.chainId);
      }
      if (args.method === "eth_requestAccounts") {
        emit("accountsChanged", result);
      }
      return result;
    }

    async enable() {
      return this.request({ method: "eth_requestAccounts" });
    }

    on(event, callback) {
      listeners[event] = listeners[event] || [];
      listeners[event].push(callback);
    }

    removeListener(event, callback) {
      listeners[event] = (listeners[event] || []).filter((cb) => cb !== callback);
    }
  }

  const provider = new CodexProvider();
  window.ethereum = provider;
  window.ethereum.providers = [provider];
  const announce = () => {
    window.dispatchEvent(new CustomEvent("eip6963:announceProvider", {
      detail: { info: providerInfo, provider }
    }));
  };
  window.addEventListener("eip6963:requestProvider", announce);
  announce();
  window.dispatchEvent(new Event("ethereum#initialized"));
})();
"""


def sanitize_filename(label: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in label).strip("-").lower()


def parse_hex_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.startswith("0x"):
        return int(value, 16)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def to_hex(value: int) -> str:
    return hex(value)


def ensure_0x(value: str) -> str:
    return value if value.startswith("0x") else f"0x{value}"


def hexbytes_to_0x(value: Any) -> str:
    if hasattr(value, "to_0x_hex"):
        return value.to_0x_hex()
    return ensure_0x(value.hex())


def normalize_typed_data_payload(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise ValueError(f"Unsupported typed data payload type: {type(payload)!r}")
    return payload


def humanize_destination(slug: str) -> str:
    names = {
        "base": "Base",
        "ethereum": "Ethereum",
        "arbitrum": "Arbitrum",
        "op-mainnet": "OP Mainnet",
        "polygon": "Polygon",
        "avalanche": "Avalanche",
        "bnb-smart-chain": "BNB Smart Chain",
        "scroll": "Scroll",
        "monad": "Monad",
        "megaeth": "MegaETH",
        "citrea": "Citrea Mainnet",
        "hyperevm": "HyperEVM",
        "kaia": "Kaia Mainnet",
    }
    return names.get(slug, slug.replace("-", " ").title())


def extract_total_usdc(text: str) -> Optional[str]:
    match = re.search(r"\n([0-9]+(?:\.[0-9]+)?) USDC\n\nMAX", text)
    return match.group(1) if match else None


def extract_first(pattern: str, text: str) -> Optional[str]:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else None


def extract_quote_details(text: str) -> Dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    def find_label(label: str) -> Optional[int]:
        for index, line in enumerate(lines):
            if line.lower() == label.lower():
                return index
        return None

    def first_amount_after(label: str, stop_labels: Optional[List[str]] = None) -> Optional[str]:
        start = find_label(label)
        if start is None:
            return None
        stop_label_set = {item.lower() for item in (stop_labels or [])}
        for line in lines[start + 1 :]:
            if line.lower() in stop_label_set:
                return None
            if re.fullmatch(r"[0-9]+(?:\.[0-9]+)? USDC", line):
                return line
        return None

    spend_index = find_label("You Spend")
    receive_index = find_label("You receive")
    fees_index = find_label("Total fees")
    amount_spent = first_amount_after("You Spend", ["You receive", "Total fees"])
    amount_received = first_amount_after("You receive", ["Total fees"])
    total_fees = first_amount_after("Total fees")

    source_summary = None
    if spend_index is not None:
        stop = receive_index if receive_index is not None else len(lines)
        for line in lines[spend_index + 1 : stop]:
            if line == amount_spent:
                continue
            if line in {"MAX", "Bridge", "Deny", "Accept", "Refreshing...", "Fetching intent..."}:
                continue
            source_summary = line
            break

    destination_shown = None
    if receive_index is not None:
        stop = fees_index if fees_index is not None else len(lines)
        for line in lines[receive_index + 1 : stop]:
            if line == amount_received:
                continue
            if line.lower().startswith("on "):
                destination_shown = line[3:].strip()
                break
            if line not in {"Deny", "Accept", "Refreshing...", "Fetching intent..."}:
                destination_shown = line
                break

    parse_errors = []
    if amount_spent is None:
        parse_errors.append("amountSpent")
    if amount_received is None:
        parse_errors.append("amountReceived")
    if total_fees is None:
        parse_errors.append("totalFees")

    return {
        "sourceSummary": source_summary,
        "amountSpent": amount_spent,
        "amountReceived": amount_received,
        "destinationShown": destination_shown,
        "totalFees": total_fees,
        "quoteParsed": not parse_errors,
        "parseErrors": parse_errors,
    }


def extract_best_quote(step_log: List[Dict[str, Any]]) -> Dict[str, Any]:
    best_quote: Optional[Dict[str, Any]] = None
    best_label: Optional[str] = None
    best_score = -1

    for step in step_log:
        quote = extract_quote_details(step.get("text", ""))
        score = sum(1 for key in ("amountSpent", "amountReceived", "totalFees") if quote.get(key))
        if score >= best_score:
            best_quote = quote
            best_label = step.get("label")
            best_score = score
        if quote.get("quoteParsed"):
            best_quote = quote
            best_label = step.get("label")

    quote = best_quote or extract_quote_details("")
    quote["evidenceLabel"] = best_label
    return quote


def extract_completion_details(text: str) -> Dict[str, Optional[str]]:
    return {
        "sourceChains": extract_first(r"Source\(s\): ([^\n]+)", text),
        "destination": extract_first(r"Destination: ([^\n]+)", text),
        "asset": extract_first(r"Asset: ([^\n]+)", text),
        "amountSpent": extract_first(r"Amount Spent: ([^\n]+)", text),
        "amountReceived": extract_first(r"Amount Received: ([^\n]+)", text),
        "totalFees": extract_first(r"Total Fees: ([^\n]+)", text),
    }


def extract_breakdown_rows(text: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    lines = [line.strip() for line in text.splitlines()]
    ignored = {
        "",
        "MAX",
        "USDC",
        "View Balance Breakdown",
        "Recipient Address",
        "Bridge",
        "Powered by",
        "Reach out to us if",
        "you face any issues",
    }
    for index, line in enumerate(lines):
        if line in ignored:
            continue
        next_value = None
        for candidate in lines[index + 1 :]:
            if candidate == "":
                continue
            next_value = candidate
            break
        if next_value and re.fullmatch(r"[0-9]+(?:\.[0-9]+)? USDC", next_value):
            rows.append({"chain": line, "balance": next_value})
    seen = set()
    unique_rows = []
    for row in rows:
        key = (row["chain"], row["balance"])
        if key not in seen:
            seen.add(key)
            unique_rows.append(row)
    return unique_rows


def extract_numeric_amount(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", value)
    return float(match.group(1)) if match else None


class WalletHarness:
    def __init__(self, address: str, private_key: str, provider_calls: List[Dict[str, Any]], tx_log: List[Dict[str, Any]]):
        self.address = Web3.to_checksum_address(address)
        self.private_key = private_key
        self.account = Account.from_key(private_key)
        self.current_chain_id = 8453
        self.provider_calls = provider_calls
        self.tx_log = tx_log
        self.clients = {
            chain_id: Web3(Web3.HTTPProvider(config["rpc"], request_kwargs={"timeout": 30}))
            for chain_id, config in CHAIN_CONFIG.items()
        }

    def current_client(self) -> Web3:
        return self.clients[self.current_chain_id]

    def rpc_passthrough(self, method: str, params: List[Any]) -> Any:
        return self.current_client().provider.make_request(method, params)["result"]

    def sign_message(self, message_hex: str) -> str:
        if message_hex.startswith("0x"):
            message_bytes = bytes.fromhex(message_hex[2:])
        else:
            message_bytes = message_hex.encode()
        signed = Account.sign_message(encode_defunct(message_bytes), private_key=self.private_key)
        return hexbytes_to_0x(signed.signature)

    def sign_typed_data(self, typed_data: Any) -> str:
        payload = normalize_typed_data_payload(typed_data)
        signed = Account.sign_typed_data(self.private_key, full_message=payload)
        return hexbytes_to_0x(signed.signature)

    def send_transaction(self, tx: Dict[str, Any]) -> str:
        w3 = self.current_client()
        tx = dict(tx)
        tx.setdefault("from", self.address)
        tx["from"] = Web3.to_checksum_address(tx["from"])
        if tx["from"] != self.address:
            raise ValueError(f"Unexpected from address {tx['from']}")

        tx_payload: Dict[str, Any] = {
            "from": self.address,
            "to": Web3.to_checksum_address(tx["to"]) if tx.get("to") else None,
            "value": parse_hex_int(tx.get("value")) or 0,
            "data": tx.get("data", "0x"),
            "nonce": parse_hex_int(tx.get("nonce")),
            "gas": parse_hex_int(tx.get("gas")),
            "chainId": self.current_chain_id,
        }

        if tx_payload["nonce"] is None:
            tx_payload["nonce"] = w3.eth.get_transaction_count(self.address, "pending")

        if tx_payload["gas"] is None:
            estimate_payload = {
                "from": self.address,
                "to": tx_payload["to"],
                "value": tx_payload["value"],
                "data": tx_payload["data"],
            }
            try:
                tx_payload["gas"] = int(w3.eth.estimate_gas(estimate_payload) * 1.2)
            except Exception:
                tx_payload["gas"] = 250000

        max_fee = parse_hex_int(tx.get("maxFeePerGas"))
        max_priority = parse_hex_int(tx.get("maxPriorityFeePerGas"))
        gas_price = parse_hex_int(tx.get("gasPrice"))

        latest_block = w3.eth.get_block("latest")
        base_fee = latest_block.get("baseFeePerGas")
        if max_fee is not None or max_priority is not None or base_fee is not None:
            if max_priority is None:
                max_priority = w3.to_wei(0.001, "gwei")
            if max_fee is None:
                base_fee_value = base_fee or w3.eth.gas_price
                max_fee = int(base_fee_value * 2 + max_priority)
            tx_payload["maxPriorityFeePerGas"] = max_priority
            tx_payload["maxFeePerGas"] = max_fee
            tx_payload["type"] = 2
        else:
            tx_payload["gasPrice"] = gas_price or w3.eth.gas_price

        signed = self.account.sign_transaction(tx_payload)
        tx_hash = hexbytes_to_0x(w3.eth.send_raw_transaction(signed.raw_transaction))
        self.tx_log.append(
            {
                "chainId": self.current_chain_id,
                "chainName": CHAIN_CONFIG[self.current_chain_id]["name"],
                "txHash": tx_hash,
                "tx": {
                    key: (hex(value) if isinstance(value, int) else value)
                    for key, value in tx_payload.items()
                },
            }
        )
        return tx_hash

    def handler(self, source: Any, payload: Dict[str, Any]) -> Any:
        method = payload.get("method")
        params = payload.get("params") or []
        self.provider_calls.append({"method": method, "params": params, "chainId": self.current_chain_id})

        if method in ("eth_requestAccounts", "eth_accounts"):
            return [self.address]
        if method == "eth_chainId":
            return hex(self.current_chain_id)
        if method == "net_version":
            return str(self.current_chain_id)
        if method == "wallet_switchEthereumChain":
            chain_hex = params[0]["chainId"]
            self.current_chain_id = int(chain_hex, 16)
            return {"chainId": chain_hex}
        if method == "wallet_addEthereumChain":
            chain_hex = params[0]["chainId"]
            self.current_chain_id = int(chain_hex, 16)
            return {"chainId": chain_hex}
        if method == "wallet_getPermissions":
            return []
        if method == "eth_coinbase":
            return self.address
        if method == "personal_sign":
            return self.sign_message(params[0])
        if method in {"eth_signTypedData", "eth_signTypedData_v3", "eth_signTypedData_v4"}:
            typed_data = params[-1]
            return self.sign_typed_data(typed_data)
        if method == "eth_sendTransaction":
            return self.send_transaction(params[0])
        if method in {
            "eth_estimateGas",
            "eth_gasPrice",
            "eth_blockNumber",
            "eth_getBalance",
            "eth_getTransactionCount",
            "eth_call",
            "eth_getCode",
            "eth_feeHistory",
            "eth_maxPriorityFeePerGas",
            "eth_getBlockByNumber",
            "eth_getBlockByHash",
            "eth_getTransactionReceipt",
            "eth_getTransactionByHash",
            "wallet_getCapabilities",
        }:
            return self.rpc_passthrough(method, params)

        return {
            "__codexUnhandled": True,
            "method": method,
            "params": params,
            "chainId": self.current_chain_id,
        }


def save_screenshot(page: Page, label: str) -> str:
    path = ASSETS_DIR / f"{sanitize_filename(label)}.png"
    page.screenshot(path=str(path), full_page=True)
    return str(path)


def find_button(page: Page, name: str):
    locator = page.get_by_role("button", name=name)
    return locator.first if locator.count() > 0 else None


def button_is_ready(button: Any) -> bool:
    if not button:
        return False
    try:
        return button.is_visible() and button.is_enabled()
    except Exception:
        return False


def wait_for_quote_ready(page: Page, timeout_ms: int = QUOTE_READY_TIMEOUT_MS) -> Dict[str, Any]:
    start = time.monotonic()
    try:
        page.wait_for_function(
            """() => {
              const text = document.body.innerText || "";
              const amountMatches = text.match(/[0-9]+(?:\\.[0-9]+)?\\s+USDC/g) || [];
              const acceptReady = Array.from(document.querySelectorAll("button")).some((button) => {
                const label = (button.innerText || button.textContent || "").trim();
                return label === "Accept" && !button.disabled && button.getAttribute("aria-disabled") !== "true";
              });
              return acceptReady
                && text.includes("You Spend")
                && text.includes("You receive")
                && text.includes("Total fees")
                && amountMatches.length >= 3;
            }""",
            timeout=timeout_ms,
        )
        return {"ready": True, "elapsedMs": int((time.monotonic() - start) * 1000), "timeoutMs": timeout_ms}
    except PlaywrightTimeoutError:
        return {
            "ready": False,
            "elapsedMs": int((time.monotonic() - start) * 1000),
            "timeoutMs": timeout_ms,
            "reason": "Quote did not reach an Accept-ready state before timeout.",
        }


def wait_and_capture(page: Page, label: str, delay_ms: int = 1500) -> Dict[str, Any]:
    page.wait_for_timeout(delay_ms)
    return {
        "label": label,
        "text": page.locator("body").inner_text()[:8000],
        "screenshot": save_screenshot(page, label),
    }


def append_issue(
    issues: List[Dict[str, Any]],
    title: str,
    severity: str,
    details: str,
    reproduction: List[str],
    issue_id: Optional[str] = None,
    evidence: Optional[str] = None,
    root_cause: Optional[str] = None,
):
    issues.append(
        {
            "id": issue_id,
            "title": title,
            "severity": severity,
            "details": details,
            "reproduction": reproduction,
            "evidence": evidence,
            "rootCause": root_cause,
        }
    )


def build_report_markdown(result: Dict[str, Any]) -> str:
    expectations_all = expectations_with_na(result)
    lines = []
    lines.append("# FastBridge QA Dashboard")
    lines.append("")
    lines.append(f"- Scenario ID: `{result['scenarioId']}`")
    lines.append(f"- Status: `{result['status']}`")
    lines.append(f"- Execution mode: `{result.get('executionMode', 'full')}`")
    lines.append(f"- Product outcome: `{result.get('productOutcome', 'unknown')}`")
    lines.append(f"- Harness outcome: `{result.get('harnessOutcome', 'unknown')}`")
    lines.append(f"- Run ID: `{result['runId']}`")
    lines.append(f"- UTC Time: `{result['timestampUtc']}`")
    lines.append(f"- Tester: `{result['tester']}`")
    lines.append(f"- App URL: `{result['appUrl']}`")
    lines.append(f"- Wallet: `{result['address']}`")
    lines.append(f"- Destination chain tested: `{result['destinationName']}`")
    lines.append(f"- Destination token tested: `USDC`")
    lines.append(f"- Transfer amount tested: `{result['bridgeAmount']} USDC`")
    lines.append(f"- Bridge completed: `{result['bridgeSuccessful']}`")
    lines.append(f"- Gasless flow detected: `{result['usedGaslessFlow']}`")
    lines.append(f"- Transactions submitted: `{len(result['txLog'])}`")
    if result.get("explorerUrl"):
        lines.append(f"- Explorer URL: `{result['explorerUrl']}`")
    lines.append("")
    lines.append("## Executive Summary")
    lines.append("")
    for item in result["summary"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Scenario Outcome")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| Scenario | `{result['scenarioId']}` |")
    lines.append(f"| Destination | `{result['destinationName']}` |")
    lines.append(f"| Requested output | `{result['bridgeAmount']} USDC` |")
    lines.append(f"| Execution mode | `{result.get('executionMode', 'full')}` |")
    lines.append(f"| Product outcome | `{result.get('productOutcome', 'unknown')}` |")
    lines.append(f"| Harness outcome | `{result.get('harnessOutcome', 'unknown')}` |")
    lines.append(f"| Quote parsed | `{result['quote'].get('quoteParsed')}` |")
    lines.append(f"| Bridge completed | `{result['bridgeSuccessful']}` |")
    lines.append(f"| Gasless flow | `{result['usedGaslessFlow']}` |")
    lines.append(f"| Source chain(s) used | `{result['completion'].get('sourceChains') or result['quote'].get('sourceSummary') or 'unknown'}` |")
    lines.append(f"| Amount spent | `{result['completion'].get('amountSpent') or result['quote'].get('amountSpent') or 'unknown'}` |")
    lines.append(f"| Amount received | `{result['completion'].get('amountReceived') or result['quote'].get('amountReceived') or 'unknown'}` |")
    lines.append(f"| Total fees | `{result['completion'].get('totalFees') or result['quote'].get('totalFees') or 'unknown'}` |")
    lines.append("")
    lines.append("## Flow Results")
    lines.append("")
    for item in result["worked"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Flow Checkpoints")
    lines.append("")
    for checkpoint in result["checkpoints"]:
        lines.append(f"- `{checkpoint['label']}`: `{checkpoint['status']}`")
        lines.append(f"  Evidence: `{checkpoint['evidence']}`")
        lines.append(f"  Notes: {checkpoint['notes']}")
    lines.append("")
    lines.append("## Expectations Assessment")
    lines.append("")
    lines.append("| Expectation | Status | Notes |")
    lines.append("| --- | --- | --- |")
    for expectation in expectations_all:
        lines.append(f"| `{expectation['id']}` {expectation['name']} | `{expectation['status']}` | {expectation['notes']} |")
    lines.append("")
    lines.append("## Issues Found")
    lines.append("")
    if result["issues"]:
        for issue in result["issues"]:
            heading = issue["title"] if not issue.get("id") else f"{issue['id']} {issue['title']}"
            lines.append(f"### {heading}")
            lines.append(f"- Severity: `{issue['severity']}`")
            lines.append(f"- Details: {issue['details']}")
            if issue.get("evidence"):
                lines.append(f"- Evidence: `{issue['evidence']}`")
            if issue.get("rootCause"):
                lines.append(f"- Suspected Root Cause: {issue['rootCause']}")
            lines.append(f"- Reproduction:")
            for step in issue["reproduction"]:
                lines.append(f"  - {step}")
            lines.append("")
    else:
        lines.append("- No blocking issues captured in this run.")
        lines.append("")
    lines.append("## Metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("| --- | --- |")
    for key, value in result["metrics"].items():
        lines.append(f"| `{key}` | `{value}` |")
    lines.append("")
    lines.append("## Balance Evidence")
    lines.append("")
    lines.append(f"- Initial unified balance: `{result['balances'].get('initialUnified') or 'unknown'}`")
    lines.append(f"- Final unified balance: `{result['balances'].get('finalUnified') or 'unknown'}`")
    if result["balances"].get("breakdown"):
        for row in result["balances"]["breakdown"]:
            lines.append(f"- `{row['chain']}`: `{row['balance']}`")
    lines.append("")
    lines.append("## Wallet Interaction")
    lines.append("")
    lines.append(f"- Methods called: `{', '.join(result['walletInteraction']['methods'])}`")
    lines.append(f"- Personal sign requests: `{result['walletInteraction']['personalSignCount']}`")
    lines.append(f"- Typed data sign requests: `{result['walletInteraction']['typedDataCount']}`")
    lines.append(f"- `eth_sendTransaction` requests: `{result['walletInteraction']['sendTransactionCount']}`")
    lines.append("")
    lines.append("## Network And Console Signals")
    lines.append("")
    for signal in result["networkSummary"]:
        lines.append(f"- {signal}")
    lines.append("")
    lines.append("## Transactions")
    lines.append("")
    if result["txLog"]:
        for tx in result["txLog"]:
            lines.append(f"- `{tx['chainName']}`: `{tx['txHash']}`")
    elif result.get("bridgeSuccessful") and result.get("usedGaslessFlow"):
        lines.append("- No direct wallet-broadcast transaction was captured. The route completed via signed approvals/intents and relayer execution.")
    else:
        lines.append("- No transactions were broadcast in this run.")
    lines.append("")
    lines.append("## Artifacts")
    lines.append("")
    for artifact in result["artifacts"]:
        lines.append(f"- `{artifact['label']}`: `{artifact['path']}`")
    lines.append("")
    return "\n".join(lines)


def exportable_result(result: Dict[str, Any]) -> Dict[str, Any]:
    exported = json.loads(json.dumps(result))
    exported["bundlePath"] = "."

    for issue in exported.get("issues", []):
        if issue.get("evidence"):
            issue["evidence"] = relative_bundle_path(issue["evidence"], result.get("bundlePath"))

    for checkpoint in exported.get("checkpoints", []):
        if checkpoint.get("evidence"):
            checkpoint["evidence"] = relative_bundle_path(checkpoint["evidence"], result.get("bundlePath"))

    for artifact in exported.get("artifacts", []):
        if artifact.get("path"):
            artifact["path"] = relative_bundle_path(artifact["path"], result.get("bundlePath"))

    for step in exported.get("stepLog", []):
        if step.get("screenshot"):
            step["screenshot"] = relative_bundle_path(step["screenshot"], result.get("bundlePath"))

    return exported


def relative_bundle_path(path_str: Optional[str], bundle_path: Optional[str] = None) -> Optional[str]:
    if not path_str:
        return None
    path = Path(path_str)
    base_dir = Path(bundle_path) if bundle_path else REPORT_BUNDLE_DIR
    try:
        rel = str(path.relative_to(base_dir))
        return f"./{rel}"
    except ValueError:
        return f"./{path.name}"


def file_to_data_url(path_str: Optional[str]) -> Optional[str]:
    if not path_str:
        return None
    path = Path(path_str)
    if not path.exists():
        return None
    mime_type, _ = mimetypes.guess_type(str(path))
    mime_type = mime_type or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def expectations_with_na(result: Dict[str, Any]) -> List[Dict[str, str]]:
    observed = {item["id"]: item for item in result["expectations"]}
    merged: List[Dict[str, str]] = []
    for item in EXPECTATION_CATALOG:
        observed_item = observed.get(item["id"])
        merged.append(
            {
                "id": item["id"],
                "category": item["category"],
                "name": item["name"],
                "description": item["description"],
                "status": observed_item["status"] if observed_item else "NA",
                "notes": observed_item["notes"] if observed_item else "Not applicable in this scenario.",
            }
        )
    return merged


def severity_class(value: str) -> str:
    normalized = value.lower()
    if normalized in {"high", "critical", "p0", "p1", "fail", "product_fail", "parser_error"}:
        return "sev-high"
    if normalized in {"medium", "partial", "anomaly", "p2", "harness_timeout", "low_balance"}:
        return "sev-medium"
    return "sev-low"


def render_badge(text: str, class_name: str = "") -> str:
    suffix = f" {class_name}" if class_name else ""
    return f'<span class="badge{suffix}">{html.escape(text)}</span>'


def shorten_address(address: str) -> str:
    return f"{address[:8]}...{address[-6:]}" if len(address) > 18 else address


def compact_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.netloc:
        return url
    path = parsed.path.rstrip("/")
    return f"{parsed.netloc}{path}" if path else parsed.netloc


def render_meta_value(label: str, value: str, *, mono: bool = False, href: Optional[str] = None, title: Optional[str] = None) -> str:
    tag = "a" if href else ("code" if mono else "span")
    attrs = ' class="meta-value"'
    if href:
        attrs += f' href="{html.escape(href)}"'
    if title:
        attrs += f' title="{html.escape(title)}"'
    return f'<div class="card meta-card"><strong>{html.escape(label)}</strong><br><{tag}{attrs}>{html.escape(value)}</{tag}></div>'


def render_copy_meta_value(label: str, display_value: str, copy_value: str, *, mono: bool = False, title: Optional[str] = None) -> str:
    classes = "meta-value copy-value"
    if mono:
        classes += " mono"
    title_attr = f' title="{html.escape(title or copy_value)}"' if title or copy_value else ""
    return (
        f'<div class="card meta-card copy-card"><strong>{html.escape(label)}</strong><br>'
        f'<button class="{classes}" type="button" data-copy="{html.escape(copy_value)}"{title_attr} '
        f'aria-label="Copy {html.escape(label)}">{html.escape(display_value)}</button>'
        f'<span class="copy-hint" aria-live="polite">Click to copy</span></div>'
    )


def build_report_html(result: Dict[str, Any]) -> str:
    bundle_path = result.get("bundlePath")
    expectations_all = expectations_with_na(result)
    app_url = result["appUrl"]
    explorer_url = result.get("explorerUrl")
    wallet_address = result["address"]
    explorer_card = (
        render_meta_value("Explorer", compact_url(explorer_url), href=explorer_url, title=explorer_url)
        if explorer_url
        else render_meta_value("Explorer", "n/a")
    )
    checkpoint_cards = []
    for checkpoint in result["checkpoints"]:
        evidence = file_to_data_url(checkpoint["evidence"])
        image_html = ""
        if evidence:
            image_html = (
                f'<button class="shot" type="button" data-fullscreen="true">'
                f'<img src="{html.escape(evidence)}" alt="{html.escape(checkpoint["label"])} screenshot"></button>'
            )
        checkpoint_cards.append(
            f"""
            <article class="card checkpoint">
              <div class="card-head">
                <h3>{html.escape(checkpoint["label"])}</h3>
                {render_badge(checkpoint["status"], severity_class(checkpoint["status"]))}
              </div>
              <p>{html.escape(checkpoint["notes"])}</p>
              {image_html}
            </article>
            """
        )

    issue_cards = []
    for issue in result["issues"]:
        evidence = file_to_data_url(issue.get("evidence"))
        evidence_html = ""
        if evidence:
            evidence_html = (
                f'<button class="shot" type="button" data-fullscreen="true">'
                f'<img src="{html.escape(evidence)}" alt="{html.escape(issue["title"])} evidence"></button>'
            )
        reproduction = "".join(f"<li>{html.escape(step)}</li>" for step in issue["reproduction"])
        issue_cards.append(
            f"""
            <article class="card issue">
              <div class="card-head">
                <h3>{html.escape((issue.get("id") + " ") if issue.get("id") else "")}{html.escape(issue["title"])}</h3>
                {render_badge(issue["severity"], severity_class(issue["severity"]))}
              </div>
              <p>{html.escape(issue["details"])}</p>
              {'<p><strong>Suspected Root Cause:</strong> ' + html.escape(issue["rootCause"]) + '</p>' if issue.get("rootCause") else ''}
              <p><strong>Reproduction</strong></p>
              <ol>{reproduction}</ol>
              {evidence_html}
            </article>
            """
        )

    artifact_cards = []
    for artifact in result["artifacts"]:
        rel = file_to_data_url(artifact["path"])
        if rel:
            artifact_cards.append(
                f"""
                <button class="artifact shot" type="button" data-fullscreen="true">
                  <img src="{html.escape(rel)}" alt="{html.escape(artifact["label"])}">
                  <span>{html.escape(artifact["label"])}</span>
                </button>
                """
            )

    expectations_rows = "".join(
        f"<tr><td><code>{html.escape(item['id'])}</code> {html.escape(item['name'])}</td>"
        f"<td>{render_badge(item['status'], severity_class(item['status']))}</td>"
        f"<td>{html.escape(item['notes'])}</td></tr>"
        for item in expectations_all
    )
    metrics_rows = "".join(
        f"<tr><td><code>{html.escape(str(key))}</code></td><td>{html.escape(str(value))}</td></tr>"
        for key, value in result["metrics"].items()
    )
    balance_rows = "".join(
        f"<tr><td>{html.escape(row['chain'])}</td><td>{html.escape(row['balance'])}</td></tr>"
        for row in result["balances"]["breakdown"]
    )
    signals = "".join(f"<li>{html.escape(signal)}</li>" for signal in result["networkSummary"])
    worked = "".join(f"<li>{html.escape(item)}</li>" for item in result["worked"])
    summary = "".join(f"<li>{html.escape(item)}</li>" for item in result["summary"])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <base href="./">
  <title>{html.escape(result["scenarioId"])} - FastBridge QA Dashboard</title>
  <style>
    :root {{
      --bg: #f6f1e8;
      --panel: rgba(255,255,255,0.88);
      --ink: #1f1f1b;
      --muted: #5c5b55;
      --line: #ddd3c3;
      --good: #146c43;
      --warn: #aa6a00;
      --bad: #a12626;
      --accent: #0c6a83;
      --shadow: 0 18px 50px rgba(53, 40, 16, 0.12);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(12,106,131,0.13), transparent 34%),
        radial-gradient(circle at top right, rgba(170,106,0,0.10), transparent 28%),
        linear-gradient(180deg, #f8f3eb 0%, #f2ebdf 100%);
      font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .wrap {{
      width: min(1240px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 28px 0 56px;
    }}
    .hero {{
      background: linear-gradient(135deg, rgba(255,255,255,0.95), rgba(252,247,240,0.88));
      border: 1px solid var(--line);
      border-radius: 28px;
      padding: 28px;
      box-shadow: var(--shadow);
    }}
    .hero h1 {{ margin: 0 0 10px; font-size: 34px; line-height: 1.1; }}
    .hero p {{ margin: 0; color: var(--muted); max-width: 820px; }}
    .meta, .grid, .artifact-grid {{ display: grid; gap: 16px; }}
    .meta {{ grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin-top: 20px; }}
    .grid {{ grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); margin-top: 24px; }}
    .artifact-grid {{ grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      min-width: 0;
      overflow: hidden;
    }}
    .meta-card {{
      display: flex;
      flex-direction: column;
      gap: 4px;
    }}
    .meta-value {{
      display: block;
      max-width: 100%;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      color: var(--ink);
    }}
    button.meta-value {{
      appearance: none;
      border: 0;
      padding: 0;
      background: transparent;
      cursor: pointer;
      font: inherit;
      text-align: left;
    }}
    button.meta-value:hover {{
      color: var(--accent);
      text-decoration: underline;
    }}
    .meta-value.mono {{
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
    a.meta-value {{
      color: var(--accent);
      font-weight: 650;
      text-decoration: none;
    }}
    a.meta-value:hover {{ text-decoration: underline; }}
    .copy-card {{
      position: relative;
    }}
    .copy-hint {{
      color: var(--muted);
      font-size: 12px;
      min-height: 18px;
    }}
    .copy-card.copied .copy-hint {{
      color: var(--good);
      font-weight: 700;
    }}
    .card-head {{
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }}
    h2 {{ margin: 28px 0 12px; font-size: 22px; }}
    h3 {{ margin: 0; font-size: 17px; }}
    p, li {{ color: var(--muted); }}
    ul, ol {{ margin: 0; padding-left: 18px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      overflow: hidden;
      box-shadow: var(--shadow);
    }}
    th, td {{
      padding: 12px 14px;
      text-align: left;
      border-bottom: 1px solid var(--line);
      vertical-align: top;
      overflow-wrap: anywhere;
    }}
    th {{ background: rgba(12,106,131,0.08); }}
    tr:last-child td {{ border-bottom: none; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      padding: 6px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.02em;
      background: rgba(12,106,131,0.10);
      color: var(--accent);
      white-space: nowrap;
    }}
    .sev-high {{ background: rgba(161,38,38,0.12); color: var(--bad); }}
    .sev-medium {{ background: rgba(170,106,0,0.14); color: var(--warn); }}
    .sev-low {{ background: rgba(20,108,67,0.12); color: var(--good); }}
    .shot {{
      margin-top: 14px;
      padding: 0;
      background: none;
      border: 0;
      cursor: zoom-in;
      text-align: left;
      width: 100%;
    }}
    .shot img {{
      display: block;
      width: 100%;
      border-radius: 16px;
      border: 1px solid var(--line);
      box-shadow: 0 10px 24px rgba(0,0,0,0.08);
    }}
    .artifact {{
      display: block;
    }}
    .artifact span {{
      display: block;
      margin-top: 8px;
      font-weight: 600;
      color: var(--ink);
    }}
    .pill-row {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 14px;
    }}
    .pill {{
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(12,106,131,0.07);
      border: 1px solid rgba(12,106,131,0.12);
      color: var(--ink);
      font-size: 13px;
    }}
    .overlay {{
      position: fixed;
      inset: 0;
      background: rgba(17,16,13,0.82);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      z-index: 1000;
      cursor: zoom-out;
    }}
    .overlay.open {{ display: flex; }}
    .overlay img {{
      max-width: min(96vw, 1600px);
      max-height: 92vh;
      border-radius: 18px;
      box-shadow: 0 30px 80px rgba(0,0,0,0.4);
    }}
    .footer-links {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }}
    .footer-links a {{
      color: var(--accent);
      text-decoration: none;
      font-weight: 600;
    }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
    @media (max-width: 720px) {{
      .wrap {{ width: min(100vw - 20px, 1240px); padding-top: 18px; }}
      .hero h1 {{ font-size: 28px; }}
      .card, .hero {{ border-radius: 20px; }}
      .meta {{ grid-template-columns: 1fr; }}
      .meta-value {{ white-space: normal; overflow-wrap: anywhere; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <div class="card-head">
        <div>
          <div class="pill-row">
            {render_badge(result["scenarioId"])}
            {render_badge(result["status"], severity_class(result["status"]))}
            {render_badge(result["destinationName"])}
          </div>
          <h1>FastBridge QA Dashboard</h1>
          <p>Static shareable report bundle for a live FastBridge execution, with embedded evidence, performance signals, and issue tracking.</p>
        </div>
      </div>
      <div class="meta">
        {render_meta_value("Run ID", result["runId"], mono=True)}
        {render_meta_value("Tester", result["tester"])}
        {render_meta_value("App URL", compact_url(app_url), href=app_url, title=app_url)}
        {render_copy_meta_value("Wallet", shorten_address(wallet_address), wallet_address, mono=True, title=wallet_address)}
        {render_meta_value("Requested Amount", result["bridgeAmount"] + " USDC")}
        <div class="card meta-card"><strong>Product Outcome</strong><br>{render_badge(result.get("productOutcome", "unknown"), severity_class(result.get("productOutcome", "unknown")))}</div>
        <div class="card meta-card"><strong>Harness Outcome</strong><br>{render_badge(result.get("harnessOutcome", "unknown"), severity_class(result.get("harnessOutcome", "unknown")))}</div>
        {explorer_card}
      </div>
    </section>

    <h2>Executive Summary</h2>
    <section class="card"><ul>{summary}</ul></section>

    <h2>Scenario Outcome</h2>
    <table>
      <tr><th>Field</th><th>Value</th></tr>
      <tr><td>Execution mode</td><td>{html.escape(result.get("executionMode", "full"))}</td></tr>
      <tr><td>Product outcome</td><td>{html.escape(result.get("productOutcome", "unknown"))}</td></tr>
      <tr><td>Harness outcome</td><td>{html.escape(result.get("harnessOutcome", "unknown"))}</td></tr>
      <tr><td>Quote parsed</td><td>{html.escape(str(result["quote"].get("quoteParsed")))}</td></tr>
      <tr><td>Source chain(s) used</td><td>{html.escape(result["completion"].get("sourceChains") or result["quote"].get("sourceSummary") or "unknown")}</td></tr>
      <tr><td>Amount spent</td><td>{html.escape(result["completion"].get("amountSpent") or result["quote"].get("amountSpent") or "unknown")}</td></tr>
      <tr><td>Amount received</td><td>{html.escape(result["completion"].get("amountReceived") or result["quote"].get("amountReceived") or "unknown")}</td></tr>
      <tr><td>Total fees</td><td>{html.escape(result["completion"].get("totalFees") or result["quote"].get("totalFees") or "unknown")}</td></tr>
      <tr><td>Gasless flow</td><td>{html.escape(str(result["usedGaslessFlow"]))}</td></tr>
      <tr><td>Wallet `eth_sendTransaction` count</td><td>{html.escape(str(result["walletInteraction"]["sendTransactionCount"]))}</td></tr>
    </table>

    <h2>Flow Results</h2>
    <section class="card"><ul>{worked}</ul></section>

    <h2>Flow Checkpoints</h2>
    <section class="grid">{''.join(checkpoint_cards)}</section>

    <h2>Expectations Assessment</h2>
    <table>
      <tr><th>Expectation</th><th>Status</th><th>Notes</th></tr>
      {expectations_rows}
    </table>

    <h2>Issues Found</h2>
    <section class="grid">{''.join(issue_cards) if issue_cards else '<article class="card"><p>No blocking issues captured in this run.</p></article>'}</section>

    <h2>Metrics</h2>
    <table>
      <tr><th>Metric</th><th>Value</th></tr>
      {metrics_rows}
    </table>

    <h2>Balance Evidence</h2>
    <section class="card">
      <p><strong>Initial unified balance:</strong> {html.escape(result["balances"].get("initialUnified") or "unknown")}</p>
      <p><strong>Final unified balance:</strong> {html.escape(result["balances"].get("finalUnified") or "unknown")}</p>
      {('<table><tr><th>Chain</th><th>Balance</th></tr>' + balance_rows + '</table>') if balance_rows else '<p>No parsed breakdown rows captured.</p>'}
    </section>

    <h2>Wallet Interaction</h2>
    <section class="card">
      <div class="pill-row">
        {''.join(f'<span class="pill">{html.escape(method)}</span>' for method in result["walletInteraction"]["methods"])}
      </div>
      <p><strong>Personal sign requests:</strong> {result["walletInteraction"]["personalSignCount"]}</p>
      <p><strong>Typed data sign requests:</strong> {result["walletInteraction"]["typedDataCount"]}</p>
      <p><strong>`eth_sendTransaction` requests:</strong> {result["walletInteraction"]["sendTransactionCount"]}</p>
    </section>

    <h2>Network And Console Signals</h2>
    <section class="card"><ul>{signals}</ul></section>

    <h2>Artifacts</h2>
    <section class="artifact-grid">{''.join(artifact_cards)}</section>

    <h2>Bundle Files</h2>
    <section class="card footer-links">
      <a href="./report.json">Open JSON</a>
      <a href="./report.md">Open Markdown</a>
      <a href="./expectations.html">Expectation Catalog</a>
    </section>
  </div>

  <div class="overlay" id="overlay" aria-hidden="true">
    <img alt="Expanded evidence view">
  </div>

  <script>
    const overlay = document.getElementById('overlay');
    const overlayImage = overlay.querySelector('img');
    document.querySelectorAll('[data-fullscreen="true"]').forEach((button) => {{
      button.addEventListener('click', () => {{
        const image = button.querySelector('img');
        if (!image) return;
        overlayImage.src = image.src;
        overlayImage.alt = image.alt || 'Expanded screenshot';
        overlay.classList.add('open');
        overlay.setAttribute('aria-hidden', 'false');
      }});
    }});
    overlay.addEventListener('click', () => {{
      overlay.classList.remove('open');
      overlay.setAttribute('aria-hidden', 'true');
      overlayImage.removeAttribute('src');
    }});
    document.addEventListener('keydown', (event) => {{
      if (event.key === 'Escape') {{
        overlay.classList.remove('open');
        overlay.setAttribute('aria-hidden', 'true');
        overlayImage.removeAttribute('src');
      }}
    }});
    const fallbackCopy = (text) => {{
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.left = '-9999px';
      document.body.appendChild(textarea);
      textarea.select();
      const copied = document.execCommand('copy');
      textarea.remove();
      return copied;
    }};
    const copyText = async (text) => {{
      if (navigator.clipboard && window.isSecureContext) {{
        try {{
          await navigator.clipboard.writeText(text);
          return true;
        }} catch (error) {{
          return fallbackCopy(text);
        }}
      }}
      return fallbackCopy(text);
    }};
    document.querySelectorAll('[data-copy]').forEach((button) => {{
      button.addEventListener('click', async () => {{
        const card = button.closest('.copy-card');
        const hint = card?.querySelector('.copy-hint');
        const originalText = hint?.textContent || 'Click to copy';
        const copied = await copyText(button.dataset.copy || '');
        if (hint) hint.textContent = copied ? 'Copied' : 'Copy failed';
        card?.classList.toggle('copied', copied);
        window.setTimeout(() => {{
          if (hint) hint.textContent = originalText;
          card?.classList.remove('copied');
        }}, 1600);
      }});
    }});
  </script>
</body>
</html>"""


def build_expectations_html(result: Dict[str, Any]) -> str:
    merged = expectations_with_na(result)
    rows = []
    for item in merged:
        status = item["status"]
        notes = item["notes"]
        rows.append(
            f"<tr><td><code>{html.escape(item['id'])}</code></td>"
            f"<td>{html.escape(item['category'])}</td>"
            f"<td>{html.escape(item['name'])}</td>"
            f"<td>{render_badge(status, severity_class(status))}</td>"
            f"<td>{html.escape(item['description'])}</td>"
            f"<td>{html.escape(notes)}</td></tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(result["scenarioId"])} - Expectation Catalog</title>
  <style>
    body {{
      margin: 0;
      background: #f7f1e7;
      color: #201f1c;
      font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    .wrap {{ width: min(1180px, calc(100vw - 32px)); margin: 0 auto; padding: 28px 0 56px; }}
    .hero, table {{
      background: rgba(255,255,255,0.9);
      border: 1px solid #ded4c3;
      border-radius: 22px;
      box-shadow: 0 16px 40px rgba(51, 41, 23, 0.10);
    }}
    .hero {{ padding: 24px; }}
    .hero h1 {{ margin: 0 0 8px; }}
    .hero p {{ margin: 0; color: #5f5b53; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 20px; overflow: hidden; }}
    th, td {{ padding: 12px 14px; text-align: left; border-bottom: 1px solid #e7ddcf; vertical-align: top; }}
    th {{ background: rgba(12,106,131,0.08); }}
    tr:last-child td {{ border-bottom: none; }}
    .badge {{
      display: inline-flex; padding: 5px 10px; border-radius: 999px; font-size: 12px; font-weight: 700;
      background: rgba(12,106,131,0.10); color: #0c6a83;
    }}
    .sev-high {{ background: rgba(161,38,38,0.12); color: #a12626; }}
    .sev-medium {{ background: rgba(170,106,0,0.14); color: #aa6a00; }}
    .sev-low {{ background: rgba(20,108,67,0.12); color: #146c43; }}
    a {{ color: #0c6a83; text-decoration: none; font-weight: 600; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  </style>
</head>
<body>
  <div class="wrap">
    <section class="hero">
      <p><a href="./index.html">Back to QA dashboard</a></p>
      <h1>Expectation Catalog</h1>
      <p>This page lists the expectation set for the scenario bundle and shows the observed status for the current run.</p>
      <p>Expectation IDs are stable identifiers, not a strict sequential list. The prefix shows category: <code>T</code> for timing, <code>D</code> for data correctness, and <code>U</code> for user experience. Gaps are normal when only part of the wider catalog is in scope for a given report bundle.</p>
    </section>
    <table>
      <tr><th>ID</th><th>Category</th><th>Name</th><th>Observed Status</th><th>Definition</th><th>Current Run Notes</th></tr>
      {''.join(rows)}
    </table>
  </div>
</body>
</html>"""


def main():
    private_key = os.environ.get("FASTBRIDGE_TEST_PRIVATE_KEY")
    if not private_key:
        private_key = getpass.getpass("Private key: ").strip()
    account_address = Account.from_key(private_key).address

    provider_calls: List[Dict[str, Any]] = []
    tx_log: List[Dict[str, Any]] = []
    console_errors: List[Dict[str, str]] = []
    page_errors: List[str] = []
    network_events: List[Dict[str, Any]] = []
    artifacts: List[Dict[str, str]] = []
    step_log: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    quote_waits: List[Dict[str, Any]] = []
    navigation_state: Dict[str, Any] = {}
    accept_attempted = False
    execution_attempted = False
    explorer_url: Optional[str] = None
    destination_name = humanize_destination(DESTINATION_SLUG)
    page_load_ms: Optional[int] = None
    quote_visible_ms: Optional[int] = None
    execution_completion_ms: Optional[int] = None
    execution_start: Optional[float] = None

    wallet = WalletHarness(account_address, private_key, provider_calls, tx_log)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context: BrowserContext = browser.new_context(viewport={"width": 1440, "height": 1100})
        context.expose_binding("pyProviderRequest", wallet.handler)
        context.add_init_script(INIT_SCRIPT.replace("%ADDRESS%", account_address).replace("%CHAIN_ID%", hex(8453)))
        page = context.new_page()

        page.on("console", lambda msg: console_errors.append({"type": msg.type, "text": msg.text}) if msg.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))

        def on_request(request):
            if any(host in request.url for host in ["avail.so"]):
                network_events.append({"type": "request", "method": request.method, "url": request.url})

        def on_response(response):
            if any(host in response.url for host in ["avail.so"]):
                event = {"type": "response", "status": response.status, "url": response.url}
                try:
                    if "application/json" in (response.headers.get("content-type") or ""):
                        event["json"] = response.json()
                except Exception:
                    pass
                network_events.append(event)

        page.on("request", on_request)
        page.on("response", on_response)

        page_load_start = time.monotonic()
        response = page.goto(BASE_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        page_load_ms = int((time.monotonic() - page_load_start) * 1000)
        navigation_state = {
            "waitUntil": "domcontentloaded",
            "responseStatus": response.status if response else None,
            "domContentLoadedMs": page_load_ms,
            "networkIdle": False,
            "networkIdleTimeoutMs": POST_LOAD_NETWORK_IDLE_TIMEOUT_MS,
        }
        try:
            page.wait_for_load_state("networkidle", timeout=POST_LOAD_NETWORK_IDLE_TIMEOUT_MS)
            navigation_state["networkIdle"] = True
            navigation_state["networkIdleMs"] = int((time.monotonic() - page_load_start) * 1000)
        except PlaywrightTimeoutError:
            navigation_state["networkIdleWarning"] = "Network idle was not reached after DOM content loaded; continuing because this app keeps background requests active."
        initial = wait_and_capture(page, f"{DESTINATION_SLUG}-initial", 2500)
        artifacts.append({"label": "Initial connected state", "path": initial["screenshot"]})
        step_log.append(initial)

        if "Initializing..." in initial["text"]:
            append_issue(
                issues,
                "Initialization Stuck",
                "high",
                "The bridge remained in an initializing state instead of loading balances and actions.",
                [
                    f"Open FastBridge on the {destination_name} route with a connected wallet.",
                    "Wait for the app to initialize.",
                    "Observe whether the CTA remains on Initializing... instead of loading balances.",
                ],
                issue_id="FB-P1-001",
                evidence=initial["screenshot"],
                root_cause="Initialization did not progress from startup state to quote-ready UI.",
            )

        if page.get_by_text("View Balance Breakdown").is_visible():
            page.get_by_text("View Balance Breakdown").click()
            breakdown = wait_and_capture(page, f"{DESTINATION_SLUG}-breakdown", 1500)
            artifacts.append({"label": "Balance breakdown", "path": breakdown["screenshot"]})
            step_log.append(breakdown)
        else:
            breakdown = {"text": ""}

        amount_input = page.locator("input[placeholder='Enter Amount']").first
        quote_start = time.monotonic()
        amount_input.fill(BRIDGE_AMOUNT)

        # EXP-T05: change amount and verify input remains responsive (UI not frozen)
        amount_change_ms: Optional[int] = None
        ui_responsive_after_change = False
        try:
            alternate_amount = str(round(float(BRIDGE_AMOUNT) + 0.01, 2))
            change_start = time.monotonic()
            amount_input.fill(alternate_amount)
            ui_responsive_after_change = (
                not amount_input.is_disabled()
                and amount_input.input_value() == alternate_amount
            )
            amount_change_ms = int((time.monotonic() - change_start) * 1000)
            amount_input.fill(BRIDGE_AMOUNT)
        except Exception:
            pass

        # EXP-T06: change amount mid-fetch and verify input stays enabled and re-fetch starts
        mid_fetch_input_enabled: Optional[bool] = None
        mid_fetch_refetch_started: Optional[bool] = None
        try:
            alternate_amount = str(round(float(BRIDGE_AMOUNT) + 0.01, 2))
            page.wait_for_timeout(400)  # let quote fetch begin but not complete
            amount_input.fill(alternate_amount)
            mid_fetch_input_enabled = not amount_input.is_disabled()
            # re-fetch started if the UI shows a loading indicator or the quote text resets
            page_text_mid = page.locator("body").inner_text()
            mid_fetch_refetch_started = (
                "Fetching" in page_text_mid
                or "fetching" in page_text_mid
                or amount_input.input_value() == alternate_amount
            )
            amount_input.fill(BRIDGE_AMOUNT)
        except Exception:
            pass

        quote_wait = wait_for_quote_ready(page)
        quote_waits.append({"stage": "after-amount", **quote_wait})
        after_amount = wait_and_capture(page, f"{DESTINATION_SLUG}-after-amount", 300)
        quote_visible_ms = int((time.monotonic() - quote_start) * 1000)
        artifacts.append({"label": "After amount input", "path": after_amount["screenshot"]})
        step_log.append(after_amount)

        accept_button = find_button(page, "Accept")
        if not button_is_ready(accept_button):
            bridge_button = find_button(page, "Bridge")
            if button_is_ready(bridge_button):
                bridge_button.click()
                review_quote_wait = wait_for_quote_ready(page, timeout_ms=10000)
                quote_waits.append({"stage": "review", **review_quote_wait})
                quote_visible_ms = int((time.monotonic() - quote_start) * 1000)
            review = wait_and_capture(page, f"{DESTINATION_SLUG}-review", 300)
            artifacts.append({"label": "Review state", "path": review["screenshot"]})
            step_log.append(review)
        else:
            review = {"text": ""}

        if STOP_BEFORE_EXECUTION:
            allowance = {"text": ""}
            post_approve = {"text": ""}
        else:
            accept_button = find_button(page, "Accept")
            if button_is_ready(accept_button):
                accept_attempted = True
                accept_button.click()
                allowance = wait_and_capture(page, f"{DESTINATION_SLUG}-allowance", 4000)
                artifacts.append({"label": "Allowance modal", "path": allowance["screenshot"]})
                step_log.append(allowance)
            else:
                allowance = {"text": ""}

            approve_button = find_button(page, "Approve Selected")
            if button_is_ready(approve_button):
                execution_attempted = True
                execution_start = time.monotonic()
                approve_button.click()
                post_approve = wait_and_capture(page, f"{DESTINATION_SLUG}-post-approve", 12000)
                artifacts.append({"label": "Post approval state", "path": post_approve["screenshot"]})
                step_log.append(post_approve)
            else:
                post_approve = {"text": ""}

        final_state = wait_and_capture(page, f"{DESTINATION_SLUG}-final", 3000)
        if execution_start is not None:
            execution_completion_ms = int((time.monotonic() - execution_start) * 1000)
        artifacts.append({"label": "Final UI state", "path": final_state["screenshot"]})
        step_log.append(final_state)
        explorer_link = page.locator("a", has_text="View Explorer").first
        if explorer_link.count() > 0:
            explorer_url = explorer_link.get_attribute("href")

        context.close()
        browser.close()

    worked = []
    summary = []
    final_text = final_state["text"]
    bridge_successful = "Bridge Successful!" in final_text and "Transaction Completed" in final_text
    used_gasless_flow = any(call["method"] in {"eth_signTypedData", "eth_signTypedData_v3", "eth_signTypedData_v4"} for call in provider_calls)
    user_facing_error_seen = any("Oops! Something went wrong. Please try again." in step["text"] for step in step_log)
    route_blocked_low_balance = any(
        "No eligible source chains available" in step.get("text", "")
        or "Insufficient" in step.get("text", "")
        for step in step_log
    )
    initial_quote = extract_best_quote(step_log)
    quote_parsed = bool(initial_quote.get("quoteParsed"))
    quote_ready_seen = any(item.get("ready") for item in quote_waits)
    sign_or_tx_attempted = used_gasless_flow or bool(tx_log)
    harness_outcome = (
        "COMPLETED"
        if quote_parsed or bridge_successful or route_blocked_low_balance
        else "PARSER_ERROR"
        if quote_ready_seen
        else "HARNESS_TIMEOUT"
    )
    product_outcome = (
        "PRODUCT_PASS"
        if bridge_successful
        else "NOT_ATTEMPTED"
        if STOP_BEFORE_EXECUTION
        else "LOW_BALANCE"
        if route_blocked_low_balance
        else "PRODUCT_FAIL"
        if accept_attempted or execution_attempted or sign_or_tx_attempted
        else "UNKNOWN"
    )
    completion = extract_completion_details(final_text)
    breakdown_rows = extract_breakdown_rows(breakdown["text"])
    total_usdc = extract_total_usdc(initial["text"])
    final_total_usdc = extract_total_usdc(final_text)
    quoted_receive = extract_numeric_amount(initial_quote.get("amountReceived"))
    actual_receive = extract_numeric_amount(completion.get("amountReceived"))
    quoted_fees = extract_numeric_amount(initial_quote.get("totalFees"))
    actual_fees = extract_numeric_amount(completion.get("totalFees"))
    initial_unified_numeric = extract_numeric_amount(total_usdc)
    final_unified_numeric = extract_numeric_amount(final_total_usdc)
    quote_evidence = next(
        (step.get("screenshot") for step in step_log if step.get("label") == initial_quote.get("evidenceLabel")),
        after_amount["screenshot"],
    )

    if total_usdc and "View Balance Breakdown" in initial["text"]:
        worked.append(f"Unified balance loaded in the live UI and surfaced a total of {total_usdc} USDC.")
    if "View Balance Breakdown" in initial["text"] and breakdown["text"]:
        worked.append("Balance breakdown opened successfully and exposed per-chain USDC balances.")
    if initial_quote.get("amountReceived"):
        worked.append(f"A real {BRIDGE_AMOUNT} USDC route to {destination_name} was quoted successfully with spend, receive, and fee information.")
    if "Set Token Allowances" in allowance["text"] or "Allowance approved" in final_text:
        worked.append("The execution flow advanced into the token allowance step for the selected source chain.")
    if tx_log:
        worked.append(f"The injected wallet broadcast {len(tx_log)} on-chain transaction(s).")
    if used_gasless_flow:
        worked.append("The wallet signed the typed-data request used during the allowance flow.")
    if bridge_successful:
        worked.append(f"The bridge completed successfully in the live UI and the destination balance updated to include {BRIDGE_AMOUNT} USDC on {destination_name}.")
    if bridge_successful and used_gasless_flow and not tx_log:
        worked.append("This route completed without a direct wallet-broadcast transaction, which is consistent with a gasless permit-plus-relayer flow.")

    if any("Failed to load resource: the server responded with a status of 400" in item["text"] for item in console_errors):
        append_issue(
            issues,
            "Background 400 Error Visible In Console",
            "medium",
            "The app still emits a recurring 400 resource error during normal usage. It did not block this route, but it remains noisy and could hide other issues.",
            [
                "Open FastBridge on any supported route.",
                "Open browser devtools console.",
                "Observe the recurring 400 resource error during startup.",
            ],
            issue_id="FB-P2-001",
            evidence=initial["screenshot"],
            root_cause="One or more startup requests are failing with a 400 without a user-facing surface.",
        )

    if any("401" in item["text"] for item in console_errors):
        append_issue(
            issues,
            "Background 401 Error During Nexus Setup",
            "medium",
            "A 401 resource error appeared during initialization, even though the route still progressed. Users do not get an explicit explanation for it in the UI.",
            [
                f"Connect a wallet on the {destination_name} route.",
                "Wait for Nexus initialization.",
                "Inspect browser console/network for 401 responses.",
            ],
            issue_id="FB-P2-002",
            evidence=initial["screenshot"],
            root_cause="Telemetry/logging endpoint requires authorization and fails noisily during normal flows.",
        )

    if execution_attempted and not tx_log and not bridge_successful:
        append_issue(
            issues,
            "Execution Attempt Did Not Submit A Transaction",
            "high",
            "The flow reached the execution stage and Approve Selected was clicked, but no on-chain transaction was broadcast from the wallet in this run. The bridge therefore did not complete end to end.",
            [
                f"Open the {destination_name} route with the funded wallet connected.",
                f"Enter {BRIDGE_AMOUNT} USDC.",
                "Click Bridge, then Accept, then Approve Selected.",
                "Observe whether an approval transaction is actually submitted and whether the flow continues.",
            ],
            issue_id="FB-P0-001",
            evidence=post_approve["screenshot"],
            root_cause="Execution stalled after approval selection and never reached a completed state.",
        )

    if any("Oops! Something went wrong. Please try again." in step["text"] for step in step_log):
        append_issue(
            issues,
            "Generic UI Error Hides Root Cause",
            "medium",
            "The app shows a generic failure message after execution errors. That message is visible to the user, but it does not explain whether the problem is allowance signing, bridge routing, wallet interaction, or backend failure.",
            [
                f"Open the {destination_name} route with the funded wallet connected.",
                f"Enter {BRIDGE_AMOUNT} USDC.",
                "Click Bridge, then Accept, then Approve Selected.",
                "If the operation fails, note that the UI shows only a generic error banner instead of a specific explanation.",
            ],
            issue_id="FB-P1-002",
            evidence=post_approve["screenshot"],
            root_cause="Execution failures are surfaced with a generic message instead of a task-specific explanation.",
        )

    if harness_outcome == "PARSER_ERROR":
        append_issue(
            issues,
            "Quote Parser Could Not Read Settled Quote",
            "medium",
            "The UI reached an Accept-ready quote state, but the harness could not parse spend, receive, and fee fields from the settled page text.",
            [
                f"Open the {destination_name} route with the funded wallet connected.",
                f"Enter {BRIDGE_AMOUNT} USDC.",
                "Wait until the Accept quote is visible.",
                "Compare the quote text against the parser labels in the harness.",
            ],
            issue_id="FB-H1-001",
            evidence=quote_evidence,
            root_cause=f"Missing quote field(s): {', '.join(initial_quote.get('parseErrors') or [])}.",
        )
    elif harness_outcome == "HARNESS_TIMEOUT" and not route_blocked_low_balance:
        append_issue(
            issues,
            "Quote Did Not Reach Accept-Ready State",
            "medium",
            "The harness did not observe a settled quote with an enabled Accept button before the timeout. This is classified as harness/product uncertainty, not a product execution failure.",
            [
                f"Open the {destination_name} route with the funded wallet connected.",
                f"Enter {BRIDGE_AMOUNT} USDC.",
                f"Wait up to {QUOTE_READY_TIMEOUT_MS} ms for the quote and Accept button.",
                "Inspect the captured final UI state before deciding whether the product or harness is at fault.",
            ],
            issue_id="FB-H1-002",
            evidence=final_state["screenshot"],
            root_cause="Quote readiness was not observed before the configured timeout.",
        )
    elif product_outcome == "UNKNOWN" and not STOP_BEFORE_EXECUTION and quote_parsed:
        append_issue(
            issues,
            "Execution Was Not Attempted After Quote",
            "medium",
            "A parseable quote was captured in full-execution mode, but the harness did not click Accept or reach the allowance/execution path.",
            [
                f"Open the {destination_name} route with the funded wallet connected.",
                f"Enter {BRIDGE_AMOUNT} USDC.",
                "Wait for the quote to settle.",
                "Confirm whether the Accept button is enabled and clickable.",
            ],
            issue_id="FB-H1-003",
            evidence=quote_evidence,
            root_cause="Harness flow control did not progress from settled quote to execution.",
        )

    summary.append(f"Unified balance aggregation is working for this wallet on the {destination_name} route.")
    if quote_parsed:
        summary.append(f"The quote path for sending {BRIDGE_AMOUNT} USDC to {destination_name} works and the information shown is complete enough to review the route.")
    elif route_blocked_low_balance:
        summary.append("The route was blocked before quote review because no eligible funded source chain was available for this destination.")
    else:
        summary.append("The tester did not capture a parseable settled quote, so quote correctness is unresolved rather than a product execution failure.")
    if STOP_BEFORE_EXECUTION:
        summary.append("Quote-only mode stopped before Accept / Approve Selected, so no live transaction was attempted by design.")
    elif bridge_successful:
        summary.append("The end-to-end bridge transaction completed successfully and the UI balance updated on the destination chain.")
    elif tx_log:
        summary.append("At least one live on-chain transaction was submitted during this run.")
    else:
        summary.append("No live on-chain transaction was submitted during this run, so execution remains incomplete.")
    if bridge_successful and used_gasless_flow and not tx_log:
        summary.append("This successful route appears to rely on a gasless signed-intent flow rather than a direct wallet-broadcast transaction.")

    checkpoints = [
        {
            "label": "Landing page and wallet state",
            "status": "PASS" if total_usdc else "FAIL",
            "evidence": initial["screenshot"],
            "notes": f"Unified balance shown as {total_usdc} USDC." if total_usdc else "Unified balance did not load.",
        },
        {
            "label": "Balance breakdown panel",
            "status": "PASS" if breakdown_rows else "FAIL",
            "evidence": breakdown.get("screenshot", initial["screenshot"]),
            "notes": f"{len(breakdown_rows)} per-chain entries captured." if breakdown_rows else "Per-chain balance breakdown did not appear.",
        },
        {
            "label": "Quote rendering",
            "status": "PASS" if quote_parsed else harness_outcome,
            "evidence": quote_evidence,
            "notes": f"Spend {initial_quote.get('amountSpent')}, receive {initial_quote.get('amountReceived')}, fees {initial_quote.get('totalFees')}." if quote_parsed else f"Quote parse incomplete: {', '.join(initial_quote.get('parseErrors') or ['unknown'])}.",
        },
        {
            "label": "Allowance review",
            "status": "NA" if STOP_BEFORE_EXECUTION else ("PASS" if "Set Token Allowances" in allowance["text"] else ("HARNESS_TIMEOUT" if product_outcome == "UNKNOWN" else "PARTIAL")),
            "evidence": allowance.get("screenshot", review.get("screenshot", after_amount["screenshot"])),
            "notes": "Quote-only mode stopped before allowance review." if STOP_BEFORE_EXECUTION else ("Allowance modal rendered with approval options." if "Set Token Allowances" in allowance["text"] else "Allowance modal was not observed in this run."),
        },
        {
            "label": "Execution completion",
            "status": "NA" if STOP_BEFORE_EXECUTION else ("PASS" if bridge_successful else ("LOW_BALANCE" if product_outcome == "LOW_BALANCE" else ("FAIL" if product_outcome == "PRODUCT_FAIL" else "HARNESS_TIMEOUT"))),
            "evidence": final_state["screenshot"],
            "notes": "Quote-only mode stopped before execution by design." if STOP_BEFORE_EXECUTION else ("Bridge successful state rendered with final amounts and explorer link." if bridge_successful else "Bridge did not reach a successful completion state."),
        },
    ]

    expectations = [
        {
            "id": "EXP-T02",
            "name": "Page load within 5s",
            "status": "PASS" if page_load_ms is not None and page_load_ms <= 5000 else "FAIL",
            "notes": f"Measured {page_load_ms} ms." if page_load_ms is not None else "Not captured.",
        },
        {
            "id": "EXP-T03",
            "name": "Balances refresh after completion",
            "status": "NA" if STOP_BEFORE_EXECUTION else ("PASS" if bridge_successful and initial_unified_numeric is not None and final_unified_numeric is not None and final_unified_numeric != initial_unified_numeric else ("NA" if not bridge_successful else "FAIL")),
            "notes": (
                "Quote-only mode stopped before completion, so balance refresh was not evaluated."
                if STOP_BEFORE_EXECUTION
                else f"Unified balance changed from {total_usdc} USDC to {final_total_usdc} USDC after completion."
                if bridge_successful and initial_unified_numeric is not None and final_unified_numeric is not None and final_unified_numeric != initial_unified_numeric
                else ("Run did not complete, so balance refresh could not be evaluated." if not bridge_successful else "Completion occurred, but the unified balance did not visibly change.")
            ),
        },
        {
            "id": "EXP-T04",
            "name": "Quote appears within 5s",
            "status": "PASS" if quote_parsed and quote_visible_ms is not None and quote_visible_ms <= 5000 else ("ANOMALY" if quote_parsed else harness_outcome),
            "notes": f"Measured {quote_visible_ms} ms." if quote_parsed and quote_visible_ms is not None else "A settled quote was not captured before timeout.",
        },
        {
            "id": "EXP-T05",
            "name": "Change in entered amount",
            "status": "PASS" if ui_responsive_after_change else ("FAIL" if amount_change_ms is not None else "NA"),
            "notes": (
                f"Input remained responsive after amount change ({amount_change_ms} ms)."
                if ui_responsive_after_change
                else "Input was unresponsive or disabled after changing the amount." if amount_change_ms is not None
                else "Amount change could not be tested."
            ),
        },
        {
            "id": "EXP-T06",
            "name": "Change in entered amount and fetching of quote",
            "status": (
                "PASS" if mid_fetch_input_enabled and mid_fetch_refetch_started
                else "FAIL" if mid_fetch_input_enabled is not None
                else "NA"
            ),
            "notes": (
                "Input stayed enabled and re-fetch triggered after mid-fetch amount change."
                if mid_fetch_input_enabled and mid_fetch_refetch_started
                else "Input was disabled during quote fetch." if mid_fetch_input_enabled is False
                else "Re-fetch did not start after mid-fetch amount change." if mid_fetch_input_enabled and not mid_fetch_refetch_started
                else "Mid-fetch amount change could not be tested."
            ),
        },
        {
            "id": "EXP-D01",
            "name": "Delivered output matches quoted output",
            "status": "NA" if STOP_BEFORE_EXECUTION else ("PASS" if bridge_successful and quoted_receive is not None and actual_receive is not None and abs(actual_receive - quoted_receive) < 0.000001 else ("NA" if not bridge_successful else "FAIL")),
            "notes": (
                "Quote-only mode stopped before completion, so delivered output was not evaluated."
                if STOP_BEFORE_EXECUTION
                else f"Quoted receive {initial_quote.get('amountReceived')}; actual receive {completion.get('amountReceived')}."
                if bridge_successful and quoted_receive is not None and actual_receive is not None
                else ("Run did not complete, so delivered output could not be evaluated." if not bridge_successful else "Quoted or actual receive amount was unavailable.")
            ),
        },
        {
            "id": "EXP-D02",
            "name": "Actual fees do not materially exceed quoted fees",
            "status": "NA" if STOP_BEFORE_EXECUTION else ("PASS" if bridge_successful and quoted_fees is not None and actual_fees is not None and actual_fees <= (quoted_fees * 1.05 + 0.000001) else ("NA" if not bridge_successful else "FAIL")),
            "notes": (
                "Quote-only mode stopped before completion, so actual fees were not evaluated."
                if STOP_BEFORE_EXECUTION
                else f"Quoted fees {initial_quote.get('totalFees')}; actual fees {completion.get('totalFees')}."
                if bridge_successful and quoted_fees is not None and actual_fees is not None
                else ("Run did not complete, so fee comparison could not be evaluated." if not bridge_successful else "Quoted or actual fee amount was unavailable.")
            ),
        },
        {
            "id": "EXP-D03",
            "name": "Unified balance and breakdown are visible",
            "status": "PASS" if total_usdc and breakdown_rows else "PARTIAL",
            "notes": f"Initial unified {total_usdc or 'unknown'} USDC; breakdown rows captured: {len(breakdown_rows)}.",
        },
        {
            "id": "EXP-U02",
            "name": "Source chain visible before confirm",
            "status": "PASS" if initial_quote.get("sourceSummary") else ("PARTIAL" if quote_parsed else harness_outcome),
            "notes": f"Source summary: {initial_quote.get('sourceSummary') or 'missing'}.",
        },
        {
            "id": "EXP-U01",
            "name": "Spend, receive, and fee details shown",
            "status": "PASS" if quote_parsed else harness_outcome,
            "notes": f"Spend {initial_quote.get('amountSpent')}, receive {initial_quote.get('amountReceived')}, fees {initial_quote.get('totalFees')}.",
        },
        {
            "id": "EXP-U03",
            "name": "Error messages are user-readable",
            "status": "FAIL" if user_facing_error_seen else "PASS",
            "notes": "A user-facing failure banner was shown, but it did not explain the specific cause or recovery path." if user_facing_error_seen else "No user-facing action failure was observed in this run.",
        },
        {
            "id": "EXP-T01",
            "name": "Execution completes within 30s",
            "status": "NA" if STOP_BEFORE_EXECUTION else ("PASS" if bridge_successful and execution_completion_ms is not None and execution_completion_ms <= 30000 else ("ANOMALY" if bridge_successful and execution_completion_ms is not None else ("LOW_BALANCE" if product_outcome == "LOW_BALANCE" else ("FAIL" if product_outcome == "PRODUCT_FAIL" else "HARNESS_TIMEOUT")))),
            "notes": "Quote-only mode stopped before execution by design." if STOP_BEFORE_EXECUTION else (f"Measured {execution_completion_ms} ms from approval click to final capture." if execution_completion_ms is not None else "Execution timing not captured."),
        },
    ]

    wallet_methods = sorted({call["method"] for call in provider_calls if call.get("method")})
    network_summary = [
        f"{len(console_errors)} console error(s) captured.",
        f"{len(page_errors)} page error(s) captured.",
        f"{sum(1 for event in network_events if event.get('status') == 401)} network 401 response(s) captured.",
        f"{sum(1 for event in network_events if event.get('status') == 400)} network 400 response(s) captured.",
    ]
    if navigation_state.get("networkIdleWarning"):
        network_summary.append(navigation_state["networkIdleWarning"])
    metrics = {
        "pageLoadMs": page_load_ms if page_load_ms is not None else "n/a",
        "navigation": navigation_state,
        "quoteVisibleMs": quote_visible_ms if quote_visible_ms is not None else "n/a",
        "quoteReadyTimeoutMs": QUOTE_READY_TIMEOUT_MS,
        "executionCompletionMs": execution_completion_ms if execution_completion_ms is not None else "n/a",
        "consoleErrorCount": len(console_errors),
        "pageErrorCount": len(page_errors),
        "networkEventCount": len(network_events),
    }

    result_status = (
        harness_outcome
        if harness_outcome != "COMPLETED" and not bridge_successful and product_outcome != "LOW_BALANCE"
        else "HARNESS_TIMEOUT"
        if product_outcome == "UNKNOWN"
        else "PRODUCT_PASS"
        if product_outcome == "PRODUCT_PASS" and not issues
        else "PARTIAL"
        if product_outcome == "PRODUCT_PASS"
        else product_outcome
    )

    result = {
        "scenarioId": f"FB-USDC-{DESTINATION_SLUG.upper()}-001",
        "executionMode": "quote-only" if STOP_BEFORE_EXECUTION else "full",
        "status": result_status,
        "productOutcome": product_outcome,
        "harnessOutcome": harness_outcome,
        "runId": RUN_ID,
        "timestampUtc": datetime.now(timezone.utc).isoformat(),
        "tester": "Codex Agent",
        "appUrl": BASE_URL,
        "destinationName": destination_name,
        "address": account_address,
        "bridgeAmount": BRIDGE_AMOUNT,
        "summary": summary,
        "worked": worked,
        "issues": issues,
        "metrics": metrics,
        "navigation": navigation_state,
        "quote": initial_quote,
        "quoteWaits": quote_waits,
        "completion": completion,
        "checkpoints": checkpoints,
        "expectations": expectations,
        "balances": {
            "initialUnified": f"{total_usdc} USDC" if total_usdc else None,
            "finalUnified": f"{final_total_usdc} USDC" if final_total_usdc else None,
            "breakdown": breakdown_rows,
        },
        "walletInteraction": {
            "methods": wallet_methods,
            "personalSignCount": sum(1 for call in provider_calls if call.get("method") == "personal_sign"),
            "typedDataCount": sum(1 for call in provider_calls if call.get("method") in {"eth_signTypedData", "eth_signTypedData_v3", "eth_signTypedData_v4"}),
            "sendTransactionCount": sum(1 for call in provider_calls if call.get("method") == "eth_sendTransaction"),
            "acceptAttempted": accept_attempted,
            "executionAttempted": execution_attempted,
        },
        "networkSummary": network_summary,
        "bridgeSuccessful": bridge_successful,
        "usedGaslessFlow": used_gasless_flow,
        "explorerUrl": explorer_url,
        "providerCalls": provider_calls,
        "txLog": tx_log,
        "consoleErrors": console_errors,
        "pageErrors": page_errors,
        "networkEvents": network_events,
        "stepLog": step_log,
        "artifacts": artifacts,
        "bundlePath": str(REPORT_BUNDLE_DIR),
    }

    exported = exportable_result(result)

    JSON_REPORT_PATH.write_text(json.dumps(exported, indent=2), encoding="utf-8")
    MD_REPORT_PATH.write_text(build_report_markdown(exported), encoding="utf-8")
    HTML_REPORT_PATH.write_text(build_report_html(result), encoding="utf-8")
    EXPECTATIONS_HTML_PATH.write_text(build_expectations_html(exported), encoding="utf-8")

    print(
        json.dumps(
            {
                **exported,
                "jsonReport": str(JSON_REPORT_PATH),
                "markdownReport": str(MD_REPORT_PATH),
                "htmlReport": str(HTML_REPORT_PATH),
                "expectationsHtml": str(EXPECTATIONS_HTML_PATH),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
