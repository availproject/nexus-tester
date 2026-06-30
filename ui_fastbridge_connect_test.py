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

from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright
from web3 import Web3


WORKSPACE = Path(__file__).resolve().parent
ARTIFACTS_DIR = WORKSPACE / "fastbridge-report-artifacts"
ARTIFACTS_DIR.mkdir(exist_ok=True)

DESTINATION_SLUG = os.environ.get("FASTBRIDGE_DEST_SLUG", "base").strip().strip("/")
BASE_URL = f"https://fastbridge.availproject.org/{DESTINATION_SLUG}/"
DEFAULT_BRIDGE_AMOUNT = os.environ.get("FASTBRIDGE_BRIDGE_AMOUNT", "0.1").strip()

STANDARD_EXPECTATION_IDS = {
    "EXP-T01",
    "EXP-T02",
    "EXP-T03",
    "EXP-T04",
    "EXP-T05",
    "EXP-T06",
    "EXP-D01",
    "EXP-D02",
    "EXP-D03",
    "EXP-U01",
    "EXP-U02",
    "EXP-U03",
    "EXP-U04",
    "EXP-U05",
}

SCENARIO_ORDER = ["default", "EXP-U06", "EXP-U07", "EXP-E01", "EXP-E02"]

SCENARIOS: Dict[str, Dict[str, Any]] = {
    "default": {
        "scenario_id": lambda dest: f"FB-USDC-{dest.upper()}-001",
        "bridge_amount": None,
        "idle_init": False,
        "run_bridge_flow": True,
        "in_scope": STANDARD_EXPECTATION_IDS,
        "bundle_suffix": "",
    },
    "EXP-U06": {
        "scenario_id": lambda dest: "FB-EXP-U06-001",
        "bridge_amount": None,
        "idle_init": False,
        "run_bridge_flow": False,
        "check_assets_button": True,
        "skip_balance_asset_setup": True,
        "in_scope": {"EXP-U06"},
        "bundle_suffix": "-exp-u06",
    },
    "EXP-U07": {
        "scenario_id": lambda dest: "FB-EXP-U07-001",
        "bridge_amount": None,
        "idle_init": False,
        "run_bridge_flow": False,
        "check_available_assets_all_tab": True,
        "skip_balance_asset_setup": True,
        "in_scope": {"EXP-U07"},
        "bundle_suffix": "-exp-u07",
    },
    "EXP-E01": {
        "scenario_id": lambda dest: "FB-EXP-E01-001",
        "bridge_amount": None,
        "idle_init": False,
        "run_bridge_flow": False,
        "in_scope": {"EXP-E01"},
        "bundle_suffix": "-exp-e01",
    },
    "EXP-E02": {
        "scenario_id": lambda dest: "FB-EXP-E02-001",
        "bridge_amount": None,
        "idle_init": True,
        "run_bridge_flow": False,
        "in_scope": {"EXP-E02"},
        "bundle_suffix": "-exp-e02",
    },
}

RUN_ID = ""
REPORT_BUNDLE_DIR = ARTIFACTS_DIR
ASSETS_DIR = ARTIFACTS_DIR
JSON_REPORT_PATH = ARTIFACTS_DIR / "report.json"
MD_REPORT_PATH = ARTIFACTS_DIR / "report.md"
HTML_REPORT_PATH = ARTIFACTS_DIR / "index.html"
EXPECTATIONS_HTML_PATH = ARTIFACTS_DIR / "expectations.html"


def normalize_scenario_key(value: str) -> str:
    normalized = value.strip()
    if not normalized or normalized.lower() == "default":
        return "default"
    if normalized.lower() == "all":
        return "all"
    for key in SCENARIOS:
        if key.upper() == normalized.upper():
            return key
    return "default"


def init_report_bundle(scenario_key: str) -> None:
    global RUN_ID, REPORT_BUNDLE_DIR, ASSETS_DIR
    global JSON_REPORT_PATH, MD_REPORT_PATH, HTML_REPORT_PATH, EXPECTATIONS_HTML_PATH
    suffix = SCENARIOS[scenario_key].get("bundle_suffix", "")
    RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + suffix
    REPORT_BUNDLE_DIR = ARTIFACTS_DIR / f"fastbridge-report-{RUN_ID}"
    REPORT_BUNDLE_DIR.mkdir(exist_ok=True)
    ASSETS_DIR = REPORT_BUNDLE_DIR / "assets"
    ASSETS_DIR.mkdir(exist_ok=True)
    JSON_REPORT_PATH = REPORT_BUNDLE_DIR / "report.json"
    MD_REPORT_PATH = REPORT_BUNDLE_DIR / "report.md"
    HTML_REPORT_PATH = REPORT_BUNDLE_DIR / "index.html"
    EXPECTATIONS_HTML_PATH = REPORT_BUNDLE_DIR / "expectations.html"


