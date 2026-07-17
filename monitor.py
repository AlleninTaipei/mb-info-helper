#!/usr/bin/env python3
"""Monitor ASUS ROG BIOS and CPU QVL changes."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import smtplib
import ssl
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import certifi


ROUTE_API = "https://api-rog.asus.com/recent-data/api/v3/Route"
SUPPORT_API = "https://rog.asus.com/support/webapi/ProductV2"
USER_AGENT = "asus-motherboard-monitor/1.0"


class MonitorError(RuntimeError):
    pass


def get_json(url: str, params: dict[str, str]) -> dict[str, Any]:
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": USER_AGENT})
    try:
        context = ssl.create_default_context(cafile=certifi.where())
        with urlopen(request, timeout=30, context=context) as response:
            return json.load(response)
    except Exception as exc:
        raise MonitorError(f"API request failed: {url}: {exc}") from exc


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


def fetch_bios(product: dict[str, Any]) -> list[dict[str, Any]]:
    params = support_params(product) | {"cpu": ""}
    payload = get_json(f"{SUPPORT_API}/GetPDBIOS", params)
    if payload.get("Status") != "SUCCESS" or not payload.get("Result"):
        raise MonitorError(f"ASUS BIOS lookup failed: {payload.get('Message', 'unknown error')}")
    groups = payload["Result"].get("Obj") or []
    files = [item for group in groups for item in (group.get("Files") or [])]
    return sorted(
        (
            {
                "version": str(item.get("Version", "")).strip(),
                "release_date": str(item.get("ReleaseDate", "")).strip(),
                "beta": str(item.get("IsRelease", "")) == "0",
                "file_size": str(item.get("FileSize", "")).strip(),
                "description": clean_text(item.get("Description")),
                "sha256": str(item.get("sha256", "")).strip(),
                "download_path": str((item.get("DownloadUrl") or {}).get("Global") or ""),
            }
            for item in files
        ),
        key=lambda item: (item["release_date"], item["version"]),
        reverse=True,
    )


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


def make_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    product = discover_product(config["product_url"])
    return {
        "schema_version": 1,
        "product": {
            "name": product.get("brandingName") or product.get("webPathName"),
            "url": config["product_url"],
            "website": product["websitePath"],
            "model": product["webPathName"],
            "m1_id": product["m1Id"],
            "level_tag_id": product["levelTagId"],
        },
        "bios": fetch_bios(product),
        "cpu_qvl": fetch_cpu_qvl(product),
    }


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
    return {
        "bios": diff_items(old.get("bios", []), new.get("bios", []), "version"),
        "cpu_qvl": diff_items(old.get("cpu_qvl", []), new.get("cpu_qvl", []), "cpu"),
    }


def has_changes(changes: dict[str, Any]) -> bool:
    return any(changes[group][kind] for group in changes for kind in ("added", "removed", "changed"))


def render_summary(snapshot: dict[str, Any], changes: dict[str, Any]) -> str:
    lines = [
        f"ASUS 主機板監控偵測到更新",
        f"型號: {snapshot['product']['name']}",
        f"時間: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    labels = {"added": "新增", "removed": "移除", "changed": "變更"}
    for group, title, key in (("bios", "BIOS", "version"), ("cpu_qvl", "CPU QVL", "cpu")):
        section = changes[group]
        if not any(section.values()):
            continue
        lines.append(f"[{title}]")
        for kind in ("added", "removed"):
            for item in section[kind]:
                detail = item[key]
                if group == "bios":
                    detail += f" ({item['release_date']}, {'Beta' if item['beta'] else '正式版'})"
                else:
                    detail += f" (BIOS: {item['bios_version'] or '未指定'})"
                lines.append(f"- {labels[kind]}: {detail}")
        for item in section["changed"]:
            lines.append(f"- {labels['changed']}: {item['after'][key]}")
            for field in sorted(item["after"]):
                if item["before"].get(field) != item["after"].get(field):
                    lines.append(f"  {field}: {item['before'].get(field, '')} -> {item['after'].get(field, '')}")
        lines.append("")
    lines.append(f"產品頁: {snapshot['product']['url']}")
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
        old = load_json(args.state, default={})
        new = make_snapshot(config)
        if not old.get("schema_version"):
            print(f"Initialized baseline: {len(new['bios'])} BIOS, {len(new['cpu_qvl'])} CPUs")
            if not args.dry_run:
                args.state.parent.mkdir(parents=True, exist_ok=True)
                args.state.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return 0

        changes = compare(old, new)
        if not has_changes(changes):
            print("No changes detected.")
            return 0

        summary = render_summary(new, changes)
        print(summary)
        if not args.dry_run:
            send_email(f"[ASUS 更新] {new['product']['name']}", summary, SmtpConfig.from_env())
            args.state.parent.mkdir(parents=True, exist_ok=True)
            args.state.write_text(json.dumps(new, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0
    except MonitorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
