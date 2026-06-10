#!/usr/bin/env python3
"""Manage the local Outlook mailbox pool.

The pool file format is:
email----password----client_id----refresh_token
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

from outlook_mail import (
    EMAIL_RE,
    OutlookMailClient,
    _mask_email,
    load_outlook_accounts,
    mark_outlook_status,
    reserve_next_outlook,
)


DEFAULT_POOL = "outlook.txt"
DEFAULT_USED = "outlook_used.txt"


def _read_used(path: str) -> Tuple[Dict[str, str], List[Tuple[str, str, str]]]:
    used_path = Path(path)
    if not used_path.is_absolute():
        used_path = Path(__file__).parent / used_path
    latest: Dict[str, str] = {}
    events: List[Tuple[str, str, str]] = []
    if not used_path.exists():
        return latest, events

    for raw in used_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line:
            continue
        match = EMAIL_RE.search(line)
        if not match:
            continue
        email = match.group(0).lower()
        status = line.split()[-1] if line.split() else "used"
        timestamp = line[:19] if len(line) >= 19 else ""
        latest[email] = status
        events.append((timestamp, email, status))
    return latest, events


def _display_email(email: str, show_email: bool) -> str:
    return email if show_email else _mask_email(email)


def _load(pool: str, used: str):
    accounts = load_outlook_accounts(pool)
    latest, events = _read_used(used)
    by_email = {a.email.lower(): a for a in accounts}
    return accounts, by_email, latest, events


def cmd_stats(args) -> int:
    accounts, _, latest, events = _load(args.pool, args.used)
    used_emails = set(latest)
    unused = [a for a in accounts if a.email.lower() not in used_emails]
    statuses = Counter(latest.values())
    domains = Counter(a.email.split("@", 1)[1].lower() for a in accounts)

    print(f"pool: {args.pool}")
    print(f"used: {args.used}")
    print(f"total: {len(accounts)}")
    print(f"unused: {len(unused)}")
    print(f"used: {len(used_emails)}")
    print(f"events: {len(events)}")
    print("statuses:")
    if statuses:
        for status, count in sorted(statuses.items()):
            print(f"  {status}: {count}")
    else:
        print("  none: 0")
    print("domains:")
    for domain, count in sorted(domains.items()):
        print(f"  {domain}: {count}")
    return 0


def cmd_list(args) -> int:
    accounts, _, latest, _ = _load(args.pool, args.used)
    rows = []
    for idx, account in enumerate(accounts, 1):
        status = latest.get(account.email.lower(), "unused")
        if args.status != "all" and status != args.status:
            continue
        rows.append((idx, account.email, status))
        if args.limit and len(rows) >= args.limit:
            break

    for idx, email, status in rows:
        print(f"{idx:04d}  {_display_email(email, args.show_email):36s}  {status}")
    if not rows:
        print("no accounts matched")
    return 0


def cmd_next(args) -> int:
    account = reserve_next_outlook(args.pool, args.used)
    print(_display_email(account.email, args.show_email))
    return 0


def cmd_mark(args) -> int:
    accounts, by_email, _, _ = _load(args.pool, args.used)
    target = args.email.strip().lower()
    if target.isdigit():
        idx = int(target)
        if idx < 1 or idx > len(accounts):
            raise SystemExit(f"index out of range: {idx}")
        email = accounts[idx - 1].email
    else:
        if target not in by_email:
            raise SystemExit(f"email not in pool: {args.email}")
        email = by_email[target].email
    mark_outlook_status(email, args.status, args.used)
    print(f"marked {_display_email(email, args.show_email)} as {args.status}")
    return 0


def cmd_test(args) -> int:
    accounts, by_email, _, _ = _load(args.pool, args.used)
    target = args.email.strip().lower()
    if target.isdigit():
        idx = int(target)
        if idx < 1 or idx > len(accounts):
            raise SystemExit(f"index out of range: {idx}")
        account = accounts[idx - 1]
    elif target:
        if target not in by_email:
            raise SystemExit(f"email not in pool: {args.email}")
        account = by_email[target]
    else:
        account = accounts[0]

    client = OutlookMailClient(
        account,
        verbose=args.verbose,
        proxy=args.proxy or "",
        prefer_imap=not args.graph,
    )
    if args.mode == "token":
        token = client._get_graph_token() if args.graph else client._get_imap_token()
        print("token_ok" if token else "token_missing")
        return 0

    if args.mode == "inbox":
        if args.graph:
            code = client._poll_graph_once(args.filters, set(), 0)
        else:
            code = client._poll_imap_once(args.filters, set(), 0)
        print("inbox_ok_code_found" if code else "inbox_ok_no_code")
        return 0

    code = client.poll_code(
        sender_filters=args.filters,
        timeout=args.timeout,
        interval=args.interval,
    )
    print("poll_code_found" if code else "poll_no_recent_code")
    return 0


def cmd_export(args) -> int:
    accounts, _, latest, _ = _load(args.pool, args.used)
    data = []
    for idx, account in enumerate(accounts, 1):
        data.append({
            "index": idx,
            "email": account.email if args.show_email else _mask_email(account.email),
            "status": latest.get(account.email.lower(), "unused"),
        })
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Outlook mailbox pool")
    parser.add_argument("--pool", default=DEFAULT_POOL, help="Outlook pool file")
    parser.add_argument("--used", default=DEFAULT_USED, help="Used-record file")
    parser.add_argument("--show-email", action="store_true", help="Show full email addresses")

    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("stats", help="Show pool summary").set_defaults(func=cmd_stats)

    p_list = sub.add_parser("list", help="List accounts")
    p_list.add_argument("--status", default="all", help="all, unused, reserved, verified, verify_failed, ...")
    p_list.add_argument("--limit", type=int, default=30)
    p_list.set_defaults(func=cmd_list)

    sub.add_parser("next", help="Reserve and print the next unused account").set_defaults(func=cmd_next)

    p_mark = sub.add_parser("mark", help="Append a status event for an account")
    p_mark.add_argument("email", help="Full email or 1-based pool index")
    p_mark.add_argument("status", help="reserved, verified, verify_failed, bad, ...")
    p_mark.set_defaults(func=cmd_mark)

    p_test = sub.add_parser("test", help="Test token/inbox/recent-code polling")
    p_test.add_argument("email", nargs="?", default="1", help="Full email or 1-based pool index")
    p_test.add_argument("--mode", choices=["token", "inbox", "poll"], default="inbox")
    p_test.add_argument("--graph", action="store_true", help="Use Graph instead of IMAP")
    p_test.add_argument("--proxy", default="", help="Mailbox proxy; empty means direct")
    p_test.add_argument("--timeout", type=int, default=12)
    p_test.add_argument("--interval", type=int, default=3)
    p_test.add_argument("--filters", nargs="+", default=["openai", "noreply", "verification", "no-reply"])
    p_test.add_argument("--verbose", action="store_true")
    p_test.set_defaults(func=cmd_test)

    p_export = sub.add_parser("export", help="Export masked status JSON")
    p_export.set_defaults(func=cmd_export)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