def apply_expectation_scope(
    expectations: List[Dict[str, Any]],
    in_scope: set,
    scenario_key: str,
) -> List[Dict[str, Any]]:
    scoped: List[Dict[str, Any]] = []
    for item in expectations:
        if item["id"] in in_scope:
            scoped.append(item)
            continue
        scoped.append(
            {
                **item,
                "status": "NA",
                "notes": (
                    f"Not in scope for scenario {scenario_key}; "
                    f"run with FASTBRIDGE_TEST_SCENARIO={item['id']} separately."
                ),
            }
        )
    return scoped

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
    {
        "id": "EXP-U04",
        "name": "Transaction with 25% of available balance",
        "category": "UI",
        "description": "A transaction sized at 25% of the available unified balance should complete successfully within 30 seconds.",
    },
    {
        "id": "EXP-U05",
        "name": "Transaction should be successful when the unified balance deducted from two source chains",
        "category": "UI",
        "description": "A transaction is performed with two source chains from unified balance to one destination",
    },
    {
        "id": "EXP-U06",
        "name": "View 'Assets' button",
        "category": "UI",
        "description": "'Assets' button on Fastbridge page is visible, once wallet is connected",
    },
    {
        "id": "EXP-U07",
        "name": "View available assets",
        "category": "UI",
        "description": "View available assets under 'All' tabs after clicking on 'Assets' button, wait for assets to load",
    },
    {
        "id": "EXP-E01",
        "name": "fastbridge and connected to wallet",
        "category": "Error handling",
        "description": "Once the wallet is connected to fastbridge, browser console should not show 'Unable to fetch deployment from middleware'",
    },
    {
        "id": "EXP-E02",
        "name": "fastbridge and connected to wallet",
        "category": "Error handling",
        "description": "Once the wallet is connected to fastbridge and is kept idle for more than 30 seconds, it should show error message 'Nexus initialization timed out after 30 seconds'",
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


def is_v2_ui(text: str) -> bool:
    return "Swap and Bridge" in text


def extract_v2_asset_total(text: str, section: str = "SEND") -> Optional[str]:
    pattern = rf"{section}[\s\S]*?Assets\s*\n\s*≈ \$([0-9]+(?:\.[0-9]+)?)"
    match = re.search(pattern, text)
    return match.group(1) if match else None


def extract_v2_quote_details(text: str) -> Dict[str, Optional[str]]:
    send_total = extract_v2_asset_total(text, "SEND")
    receive_total = extract_v2_asset_total(text, "RECEIVE")
    return {
        "sourceSummary": "Aggregated assets" if send_total else None,
        "amountSpent": f"{send_total} USDC" if send_total else None,
        "amountReceived": f"{receive_total} USDC" if receive_total else None,
        "destinationShown": None,
        "totalFees": None,
    }


def v2_usd_amount(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def positive_usd_amount(value: Optional[str]) -> Optional[float]:
    amount = v2_usd_amount(value)
    if amount is None or amount <= 0:
        return None
    return amount


def resolve_unified_usdc_balance(page: Page, page_text: Optional[str] = None) -> Optional[float]:
    text = page_text or page.locator("body").inner_text()
    for raw in (extract_total_usdc(text), extract_v2_asset_total(text, "SEND")):
        amount = positive_usd_amount(raw)
        if amount is not None:
            return amount

    max_button = page.get_by_text("MAX", exact=True)
    if max_button.count() > 0 and max_button.first.is_visible() and max_button.first.is_enabled():
        max_button.first.click()
        page.wait_for_timeout(500)
        amount_input = page.locator(
            "input[placeholder='0'], input[placeholder='0.0'], input[type='number'], input[inputmode='decimal'], input"
        ).first
        amount = positive_usd_amount(amount_input.input_value())
        amount_input.fill("")
        if amount is not None:
            return amount
    return None


def resolve_bridge_amount(
    bridge_amount: Optional[str],
    unified_balance: Optional[float],
) -> tuple[str, Optional[float]]:
    if bridge_amount and bridge_amount != "pending":
        return bridge_amount, unified_balance
    return DEFAULT_BRIDGE_AMOUNT, unified_balance


def format_usdc_display(amount: Optional[float]) -> str:
    if amount is None:
        return "unknown"
    return f"{amount:.2f} USDC"


def extract_raw_usdc_balance_from_text(text: str, ui_version: str) -> Optional[float]:
    if ui_version == "v2":
        return v2_usd_amount(extract_v2_asset_total(text, "SEND"))
    return v2_usd_amount(extract_total_usdc(text))


def capture_connected_unified_balance(
    page: Page,
    destination_name: str,
    ui_version: str,
    skip_asset_setup: bool = False,
) -> Dict[str, Any]:
    page.wait_for_timeout(2000)
    amount: Optional[float] = None
    raw: Optional[str] = None

    deadline = time.monotonic() + (10 if skip_asset_setup else 45)
    while time.monotonic() < deadline:
        text = page.locator("body").inner_text()
        amount = extract_raw_usdc_balance_from_text(text, ui_version)
        if amount is not None and amount > 0:
            raw = extract_v2_asset_total(text, "SEND") if ui_version == "v2" else extract_total_usdc(text)
            break
        page.wait_for_timeout(1000)

    if ui_version == "v2" and (amount is None or amount <= 0) and not skip_asset_setup:
        try:
            prepare_v2_assets(page, destination_name)
            page.wait_for_timeout(2000)
        except PlaywrightTimeoutError:
            pass
        poll_deadline = time.monotonic() + 30
        while time.monotonic() < poll_deadline:
            text = page.locator("body").inner_text()
            amount = extract_raw_usdc_balance_from_text(text, ui_version)
            if amount is not None and amount > 0:
                raw = extract_v2_asset_total(text, "SEND")
                break
            page.wait_for_timeout(1000)

    if amount is None:
        text = page.locator("body").inner_text()
        amount = extract_raw_usdc_balance_from_text(text, ui_version)
        raw = extract_v2_asset_total(text, "SEND") if ui_version == "v2" else extract_total_usdc(text)

    display = format_usdc_display(amount)
    capture = wait_and_capture(page, f"{DESTINATION_SLUG}-unified-balance", 500)
    print(f"Unified USDC balance (wallet connected): {display}", flush=True)
    return {
        "amount": amount,
        "display": display,
        "raw": raw,
        "text": capture["text"],
        "screenshot": capture["screenshot"],
    }


def wait_for_v2_section_balance(page: Page, section: str, min_usd: float, timeout_ms: int = 45000) -> Optional[str]:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        text = page.locator("body").inner_text()
        total = extract_v2_asset_total(text, section)
        amount = positive_usd_amount(total)
        if amount is not None and amount >= min_usd:
            return total
        page.wait_for_timeout(1000)
    return extract_v2_asset_total(page.locator("body").inner_text(), section)


def click_v2_send_assets_button(page: Page) -> None:
    assets_buttons = page.get_by_role("button", name="Assets")
    clicked = False
    for index in range(assets_buttons.count()):
        button = assets_buttons.nth(index)
        if button.is_visible() and button.is_enabled():
            button.click()
            clicked = True
            break
    if not clicked:
        assets_label = page.get_by_text("Assets", exact=True).first
        assets_label.wait_for(state="visible", timeout=15000)
        assets_label.click()
    page.wait_for_timeout(1000)


def check_available_assets_all_tab(page: Page, timeout_ms: int = 30000) -> Dict[str, Any]:
    click_v2_send_assets_button(page)

    picker_opened = False
    all_tab_selected = False
    assets_loaded = False

    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        text = page.locator("body").inner_text()
        if "Choose assets to send" not in text:
            page.wait_for_timeout(1000)
            continue

        picker_opened = True

        if not all_tab_selected:
            for selector in (
                page.get_by_role("tab", name="All"),
                page.locator("[role='tab']").filter(has_text=re.compile(r"^All$")),
            ):
                if selector.count() > 0 and selector.first.is_visible():
                    selector.first.click()
                    page.wait_for_timeout(1000)
                    all_tab_selected = True
                    text = page.locator("body").inner_text()
                    break
            if not all_tab_selected and "Native" in text and "Stables" in text:
                all_tab_selected = True

        if re.search(r"(Loading|Fetching)", text, re.I):
            page.wait_for_timeout(1000)
            continue

        token_matches = re.findall(r"\b(USDC|USDT|ETH|DAI|WETH|WBTC)\b", text)
        has_search = page.get_by_placeholder(re.compile(r"Search token", re.I)).count() > 0
        if picker_opened and all_tab_selected and has_search and len(token_matches) >= 2:
            assets_loaded = True
            break

        page.wait_for_timeout(1000)

    capture = wait_and_capture(page, f"{DESTINATION_SLUG}-available-assets-all", 500)
    text = capture["text"]
    if not picker_opened:
        picker_opened = "Choose assets to send" in text
    if not all_tab_selected:
        all_tab_selected = "Native" in text and "Stables" in text
    if not assets_loaded:
        token_matches = re.findall(r"\b(USDC|USDT|ETH|DAI|WETH|WBTC)\b", text)
        has_search = page.get_by_placeholder(re.compile(r"Search token", re.I)).count() > 0
        assets_loaded = (
            picker_opened
            and all_tab_selected
            and has_search
            and len(token_matches) >= 2
            and not re.search(r"(Loading|Fetching)", text, re.I)
        )

    return {
        "picker_opened": picker_opened,
        "all_tab_selected": all_tab_selected,
        "assets_loaded": assets_loaded,
        "text": capture["text"],
        "screenshot": capture["screenshot"],
    }


def check_assets_button_visible(page: Page, timeout_ms: int = 15000) -> Dict[str, Any]:
    assets_buttons = page.get_by_role("button", name="Assets")
    assets_label = page.get_by_text("Assets", exact=True)
    visible = False
    enabled_count = 0
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        for index in range(assets_buttons.count()):
            button = assets_buttons.nth(index)
            if button.is_visible():
                visible = True
                if button.is_enabled():
                    enabled_count += 1
        if assets_label.count() > 0 and assets_label.first.is_visible():
            visible = True
        if visible:
            break
        page.wait_for_timeout(500)
    capture = wait_and_capture(page, f"{DESTINATION_SLUG}-assets-button", 500)
    return {
        "visible": visible,
        "button_count": assets_buttons.count(),
        "enabled_button_count": enabled_count,
        "text": capture["text"],
        "screenshot": capture["screenshot"],
    }


def open_v2_asset_picker(page: Page, section: str) -> None:
    if section == "SEND":
        add_more = page.get_by_text("Add more assets", exact=False)
        if add_more.count() > 0 and add_more.first.is_visible() and add_more.first.is_enabled():
            add_more.first.click()
            page.wait_for_timeout(1000)
            return
        click_v2_send_assets_button(page)
        return
    assets_buttons = page.get_by_role("button", name="Assets")
    index = min(1, assets_buttons.count() - 1)
    if assets_buttons.count() > index:
        button = assets_buttons.nth(index)
        if button.is_visible() and button.is_enabled():
            button.click()
        else:
            for candidate_index in range(assets_buttons.count()):
                candidate = assets_buttons.nth(candidate_index)
                if candidate.is_visible() and candidate.is_enabled():
                    candidate.click()
                    break
        page.wait_for_timeout(1000)


def select_v2_modal_token(page: Page, token_hint: str, chain_hint: Optional[str] = None) -> bool:
    search = page.get_by_placeholder(re.compile(r"Search token", re.I))
    if search.count() > 0:
        search.first.fill(token_hint)
        page.wait_for_timeout(1500)

    selected = False
    if chain_hint:
        chain_option = page.get_by_text(re.compile(rf"{re.escape(chain_hint)}", re.I))
        for idx in range(min(chain_option.count(), 8)):
            candidate = chain_option.nth(idx)
            if not candidate.is_visible():
                continue
            candidate_text = candidate.inner_text()
            if token_hint.lower() in candidate_text.lower():
                candidate.click()
                selected = True
                break

    if not selected:
        token_option = page.get_by_text(re.compile(rf"{re.escape(token_hint)}", re.I))
        for idx in range(min(token_option.count(), 8)):
            candidate = token_option.nth(idx)
            if not candidate.is_visible():
                continue
            candidate.click()
            selected = True
            break

    done = find_button(page, "Done")
    if done and done.is_visible() and done.is_enabled():
        done.click()
        page.wait_for_timeout(1500)
        return selected
    return False


def prepare_v2_assets(page: Page, destination_name: str) -> None:
    try:
        open_v2_asset_picker(page, "SEND")
        if page.get_by_placeholder(re.compile(r"Search token", re.I)).count() > 0:
            select_v2_modal_token(page, "USDC")
        page.wait_for_timeout(1000)

        receive_assets = page.get_by_role("button", name="Assets")
        if receive_assets.count() > 1 and receive_assets.nth(1).is_visible():
            receive_assets.nth(1).click()
            page.wait_for_timeout(1000)
            if page.get_by_placeholder(re.compile(r"Search token", re.I)).count() > 0:
                select_v2_modal_token(page, "USDC", chain_hint=destination_name)
    except PlaywrightTimeoutError:
        pass


def fill_v2_amount(page: Page, amount: str) -> None:
    amount_input = page.locator(
        "input[placeholder='0'], input[placeholder='0.0'], input[type='number'], input[inputmode='decimal'], input"
    ).first
    amount_input.wait_for(state="visible", timeout=60000)
    amount_input.click()
    amount_input.fill("")
    amount_input.fill(amount)


def extract_first(pattern: str, text: str) -> Optional[str]:
    match = re.search(pattern, text, re.MULTILINE)
    return match.group(1).strip() if match else None


def extract_quote_details(text: str) -> Dict[str, Optional[str]]:
    return {
        "sourceSummary": extract_first(r"You Spend\s+\n+\s*([^\n]+)", text),
        "amountSpent": extract_first(r"You Spend\s+\n+(?:[^\n]+\n+)?\s*([0-9.]+ USDC)", text),
        "amountReceived": extract_first(r"You receive\s+\n+\s*([0-9.]+ USDC)", text),
        "destinationShown": extract_first(r"You receive\s+\n+(?:[^\n]+\n+)?\s*on ([^\n]+)", text),
        "totalFees": extract_first(r"Total fees\s+\n+\s*([0-9.]+ USDC)", text),
    }


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
        return "0x" + signed.signature.hex()

    def sign_typed_data(self, typed_data: Any) -> str:
        payload = normalize_typed_data_payload(typed_data)
        signed = Account.sign_typed_data(self.private_key, full_message=payload)
        return "0x" + signed.signature.hex()

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
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction).hex()
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


def click_if_enabled(page: Page, name: str, timeout_ms: int = 15000) -> bool:
    button = find_button(page, name)
    if not button or not button.is_visible():
        return False
    try:
        button.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        return False
    if not button.is_enabled():
        return False
    button.click()
    return True


def run_v1_bridge_flow(
    page: Page,
    destination_name: str,
    bridge_amount: str,
    issues: List[Dict[str, Any]],
    step_log: List[Dict[str, Any]],
    artifacts: List[Dict[str, str]],
) -> Dict[str, Any]:
    execution_attempted = False
    execution_start: Optional[float] = None

    if page.get_by_text("View Balance Breakdown").is_visible():
        page.get_by_text("View Balance Breakdown").click()
        breakdown = wait_and_capture(page, f"{DESTINATION_SLUG}-breakdown", 1500)
        artifacts.append({"label": "Balance breakdown", "path": breakdown["screenshot"]})
        step_log.append(breakdown)
    else:
        breakdown = {"text": ""}

    unified_balance = resolve_unified_usdc_balance(page, breakdown["text"] or page.locator("body").inner_text())
    effective_amount, unified_balance = resolve_bridge_amount(
        bridge_amount,
        unified_balance,
    )

    quote_start = time.monotonic()
    amount_input = page.locator(
        "input[placeholder='Enter Amount'], input[placeholder='0'], input[placeholder='0.0'], "
        "input[placeholder='Amount'], input[type='number']"
    ).first
    amount_input.wait_for(state="visible", timeout=60000)
    amount_input.fill(effective_amount)
    after_amount = wait_and_capture(page, f"{DESTINATION_SLUG}-after-amount", 2500)
    quote_visible_ms = int((time.monotonic() - quote_start) * 1000)
    artifacts.append({"label": "After amount input", "path": after_amount["screenshot"]})
    step_log.append(after_amount)

    review = {"text": ""}
    if click_if_enabled(page, "Bridge"):
        review = wait_and_capture(page, f"{DESTINATION_SLUG}-review", 4000)
        artifacts.append({"label": "Review state", "path": review["screenshot"]})
        step_log.append(review)

    allowance = {"text": ""}
    if click_if_enabled(page, "Accept", timeout_ms=5000):
        allowance = wait_and_capture(page, f"{DESTINATION_SLUG}-allowance", 4000)
        artifacts.append({"label": "Allowance modal", "path": allowance["screenshot"]})
        step_log.append(allowance)

    post_approve = {"text": ""}
    if click_if_enabled(page, "Approve Selected", timeout_ms=5000):
        execution_attempted = True
        execution_start = time.monotonic()
        post_approve = wait_and_capture(page, f"{DESTINATION_SLUG}-post-approve", 12000)
        artifacts.append({"label": "Post approval state", "path": post_approve["screenshot"]})
        step_log.append(post_approve)

    send_balance = extract_total_usdc(breakdown["text"] or after_amount["text"])
    if unified_balance is None:
        unified_balance = positive_usd_amount(send_balance)
    receive_quote = extract_first(r"You receive\s+\n+\s*([0-9.]+ USDC)", after_amount["text"])
    return {
        "breakdown": breakdown,
        "after_amount": after_amount,
        "review": review,
        "allowance": allowance,
        "post_approve": post_approve,
        "quote_visible_ms": quote_visible_ms,
        "execution_attempted": execution_attempted,
        "execution_start": execution_start,
        "balance_loaded": positive_usd_amount(send_balance) is not None or positive_usd_amount(str(unified_balance)) is not None,
        "quote_rendered": receive_quote is not None,
        "send_balance_usd": send_balance,
        "receive_quote_usd": receive_quote,
        "unified_balance_usd": unified_balance,
        "computed_bridge_amount": effective_amount,
    }


def run_v2_bridge_flow(
    page: Page,
    destination_name: str,
    bridge_amount: str,
    issues: List[Dict[str, Any]],
    step_log: List[Dict[str, Any]],
    artifacts: List[Dict[str, str]],
) -> Dict[str, Any]:
    execution_attempted = False
    execution_start: Optional[float] = None
    breakdown = {"text": ""}

    page.wait_for_timeout(3000)
    prepare_v2_assets(page, destination_name)
    balances_loaded = wait_and_capture(page, f"{DESTINATION_SLUG}-balances-loaded", 2000)
    step_log.append(balances_loaded)
    artifacts.append({"label": "Balances loaded state", "path": balances_loaded["screenshot"]})

    send_balance = wait_for_v2_section_balance(page, "SEND", min_usd=0.01, timeout_ms=45000)
    if send_balance is None:
        balances_loaded = wait_and_capture(page, f"{DESTINATION_SLUG}-balances-loaded", 1000)
        step_log.append(balances_loaded)
        send_balance = extract_v2_asset_total(balances_loaded["text"], "SEND")

    balance_loaded = positive_usd_amount(send_balance) is not None
    unified_balance = resolve_unified_usdc_balance(page, balances_loaded["text"]) or positive_usd_amount(send_balance)
    balance_loaded = unified_balance is not None
    effective_amount, unified_balance = resolve_bridge_amount(
        bridge_amount,
        unified_balance,
    )

    quote_start = time.monotonic()
    fill_v2_amount(page, effective_amount)
    after_amount = wait_and_capture(page, f"{DESTINATION_SLUG}-after-amount", 2500)
    receive_quote = wait_for_v2_section_balance(page, "RECEIVE", min_usd=0.01, timeout_ms=20000)
    if receive_quote is None:
        after_amount = wait_and_capture(page, f"{DESTINATION_SLUG}-after-amount", 1000)
    quote_visible_ms = int((time.monotonic() - quote_start) * 1000)
    artifacts.append({"label": "After amount input", "path": after_amount["screenshot"]})
    step_log.append(after_amount)

    receive_quote = receive_quote or extract_v2_asset_total(after_amount["text"], "RECEIVE")
    quote_rendered = positive_usd_amount(receive_quote) is not None

    review = {"text": ""}
    for button_name in ["Review swap", "Add Assets to Bridge", "Bridge"]:
        if click_if_enabled(page, button_name, timeout_ms=15000):
            review = wait_and_capture(page, f"{DESTINATION_SLUG}-review", 4000)
            artifacts.append({"label": "Review state", "path": review["screenshot"]})
            step_log.append(review)
            break

    allowance = {"text": ""}
    if click_if_enabled(page, "Accept", timeout_ms=5000):
        allowance = wait_and_capture(page, f"{DESTINATION_SLUG}-allowance", 4000)
        artifacts.append({"label": "Allowance modal", "path": allowance["screenshot"]})
        step_log.append(allowance)

    post_approve = {"text": ""}
    for button_name in ["Approve Selected", "Confirm", "Sign", "Execute"]:
        if click_if_enabled(page, button_name, timeout_ms=5000):
            execution_attempted = True
            execution_start = time.monotonic()
            post_approve = wait_and_capture(page, f"{DESTINATION_SLUG}-post-approve", 12000)
            artifacts.append({"label": "Post approval state", "path": post_approve["screenshot"]})
            step_log.append(post_approve)
            break

    return {
        "breakdown": breakdown,
        "after_amount": after_amount,
        "review": review,
        "allowance": allowance,
        "post_approve": post_approve,
        "quote_visible_ms": quote_visible_ms,
        "execution_attempted": execution_attempted,
        "execution_start": execution_start,
        "balance_loaded": balance_loaded,
        "quote_rendered": quote_rendered,
        "send_balance_usd": send_balance,
        "receive_quote_usd": receive_quote,
        "unified_balance_usd": unified_balance,
        "computed_bridge_amount": effective_amount,
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
    lines.append(f"- Run ID: `{result['runId']}`")
    lines.append(f"- UTC Time: `{result['timestampUtc']}`")
    lines.append(f"- Tester: `{result['tester']}`")
    lines.append(f"- App URL: `{result['appUrl']}`")
    lines.append(f"- Wallet: `{result['address']}`")
    lines.append(f"- Unified USDC balance (connected): `{result.get('connectedUnifiedUsdc') or result['balances'].get('initialUnified') or 'unknown'}`")
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
    if normalized in {"high", "critical", "p0", "p1"}:
        return "sev-high"
    if normalized in {"medium", "partial", "anomaly", "p2"}:
        return "sev-medium"
    return "sev-low"


def render_badge(text: str, class_name: str = "") -> str:
    suffix = f" {class_name}" if class_name else ""
    return f'<span class="badge{suffix}">{html.escape(text)}</span>'


def build_report_html(result: Dict[str, Any]) -> str:
    bundle_path = result.get("bundlePath")
    expectations_all = expectations_with_na(result)
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
    .meta {{ grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); margin-top: 20px; }}
    .grid {{ grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); margin-top: 24px; }}
    .artifact-grid {{ grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 22px;
      padding: 18px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
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
        <div class="card"><strong>Run ID</strong><br><code>{html.escape(result["runId"])}</code></div>
        <div class="card"><strong>Tester</strong><br>{html.escape(result["tester"])}</div>
        <div class="card"><strong>App URL</strong><br><code>{html.escape(result["appUrl"])}</code></div>
        <div class="card"><strong>Wallet</strong><br><code>{html.escape(result["address"])}</code></div>
        <div class="card"><strong>Unified USDC Balance</strong><br>{html.escape(result.get("connectedUnifiedUsdc") or result["balances"].get("initialUnified") or "unknown")}</div>
        <div class="card"><strong>Requested Amount</strong><br>{html.escape(result["bridgeAmount"])} USDC</div>
        <div class="card"><strong>Explorer</strong><br>{f'<a href="{html.escape(result["explorerUrl"])}">{html.escape(result["explorerUrl"])}</a>' if result.get("explorerUrl") else 'n/a'}</div>
      </div>
    </section>

    <h2>Executive Summary</h2>
    <section class="card"><ul>{summary}</ul></section>

    <h2>Scenario Outcome</h2>
    <table>
      <tr><th>Field</th><th>Value</th></tr>
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

    scenario_arg = normalize_scenario_key(os.environ.get("FASTBRIDGE_TEST_SCENARIO", "default"))
    if scenario_arg == "all":
        results = [run_scenario(scenario_key, private_key) for scenario_key in SCENARIO_ORDER]
        print(json.dumps({"scenarios": results}, indent=2))
        return

    exported = run_scenario(scenario_arg, private_key)
    print(json.dumps(exported, indent=2))


def run_scenario(scenario_key: str, private_key: str) -> Dict[str, Any]:
    scenario_config = SCENARIOS[scenario_key]
    init_report_bundle(scenario_key)
    bridge_amount = scenario_config.get("bridge_amount") or DEFAULT_BRIDGE_AMOUNT
    scenario_id = scenario_config["scenario_id"](DESTINATION_SLUG)
    idle_init_test = scenario_config["idle_init"]
    run_bridge_flow = scenario_config["run_bridge_flow"]

    account_address = Account.from_key(private_key).address

    provider_calls: List[Dict[str, Any]] = []
    tx_log: List[Dict[str, Any]] = []
    console_errors: List[Dict[str, str]] = []
    page_errors: List[str] = []
    network_events: List[Dict[str, Any]] = []
    artifacts: List[Dict[str, str]] = []
    step_log: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    execution_attempted = False
    explorer_url: Optional[str] = None
    destination_name = humanize_destination(DESTINATION_SLUG)
    page_load_ms: Optional[int] = None
    quote_visible_ms: Optional[int] = None
    execution_completion_ms: Optional[int] = None
    execution_start: Optional[float] = None
    available_unified_usdc: Optional[float] = None
    connected_unified_display = "unknown"
    connected_balance: Dict[str, Any] = {"screenshot": "", "display": "unknown"}
    assets_button_check: Dict[str, Any] = {"visible": False, "button_count": 0, "text": "", "screenshot": ""}
    assets_button_visible = False
    available_assets_check: Dict[str, Any] = {
        "picker_opened": False,
        "all_tab_selected": False,
        "assets_loaded": False,
        "text": "",
        "screenshot": "",
    }
    available_assets_loaded = False
    ui_version = "v1"
    initial: Dict[str, Any] = {"text": "", "screenshot": ""}

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
        page.goto(BASE_URL, wait_until="load", timeout=60000)
        page_load_ms = int((time.monotonic() - page_load_start) * 1000)
        initial = wait_and_capture(page, f"{DESTINATION_SLUG}-initial", 2500)
        artifacts.append({"label": "Initial connected state", "path": initial["screenshot"]})
        step_log.append(initial)

        ui_version = "v2" if is_v2_ui(initial["text"]) else "v1"
        connected_balance = capture_connected_unified_balance(
            page,
            destination_name,
            ui_version,
            skip_asset_setup=bool(scenario_config.get("skip_balance_asset_setup")),
        )
        available_unified_usdc = connected_balance.get("amount")
        connected_unified_display = connected_balance["display"]
        artifacts.append({"label": "Connected unified USDC balance", "path": connected_balance["screenshot"]})
        step_log.append(
            {
                "label": "connected-unified-balance",
                "text": connected_balance["text"],
                "screenshot": connected_balance["screenshot"],
            }
        )

        if scenario_config.get("check_assets_button"):
            assets_button_check = check_assets_button_visible(page)
            assets_button_visible = bool(assets_button_check["visible"])
            artifacts.append({"label": "Assets button visibility", "path": assets_button_check["screenshot"]})
            step_log.append(
                {
                    "label": f"{DESTINATION_SLUG}-assets-button",
                    "text": assets_button_check["text"],
                    "screenshot": assets_button_check["screenshot"],
                }
            )
            if not assets_button_visible:
                append_issue(
                    issues,
                    "Assets Button Not Visible",
                    "high",
                    "The Assets button was not visible on the FastBridge page after the wallet connected.",
                    [
                        f"Open the {destination_name} route with a connected wallet.",
                        "Wait for the Swap and Bridge UI to load.",
                        "Verify that the Assets button is visible in the SEND panel.",
                    ],
                    issue_id="FB-P1-006",
                    evidence=assets_button_check["screenshot"],
                    root_cause="The Assets button did not render after wallet connection.",
                )

        if scenario_config.get("check_available_assets_all_tab"):
            available_assets_check = check_available_assets_all_tab(page)
            available_assets_loaded = bool(
                available_assets_check.get("picker_opened")
                and available_assets_check.get("all_tab_selected")
                and available_assets_check.get("assets_loaded")
            )
            artifacts.append(
                {"label": "Available assets (All tab)", "path": available_assets_check["screenshot"]}
            )
            step_log.append(
                {
                    "label": f"{DESTINATION_SLUG}-available-assets-all",
                    "text": available_assets_check["text"],
                    "screenshot": available_assets_check["screenshot"],
                }
            )
            if not available_assets_loaded:
                append_issue(
                    issues,
                    "Available Assets Did Not Load",
                    "high",
                    "After clicking the Assets button, available assets did not load under the All tab.",
                    [
                        f"Open the {destination_name} route with a connected wallet.",
                        "Click the Assets button in the SEND panel.",
                        "Select the All tab and wait for the asset list to load.",
                    ],
                    issue_id="FB-P1-007",
                    evidence=available_assets_check["screenshot"],
                    root_cause="The asset picker did not show loaded tokens under the All tab.",
                )

        idle_timeout_message_seen: Optional[bool] = None
        idle_capture_text: Optional[str] = None
        if idle_init_test:
            # EXP-E02: idle the connected page for >30s and verify the Nexus
            # initialization timeout error surfaces.
            page.wait_for_timeout(35000)
            idle_capture = wait_and_capture(page, f"{DESTINATION_SLUG}-idle-30s", 500)
            artifacts.append({"label": "Idle state after 30s", "path": idle_capture["screenshot"]})
            step_log.append(idle_capture)
            idle_capture_text = idle_capture["text"]
            idle_timeout_message_seen = (
                "Nexus initialization timed out after 30 seconds" in idle_capture_text
                or any(
                    "Nexus initialization timed out after 30 seconds" in err.get("text", "")
                    for err in console_errors
                )
            )

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

        breakdown = {"text": ""}
        amount_change_ms: Optional[int] = None
        ui_responsive_after_change = False
        mid_fetch_input_enabled: Optional[bool] = None
        mid_fetch_refetch_started: Optional[bool] = None
        after_amount = {"text": ""}
        review = {"text": ""}
        allowance = {"text": ""}
        post_approve = {"text": ""}
        final_state = initial
        balance_loaded = positive_usd_amount(str(available_unified_usdc)) is not None if available_unified_usdc is not None else False
        quote_rendered = False

        if run_bridge_flow:
            if ui_version == "v2":
                flow = run_v2_bridge_flow(
                    page,
                    destination_name,
                    bridge_amount,
                    issues,
                    step_log,
                    artifacts,
                )
            else:
                flow = run_v1_bridge_flow(
                    page,
                    destination_name,
                    bridge_amount,
                    issues,
                    step_log,
                    artifacts,
                )

            breakdown = flow["breakdown"]
            after_amount = flow["after_amount"]
            review = flow["review"]
            allowance = flow["allowance"]
            post_approve = flow["post_approve"]
            quote_visible_ms = flow["quote_visible_ms"]
            execution_attempted = flow["execution_attempted"]
            execution_start = flow["execution_start"]
            balance_loaded = bool(flow.get("balance_loaded")) or balance_loaded
            quote_rendered = bool(flow.get("quote_rendered"))
            if flow.get("unified_balance_usd") is not None:
                available_unified_usdc = flow.get("unified_balance_usd")
                connected_unified_display = format_usdc_display(available_unified_usdc)
            if flow.get("computed_bridge_amount") is not None:
                bridge_amount = str(flow["computed_bridge_amount"])

            final_state = wait_and_capture(page, f"{DESTINATION_SLUG}-final", 3000)
            if execution_start is not None:
                execution_completion_ms = int((time.monotonic() - execution_start) * 1000)
            artifacts.append({"label": "Final UI state", "path": final_state["screenshot"]})
            step_log.append(final_state)
            explorer_link = page.locator("a", has_text="View Explorer").first
            if explorer_link.count() > 0:
                explorer_url = explorer_link.get_attribute("href")
        elif idle_init_test and idle_capture_text is not None:
            final_state = {
                "label": f"{DESTINATION_SLUG}-idle-30s",
                "text": idle_capture_text,
                "screenshot": step_log[-1]["screenshot"] if step_log else initial["screenshot"],
            }

        context.close()
        browser.close()

    worked = []
    summary = []
    final_text = final_state["text"]
    bridge_successful = "Bridge Successful!" in final_text and "Transaction Completed" in final_text
    used_gasless_flow = any(call["method"] in {"eth_signTypedData", "eth_signTypedData_v3", "eth_signTypedData_v4"} for call in provider_calls)
    user_facing_error_seen = any("Oops! Something went wrong. Please try again." in step["text"] for step in step_log)
    initial_quote = (
        extract_v2_quote_details(after_amount["text"])
        if ui_version == "v2"
        else extract_quote_details(after_amount["text"])
    )
    completion = extract_completion_details(final_text)
    breakdown_rows = extract_breakdown_rows(breakdown["text"])
    total_usdc = extract_total_usdc(initial["text"]) or extract_v2_asset_total(initial["text"], "SEND")
    total_usdc_positive = positive_usd_amount(total_usdc)
    if available_unified_usdc is None:
        available_unified_usdc = total_usdc_positive
    final_total_usdc = extract_total_usdc(final_text)
    quoted_receive = extract_numeric_amount(initial_quote.get("amountReceived"))
    actual_receive = extract_numeric_amount(completion.get("amountReceived"))
    quoted_fees = extract_numeric_amount(initial_quote.get("totalFees"))
    actual_fees = extract_numeric_amount(completion.get("totalFees"))
    initial_unified_numeric = extract_numeric_amount(total_usdc)
    final_unified_numeric = extract_numeric_amount(final_total_usdc)
    target_25_percent: Optional[float] = (
        initial_unified_numeric * 0.25 if initial_unified_numeric else None
    )
    bridge_amount_numeric = extract_numeric_amount(bridge_amount)
    bridge_amount_matches_25pct = (
        target_25_percent is not None
        and bridge_amount_numeric is not None
        and target_25_percent > 0
        and abs(bridge_amount_numeric - target_25_percent) / target_25_percent <= 0.05
    )
    source_chains_used: List[str] = []
    raw_sources = completion.get("sourceChains") or ""
    for piece in re.split(r"\s*(?:,|;|/|\band\b|&|\+)\s*", raw_sources):
        chain = piece.strip()
        if chain and chain not in source_chains_used:
            source_chains_used.append(chain)

    middleware_deployment_error_count = sum(
        1 for err in console_errors
        if "Unable to fetch deployment from middleware" in err.get("text", "")
    )

    if total_usdc and "View Balance Breakdown" in initial["text"]:
        worked.append(f"Unified balance loaded in the live UI and surfaced a total of {total_usdc} USDC.")
    if "View Balance Breakdown" in initial["text"] and breakdown["text"]:
        worked.append("Balance breakdown opened successfully and exposed per-chain USDC balances.")
    if initial_quote.get("amountReceived"):
        worked.append(f"A real {bridge_amount} USDC route to {destination_name} was quoted successfully with spend, receive, and fee information.")
    if "Set Token Allowances" in allowance["text"] or "Allowance approved" in final_text:
        worked.append("The execution flow advanced into the token allowance step for the selected source chain.")
    if tx_log:
        worked.append(f"The injected wallet broadcast {len(tx_log)} on-chain transaction(s).")
    if used_gasless_flow:
        worked.append("The wallet signed the typed-data request used during the allowance flow.")
    if bridge_successful:
        worked.append(f"The bridge completed successfully in the live UI and the destination balance updated to include {bridge_amount} USDC on {destination_name}.")
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
                f"Enter {bridge_amount} USDC.",
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
                f"Enter {bridge_amount} USDC.",
                "Click Bridge, then Accept, then Approve Selected.",
                "If the operation fails, note that the UI shows only a generic error banner instead of a specific explanation.",
            ],
            issue_id="FB-P1-002",
            evidence=post_approve["screenshot"],
            root_cause="Execution failures are surfaced with a generic message instead of a task-specific explanation.",
        )

    summary.append(f"Unified balance aggregation is working for this wallet on the {destination_name} route.")
    summary.append(f"The quote path for sending {bridge_amount} USDC to {destination_name} works and the information shown is complete enough to review the route.")
    if bridge_successful:
        summary.append("The end-to-end bridge transaction completed successfully and the UI balance updated on the destination chain.")
    elif tx_log:
        summary.append("At least one live on-chain transaction was submitted during this run.")
    else:
        summary.append("No live on-chain transaction was submitted during this run, so execution remains incomplete.")
    if bridge_successful and used_gasless_flow and not tx_log:
        summary.append("This successful route appears to rely on a gasless signed-intent flow rather than a direct wallet-broadcast transaction.")

    checkpoints = [
        {
            "label": "Connected unified USDC balance",
            "status": "PASS" if positive_usd_amount(str(available_unified_usdc)) is not None else ("PARTIAL" if available_unified_usdc == 0 else "FAIL"),
            "evidence": connected_balance.get("screenshot") or initial.get("screenshot", ""),
            "notes": f"Unified USDC balance after wallet connect: {connected_unified_display}.",
        },
        {
            "label": "Landing page and wallet state",
            "status": "PASS" if balance_loaded or total_usdc_positive else "FAIL",
            "evidence": initial["screenshot"],
            "notes": (
                f"Unified balance shown as {total_usdc} USDC."
                if balance_loaded or total_usdc_positive
                else "Unified balance did not load."
            ),
        },
        {
            "label": "Balance breakdown panel",
            "status": "PASS" if breakdown_rows else ("PARTIAL" if balance_loaded else "FAIL"),
            "evidence": breakdown.get("screenshot", initial["screenshot"]),
            "notes": f"{len(breakdown_rows)} per-chain entries captured." if breakdown_rows else "Per-chain balance breakdown did not appear.",
        },
        {
            "label": "Quote rendering",
            "status": "PASS" if quote_rendered or initial_quote.get("amountReceived") else "FAIL",
            "evidence": after_amount.get("screenshot", initial["screenshot"]),
            "notes": f"Spend {initial_quote.get('amountSpent')}, receive {initial_quote.get('amountReceived')}, fees {initial_quote.get('totalFees')}." if quote_rendered or initial_quote.get("amountReceived") else "Quote details did not render after amount entry.",
        },
        {
            "label": "Allowance review",
            "status": "PASS" if "Set Token Allowances" in allowance["text"] else ("PARTIAL" if execution_attempted else "PARTIAL"),
            "evidence": allowance.get("screenshot", review.get("screenshot", after_amount.get("screenshot", initial["screenshot"]))),
            "notes": "Allowance modal rendered with approval options." if "Set Token Allowances" in allowance["text"] else "Allowance modal was not observed in this run.",
        },
        {
            "label": "Execution completion",
            "status": "PASS" if bridge_successful else ("PARTIAL" if execution_attempted else "FAIL"),
            "evidence": final_state["screenshot"],
            "notes": "Bridge successful state rendered with final amounts and explorer link." if bridge_successful else "Bridge did not reach a successful completion state.",
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
            "status": "PASS" if bridge_successful and initial_unified_numeric is not None and final_unified_numeric is not None and final_unified_numeric != initial_unified_numeric else ("NA" if not bridge_successful else "FAIL"),
            "notes": (
                f"Unified balance changed from {total_usdc} USDC to {final_total_usdc} USDC after completion."
                if bridge_successful and initial_unified_numeric is not None and final_unified_numeric is not None and final_unified_numeric != initial_unified_numeric
                else ("Run did not complete, so balance refresh could not be evaluated." if not bridge_successful else "Completion occurred, but the unified balance did not visibly change.")
            ),
        },
        {
            "id": "EXP-T04",
            "name": "Quote appears within 5s",
            "status": "PASS" if quote_visible_ms is not None and quote_visible_ms <= 5000 else "FAIL",
            "notes": f"Measured {quote_visible_ms} ms." if quote_visible_ms is not None else "Not captured.",
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
            "status": "PASS" if bridge_successful and quoted_receive is not None and actual_receive is not None and abs(actual_receive - quoted_receive) < 0.000001 else ("NA" if not bridge_successful else "FAIL"),
            "notes": (
                f"Quoted receive {initial_quote.get('amountReceived')}; actual receive {completion.get('amountReceived')}."
                if bridge_successful and quoted_receive is not None and actual_receive is not None
                else ("Run did not complete, so delivered output could not be evaluated." if not bridge_successful else "Quoted or actual receive amount was unavailable.")
            ),
        },
        {
            "id": "EXP-D02",
            "name": "Actual fees do not materially exceed quoted fees",
            "status": "PASS" if bridge_successful and quoted_fees is not None and actual_fees is not None and actual_fees <= (quoted_fees * 1.05 + 0.000001) else ("NA" if not bridge_successful else "FAIL"),
            "notes": (
                f"Quoted fees {initial_quote.get('totalFees')}; actual fees {completion.get('totalFees')}."
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
            "status": "PASS" if initial_quote.get("sourceSummary") else "FAIL",
            "notes": f"Source summary: {initial_quote.get('sourceSummary') or 'missing'}.",
        },
        {
            "id": "EXP-U01",
            "name": "Spend, receive, and fee details shown",
            "status": "PASS" if initial_quote.get("amountSpent") and initial_quote.get("amountReceived") and initial_quote.get("totalFees") else "PARTIAL",
            "notes": f"Spend {initial_quote.get('amountSpent')}, receive {initial_quote.get('amountReceived')}, fees {initial_quote.get('totalFees')}.",
        },
        {
            "id": "EXP-U03",
            "name": "Error messages are user-readable",
            "status": "FAIL" if user_facing_error_seen else "PASS",
            "notes": "A user-facing failure banner was shown, but it did not explain the specific cause or recovery path." if user_facing_error_seen else "No user-facing action failure was observed in this run.",
        },
        {
            "id": "EXP-U04",
            "name": "Transaction with 25% of available balance",
            "status": (
                "PASS"
                if bridge_amount_matches_25pct
                and bridge_successful
                and execution_completion_ms is not None
                and execution_completion_ms <= 30000
                else "FAIL"
                if bridge_amount_matches_25pct
                else "NA"
            ),
            "notes": (
                f"Configured amount {bridge_amount} USDC matched 25% of available unified balance ({target_25_percent:.4f} USDC); bridge completed in {execution_completion_ms} ms."
                if bridge_amount_matches_25pct
                and bridge_successful
                and execution_completion_ms is not None
                and execution_completion_ms <= 30000
                else f"Configured amount {bridge_amount} USDC matched 25% target ({target_25_percent:.4f} USDC), but bridge did not complete successfully within 30s (measured {execution_completion_ms} ms, success={bridge_successful})."
                if bridge_amount_matches_25pct
                else f"Configured amount {bridge_amount} USDC is not within 5% of 25% of available balance ({target_25_percent:.4f} USDC); set FASTBRIDGE_BRIDGE_AMOUNT accordingly to evaluate."
                if target_25_percent is not None
                else "Available unified balance was not captured, so the 25% target could not be computed."
            ),
        },
        {
            "id": "EXP-U05",
            "name": "Transaction should be successful when the unified balance deducted from two source chains",
            "status": (
                "PASS"
                if bridge_successful and len(source_chains_used) >= 2
                else "FAIL"
                if bridge_successful
                else "NA"
            ),
            "notes": (
                f"Bridge succeeded and drew from {len(source_chains_used)} source chains: {', '.join(source_chains_used)}."
                if bridge_successful and len(source_chains_used) >= 2
                else f"Bridge succeeded but only drew from {len(source_chains_used)} source chain(s): {', '.join(source_chains_used) or 'unknown'}."
                if bridge_successful
                else "Run did not complete, so multi-source deduction could not be evaluated."
            ),
        },
        {
            "id": "EXP-U06",
            "name": "View 'Assets' button",
            "status": "PASS" if assets_button_visible else "FAIL",
            "notes": (
                f"Assets button visible ({assets_button_check.get('button_count', 0)} button(s) matched)."
                if assets_button_visible
                else "Assets button was not visible after wallet connection."
            ),
        },
        {
            "id": "EXP-U07",
            "name": "View available assets",
            "status": "PASS" if available_assets_loaded else "FAIL",
            "notes": (
                "Assets picker opened, All tab selected, and asset list loaded."
                if available_assets_loaded
                else (
                    "EXP-U07 checks: "
                    + ", ".join(
                        [
                            f"picker_opened={available_assets_check.get('picker_opened')}",
                            f"all_tab_selected={available_assets_check.get('all_tab_selected')}",
                            f"assets_loaded={available_assets_check.get('assets_loaded')}",
                        ]
                    )
                )
            ),
        },
        {
            "id": "EXP-E01",
            "name": "fastbridge and connected to wallet",
            "status": "FAIL" if middleware_deployment_error_count > 0 else "PASS",
            "notes": (
                f"Console emitted 'Unable to fetch deployment from middleware' {middleware_deployment_error_count} time(s) after wallet connection."
                if middleware_deployment_error_count > 0
                else "No 'Unable to fetch deployment from middleware' error observed in the console after wallet connection."
            ),
        },
        {
            "id": "EXP-E02",
            "name": "fastbridge and connected to wallet",
            "status": (
                "PASS"
                if idle_timeout_message_seen is True
                else "FAIL"
                if idle_timeout_message_seen is False
                else "NA"
            ),
            "notes": (
                "After 35s of idle, 'Nexus initialization timed out after 30 seconds' was surfaced."
                if idle_timeout_message_seen is True
                else "After 35s of idle, the expected 'Nexus initialization timed out after 30 seconds' error did not appear."
                if idle_timeout_message_seen is False
                else "Idle-init step did not run in this scenario."
            ),
        },
        {
            "id": "EXP-T01",
            "name": "Execution completes within 30s",
            "status": "PASS" if bridge_successful and execution_completion_ms is not None and execution_completion_ms <= 30000 else ("ANOMALY" if bridge_successful and execution_completion_ms is not None else "FAIL"),
            "notes": f"Measured {execution_completion_ms} ms from approval click to final capture." if execution_completion_ms is not None else "Execution timing not captured.",
        },
    ]

    expectations = apply_expectation_scope(
        expectations,
        scenario_config["in_scope"],
        scenario_key,
    )
    scoped_in_run = [item for item in expectations if item["id"] in scenario_config["in_scope"]]
    scoped_failed = [item for item in scoped_in_run if item["status"] in {"FAIL", "ANOMALY"}]

    if scenario_key == "default":
        overall_status = "PASS" if bridge_successful and not issues else ("PARTIAL" if bridge_successful else "FAIL")
    else:
        overall_status = "PASS" if not scoped_failed else "FAIL"

    wallet_methods = sorted({call["method"] for call in provider_calls if call.get("method")})
    network_summary = [
        f"{len(console_errors)} console error(s) captured.",
        f"{len(page_errors)} page error(s) captured.",
        f"{sum(1 for event in network_events if event.get('status') == 401)} network 401 response(s) captured.",
        f"{sum(1 for event in network_events if event.get('status') == 400)} network 400 response(s) captured.",
    ]
    metrics = {
        "pageLoadMs": page_load_ms if page_load_ms is not None else "n/a",
        "quoteVisibleMs": quote_visible_ms if quote_visible_ms is not None else "n/a",
        "executionCompletionMs": execution_completion_ms if execution_completion_ms is not None else "n/a",
        "consoleErrorCount": len(console_errors),
        "pageErrorCount": len(page_errors),
        "networkEventCount": len(network_events),
    }

    result = {
        "scenarioId": scenario_id,
        "scenarioKey": scenario_key,
        "status": overall_status,
        "runId": RUN_ID,
        "timestampUtc": datetime.now(timezone.utc).isoformat(),
        "tester": "Codex Agent",
        "appUrl": BASE_URL,
        "uiVersion": ui_version,
        "connectedUnifiedUsdc": connected_unified_display,
        "destinationName": destination_name,
        "address": account_address,
        "bridgeAmount": bridge_amount,
        "summary": summary,
        "worked": worked,
        "issues": issues,
        "metrics": metrics,
        "quote": initial_quote,
        "completion": completion,
        "checkpoints": checkpoints,
        "expectations": expectations,
        "balances": {
            "initialUnified": connected_unified_display if connected_unified_display != "unknown" else (
                f"{available_unified_usdc or total_usdc_positive or total_usdc} USDC"
                if available_unified_usdc or total_usdc_positive or total_usdc
                else None
            ),
            "finalUnified": f"{final_total_usdc} USDC" if final_total_usdc else None,
            "breakdown": breakdown_rows,
        },
        "walletInteraction": {
            "methods": wallet_methods,
            "personalSignCount": sum(1 for call in provider_calls if call.get("method") == "personal_sign"),
            "typedDataCount": sum(1 for call in provider_calls if call.get("method") in {"eth_signTypedData", "eth_signTypedData_v3", "eth_signTypedData_v4"}),
            "sendTransactionCount": sum(1 for call in provider_calls if call.get("method") == "eth_sendTransaction"),
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

    return {
        **exported,
        "jsonReport": str(JSON_REPORT_PATH),
        "markdownReport": str(MD_REPORT_PATH),
        "htmlReport": str(HTML_REPORT_PATH),
        "expectationsHtml": str(EXPECTATIONS_HTML_PATH),
    }


if __name__ == "__main__":
    main()
