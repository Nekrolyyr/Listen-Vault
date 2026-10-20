"""Operator commands, run inside the api container (see scripts/).

    python -m listen.cli make-admin <email>         create or promote an administrator (asks for the password)
    python -m listen.cli set-quota <email> <pages>  change a user's page quota
    python -m listen.cli dedup-existing [--apply]   merge stored PDFs that have the same bytes (dry run by default)
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import logging
import sys

import psycopg
from psycopg.rows import dict_row
from pypdf import PdfReader

from . import accounts, config, db
from .storage import DocPaths


def _password(args) -> str:
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password (at least 12 characters): ")
        if getpass.getpass("Repeat the password: ") != password:
            raise SystemExit("The passwords do not match")
    try:
        accounts.check_password_rules(password)
    except accounts.AccountError as e:
        raise SystemExit(str(e)) from e
    return password


def _connect(settings: config.Settings) -> psycopg.Connection:
    return psycopg.connect(settings.database_url, row_factory=dict_row)


def make_admin(settings: config.Settings, args) -> None:
    email = accounts.normalise_email(args.email)
    password = _password(args)
    with _connect(settings) as conn, conn.transaction():
        user = accounts.claim_legacy(conn, email, password)
        if user is None:
            user = accounts.find_user(conn, email)
            if user is None:
                user = accounts.create_user(conn, email, password, verified=True)
            else:
                accounts.set_password(conn, user["id"], password)
                accounts.mark_verified(conn, user["id"])
        conn.execute("UPDATE users SET is_admin = true, is_disabled = false WHERE id = %s", (user["id"],))
        accounts.audit(conn, None, "cli.make-admin", user["id"], {"email": email})
    print(f"{email} is an administrator")


def set_quota(settings: config.Settings, args) -> None:
    if args.pages < 0:
        raise SystemExit("The quota cannot be negative")
    with _connect(settings) as conn, conn.transaction():
        user = accounts.find_user(conn, accounts.normalise_email(args.email))
        if user is None:
            raise SystemExit("No such account")
        conn.execute("UPDATE users SET page_quota = %s WHERE id = %s", (args.pages, user["id"]))
        accounts.audit(conn, None, "cli.set-quota", user["id"], {"from": user["page_quota"], "to": args.pages})
    print(f"{args.email}: quota {args.pages} pages")


def dedup_existing(settings: config.Settings, args) -> None:
    """Hash the PDFs stored before accounts existed. Papers with the same bytes are merged: their library entries
    and playback move to the oldest copy, and the other copies are removed (their files after the commit).
    A dry run changes nothing."""
    conn = _connect(settings)
    try:
        rows = conn.execute("SELECT id, pages FROM documents WHERE content_sha256 IS NULL "
                            "AND source_type = 'pdf' ORDER BY created_at, id").fetchall()
        groups: dict[str, list[dict]] = {}
        for row in rows:
            pdf = DocPaths(settings.docs_dir, row["id"]).source_pdf
            if pdf.exists():
                groups.setdefault(hashlib.sha256(pdf.read_bytes()).hexdigest(), []).append(row)
        merged = 0
        for digest, (keeper, *others) in groups.items():
            if not others:
                conn.execute("UPDATE documents SET content_sha256 = %s WHERE id = %s", (digest, keeper["id"]))
                continue
            print(f"{digest[:12]}  keep {keeper['id']}  merge {[str(o['id']) for o in others]}")
            for other in others:
                conn.execute(
                    "INSERT INTO library_entries (user_id, document_id, added_at, listened, last_listened_at) "
                    "SELECT user_id, %s, added_at, listened, last_listened_at FROM library_entries "
                    "WHERE document_id = %s ON CONFLICT DO NOTHING", (keeper["id"], other["id"]))
                conn.execute(
                    "UPDATE playback SET document_id = %s WHERE document_id = %s AND NOT EXISTS "
                    "(SELECT 1 FROM playback p WHERE p.user_id = playback.user_id AND p.document_id = %s)",
                    (keeper["id"], other["id"], keeper["id"]))
                conn.execute("DELETE FROM documents WHERE id = %s", (other["id"],))
                merged += 1
            pages = keeper["pages"]
            if pages is None:
                try:
                    pages = len(PdfReader(str(DocPaths(settings.docs_dir, keeper["id"]).source_pdf)).pages)
                except Exception:  # noqa: BLE001 - an unreadable file leaves its page count empty
                    pages = None
            conn.execute("UPDATE documents SET content_sha256 = %s, pages = %s WHERE id = %s",
                         (digest, pages, keeper["id"]))
        print(f"{len(groups)} distinct PDF(s), {merged} duplicate document(s) to merge")
        if not args.apply:
            conn.rollback()
            print("dry run: nothing changed (use --apply)")
            return
        conn.commit()
    finally:
        conn.close()
    for members in groups.values():
        for other in members[1:]:
            DocPaths(settings.docs_dir, other["id"]).remove()
    print("applied")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m listen.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("make-admin", help="create or promote an administrator")
    admin.add_argument("email")
    admin.add_argument("--password-stdin", action="store_true", help="read the password from stdin (scripts)")
    quota = sub.add_parser("set-quota", help="change a user's page quota")
    quota.add_argument("email")
    quota.add_argument("pages", type=int)
    dedup = sub.add_parser("dedup-existing", help="merge stored PDFs with the same bytes")
    dedup.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    settings = config.load()
    db.wait_for_db(settings.database_url)
    db.migrate(settings.database_url)
    if args.command == "make-admin":
        make_admin(settings, args)
    elif args.command == "set-quota":
        set_quota(settings, args)
    elif args.command == "dedup-existing":
        dedup_existing(settings, args)


if __name__ == "__main__":
    main()
