#!/usr/bin/env python3
"""Monitor ASUS ROG BIOS, firmware, Intel ME, and CPU QVL changes."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import certifi


ROUTE_API = "https://api-rog.asus.com/recent-data/api/v3/Route"
SUPPORT_API = "https://rog.asus.com/support/webapi/ProductV2"
USER_AGENT = "asus-motherboard-monitor/1.0"
RELEASE_GROUPS = ("bios", "firmware", "intel_me")
ALL_GROUPS = RELEASE_GROUPS + ("cpu_qvl",)
GROUP_LABELS = {
    "bios": "BIOS",
    "firmware": "PD Firmware / 韌體",
    "intel_me": "Intel ME",
    "cpu_qvl": "CPU QVL",
}


class MonitorError(RuntimeError):
    pass


RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0


def get_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": USER_AGENT})
    context = ssl.create_default_context(cafile=certifi.where())
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urlopen(request, timeout=30, context=context) as response:
                return json.load(response)
        except HTTPError as exc:
            last_exc = exc
            if exc.code not in RETRYABLE_HTTP_STATUSES or attempt == MAX_RETRIES:
                raise MonitorError(f"API request failed: {url}: {exc}") from exc
        except URLError as exc:
            last_exc = exc
            if attempt == MAX_RETRIES:
                raise MonitorError(f"API request failed: {url}: {exc}") from exc
        except Exception as exc:
            raise MonitorError(f"API request failed: {url}: {exc}") from exc
        time.sleep(RETRY_BACKOFF_SECONDS * (2**attempt))
    raise MonitorError(f"API request failed: {url}: {last_exc}") from last_exc


def product_path(product_url: str) -> str:
    path = urlparse(product_url).path.strip("/")
    for suffix in ("helpdesk_bios", "helpdesk_qvl_cpu"):
        if path.endswith(suffix):
            path = path[: -len(suffix)].rstrip("/")
    if not path:
        raise MonitorError(f"Invalid product URL: {product_url}")
    return f"{path}/"


def discover_product(product_url: str) -> dict[str, Any]:
    payload = get_json(ROUTE_API, {"WebURL": product_path(product_url)})
    if payload.get("status") != 200 or not payload.get("result"):
        raise MonitorError(f"ASUS route lookup failed: {payload.get('message', 'unknown error')}")
    return payload["result"]


def support_params(product: dict[str, Any]) -> dict[str, str]:
    return {
        "website": str(product["websitePath"]),
        "model": str(product["webPathName"]),
        "pdid": "0",
        "m1id": str(product["m1Id"]),
        "LevelTagId": str(product["levelTagId"]),
    }


def clean_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip('"')


def release_group_key(name: Any) -> str | None:
    normalized = str(name or "").strip().casefold()
    if normalized == "bios":
        return "bios"
    if normalized in {"韌體", "firmware"}:
        return "firmware"
    if normalized in {"intel me", "me"}:
        return "intel_me"
    return None


def normalize_release(item: dict[str, Any], group: str) -> dict[str, Any]:
    version = str(item.get("Version", "")).strip()
    title = clean_text(item.get("Title"))
    download_path = str((item.get("DownloadUrl") or {}).get("Global") or "")
    identity = version if group == "bios" else f"{title or GROUP_LABELS[group]}|{version}"
    return {
        "key": identity,
        "version": version,
        "title": title,
        "release_date": str(item.get("ReleaseDate", "")).strip(),
        "beta": str(item.get("IsRelease", "")) == "0",
        "file_size": str(item.get("FileSize", "")).strip(),
        "description": clean_text(item.get("Description")),
        "sha256": str(item.get("sha256", "")).strip(),
        "download_path": download_path,
    }


def fetch_releases(product: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    params = support_params(product) | {"cpu": ""}
    payload = get_json(f"{SUPPORT_API}/GetPDBIOS", params)
    if payload.get("Status") != "SUCCESS" or not payload.get("Result"):
        raise MonitorError(f"ASUS BIOS lookup failed: {payload.get('Message', 'unknown error')}")
    groups = payload["Result"].get("Obj") or []
    result: dict[str, list[dict[str, Any]]] = {group: [] for group in RELEASE_GROUPS}
    for api_group in groups:
        group = release_group_key(api_group.get("Name"))
        if group is None:
            continue
        result[group].extend(normalize_release(item, group) for item in (api_group.get("Files") or []))
    for group in RELEASE_GROUPS:
        result[group].sort(
            key=lambda item: (item["release_date"], item["version"], item["title"]), reverse=True
        )
    return result


def fetch_cpu_qvl(product: dict[str, Any]) -> list[dict[str, str]]:
    params = support_params(product) | {"mode": ""}
    payload = get_json(f"{SUPPORT_API}/GetPDCPUList", params)
    if payload.get("Status") != "SUCCESS" or not payload.get("Result"):
        raise MonitorError(f"ASUS CPU QVL lookup failed: {payload.get('Message', 'unknown error')}")
    rows = payload["Result"].get("Obj") or []
    return sorted(
        (
            {
                "cpu": str(item.get("CPU", "")).strip(),
                "pcb_version": str(item.get("PcbVersion", "")).strip(),
                "bios_version": str(item.get("BiosVersion", "")).strip(),
                "memo": clean_text(item.get("Memo")),
            }
            for item in rows
        ),
        key=lambda item: item["cpu"].casefold(),
    )


def configured_products(config: dict[str, Any]) -> list[dict[str, str]]:
    if isinstance(config.get("products"), list) and config["products"]:
        products = config["products"]
    elif config.get("product_url"):
        products = [{"product_url": config["product_url"], "socket": ""}]
    else:
        raise MonitorError("config.json must contain a non-empty products list")
    for entry in products:
        if not isinstance(entry, dict) or not entry.get("product_url"):
            raise MonitorError("Each configured product must contain product_url")
    return products


def make_product_snapshot(entry: dict[str, str]) -> dict[str, Any]:
    product = discover_product(entry["product_url"])
    releases = fetch_releases(product)
    return {
        "product": {
            "name": product.get("brandingName") or product.get("webPathName"),
            "socket": str(entry.get("socket", "")).strip(),
            "url": entry["product_url"],
            "website": product["websitePath"],
            "model": product["webPathName"],
            "m1_id": product["m1Id"],
            "level_tag_id": product["levelTagId"],
        },
        **releases,
        "cpu_qvl": fetch_cpu_qvl(product),
    }


def make_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    products: dict[str, dict[str, Any]] = {}
    for entry in configured_products(config):
        snapshot = make_product_snapshot(entry)
        product_id = str(snapshot["product"]["m1_id"])
        if product_id in products:
            raise MonitorError(f"Duplicate product in config.json: {snapshot['product']['name']}")
        products[product_id] = snapshot
    return {"schema_version": 2, "products": products}


def index_by(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    return {str(item[key]): item for item in items}


def diff_items(old: list[dict[str, Any]], new: list[dict[str, Any]], key: str) -> dict[str, Any]:
    before, after = index_by(old, key), index_by(new, key)
    return {
        "added": [after[item] for item in sorted(after.keys() - before.keys())],
        "removed": [before[item] for item in sorted(before.keys() - after.keys())],
        "changed": [
            {"before": before[item], "after": after[item]}
            for item in sorted(before.keys() & after.keys())
            if before[item] != after[item]
        ],
    }


def compare(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    changes = {
        group: diff_items(old.get(group, []), new.get(group, []), "key")
        for group in RELEASE_GROUPS
    }
    changes["cpu_qvl"] = diff_items(old.get("cpu_qvl", []), new.get("cpu_qvl", []), "cpu")
    return changes


def has_changes(changes: dict[str, Any]) -> bool:
    return any(changes[group][kind] for group in changes for kind in ("added", "removed", "changed"))


def normalize_old_state(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    if state.get("schema_version") == 2 and isinstance(state.get("products"), dict):
        return state
    if state.get("schema_version") != 1 or not state.get("product"):
        return {"schema_version": 2, "products": {}}

    old_product = state["product"]
    socket = ""
    for entry in configured_products(config):
        if product_path(entry["product_url"]) == product_path(old_product.get("url", "")):
            socket = str(entry.get("socket", ""))
            break
    migrated = {
        "product": old_product | {"socket": socket},
        "bios": [item | {"key": item.get("key", item.get("version", "")), "title": item.get("title", "")} for item in state.get("bios", [])],
        "firmware": [],
        "intel_me": [],
        "cpu_qvl": state.get("cpu_qvl", []),
    }
    return {"schema_version": 2, "products": {str(old_product["m1_id"]): migrated}}


def release_detail(item: dict[str, Any], group: str) -> str:
    prefix = f"{item.get('title')} " if item.get("title") else ""
    detail = f"{prefix}{item.get('version', '')}".strip()
    detail += f" ({item.get('release_date', '')}, {'Beta' if item.get('beta') else '正式版'})"
    return detail


def render_summary(snapshot: dict[str, Any], product_changes: dict[str, dict[str, Any]]) -> str:
    lines = [
        "ASUS 主機板監控偵測到更新",
        f"時間: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    labels = {"added": "新增", "removed": "移除", "changed": "變更"}
    for product_id in sorted(product_changes, key=lambda item: snapshot["products"][item]["product"]["name"]):
        product_snapshot = snapshot["products"][product_id]
        product = product_snapshot["product"]
        socket = f"{product.get('socket')} / " if product.get("socket") else ""
        lines.extend([f"=== {socket}{product['name']} ===", ""])
        changes = product_changes[product_id]
        for group in ALL_GROUPS:
            section = changes[group]
            if not any(section.values()):
                continue
            lines.append(f"[{GROUP_LABELS[group]}]")
            for kind in ("added", "removed"):
                for item in section[kind]:
                    if group == "cpu_qvl":
                        detail = f"{item['cpu']} (BIOS: {item['bios_version'] or '未指定'})"
                    else:
                        detail = release_detail(item, group)
                    lines.append(f"- {labels[kind]}: {detail}")
                    if kind == "added" and group in RELEASE_GROUPS and item.get("description"):
                        lines.extend(f"  {line}" for line in item["description"].splitlines())
                    if kind == "added" and group in RELEASE_GROUPS and item.get("download_path"):
                        lines.append(f"  下載: https://dlcdnets.asus.com{item['download_path']}")
            for item in section["changed"]:
                identity = item["after"].get("cpu") or release_detail(item["after"], group)
                lines.append(f"- {labels['changed']}: {identity}")
                for field in sorted(item["after"]):
                    if field == "key":
                        continue
                    if item["before"].get(field) != item["after"].get(field):
                        lines.append(
                            f"  {field}: {item['before'].get(field, '')} -> {item['after'].get(field, '')}"
                        )
            lines.append("")
        lines.append(f"產品頁: {product['url']}")
        lines.append("")
    return "\n".join(lines)


@dataclass
class SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    sender: str
    recipient: str
    starttls: bool

    @classmethod
    def from_env(cls) -> "SmtpConfig":
        required = ["SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD", "MAIL_TO"]
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise MonitorError(f"Missing email environment variables: {', '.join(missing)}")
        return cls(
            host=os.environ["SMTP_HOST"],
            port=int(os.environ.get("SMTP_PORT", "587")),
            username=os.environ["SMTP_USERNAME"],
            password=os.environ["SMTP_PASSWORD"],
            sender=os.environ.get("MAIL_FROM", os.environ["SMTP_USERNAME"]),
            recipient=os.environ["MAIL_TO"],
            starttls=os.environ.get("SMTP_STARTTLS", "true").lower() not in {"0", "false", "no"},
        )


def send_email(subject: str, body: str, smtp_config: SmtpConfig) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = smtp_config.sender
    message["To"] = smtp_config.recipient
    message.set_content(body)
    try:
        if smtp_config.starttls:
            with smtplib.SMTP(smtp_config.host, smtp_config.port, timeout=30) as server:
                server.starttls(context=ssl.create_default_context())
                server.login(smtp_config.username, smtp_config.password)
                server.send_message(message)
        else:
            with smtplib.SMTP_SSL(
                smtp_config.host, smtp_config.port, timeout=30, context=ssl.create_default_context()
            ) as server:
                server.login(smtp_config.username, smtp_config.password)
                server.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise MonitorError(f"Email delivery failed: {exc}") from exc


def load_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        if default is not None:
            return default
        raise MonitorError(f"File not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MonitorError(f"Cannot read JSON file {path}: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--state", type=Path, default=Path("data/state.json"))
    parser.add_argument("--dry-run", action="store_true", help="Do not send email or update state")
    parser.add_argument(
        "--test-email",
        action="store_true",
        help="Send a test email without querying ASUS or updating state",
    )
    args = parser.parse_args()

    try:
        if args.test_email:
            smtp_config = SmtpConfig.from_env()
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            send_email(
                "[ASUS 監控] SMTP 測試成功",
                "ASUS 主機板更新追蹤的 SMTP 設定可正常寄信。\n\n"
                f"測試時間: {now}\n"
                "此測試不會查詢 ASUS API, 也不會修改 state.json。",
                smtp_config,
            )
            print(f"Test email sent to {smtp_config.recipient}.")
            return 0

        config = load_json(args.config)
        raw_old = load_json(args.state, default={})
        old = normalize_old_state(raw_old, config)
        new = make_snapshot(config)

        # Schema migrations and newly added products establish a baseline without
        # reporting every historical release as a new update.
        if raw_old.get("schema_version") != 2:
            totals = {
                group: sum(len(product[group]) for product in new["products"].values())
                for group in ALL_GROUPS
            }
            print(
                "Initialized multi-product baseline: "
                f"{len(new['products'])} products, {totals['bios']} BIOS, "
                f"{totals['firmware']} firmware, {totals['intel_me']} Intel ME, "
                f"{totals['cpu_qvl']} CPUs"
            )
            if not args.dry_run:
                args.state.parent.mkdir(parents=True, exist_ok=True)
                args.state.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 0

        old_ids = set(old["products"])
        new_ids = set(new["products"])
        added_ids = new_ids - old_ids
        removed_ids = old_ids - new_ids
        product_changes = {
            product_id: compare(old["products"][product_id], new["products"][product_id])
            for product_id in old_ids & new_ids
        }
        product_changes = {
            product_id: changes
            for product_id, changes in product_changes.items()
            if has_changes(changes)
        }

        if not product_changes:
            if added_ids or removed_ids:
                print(
                    f"Updated configured-product baseline: +{len(added_ids)} / -{len(removed_ids)} products."
                )
                if not args.dry_run:
                    args.state.parent.mkdir(parents=True, exist_ok=True)
                    args.state.write_text(
                        json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
            else:
                print("No changes detected.")
            return 0

        summary = render_summary(new, product_changes)
        print(summary)
        if not args.dry_run:
            if len(product_changes) == 1:
                only_id = next(iter(product_changes))
                subject_target = new["products"][only_id]["product"]["name"]
            else:
                subject_target = f"{len(product_changes)} 張主機板"
            send_email(f"[ASUS 更新] {subject_target}", summary, SmtpConfig.from_env())
            args.state.parent.mkdir(parents=True, exist_ok=True)
            args.state.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    except MonitorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
