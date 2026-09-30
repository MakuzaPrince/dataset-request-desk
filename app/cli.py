"""Management commands.

    python -m app.cli seed-users [--file seed/users.json]
    python -m app.cli import-episodes seed/episodes.csv
"""
import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import Role, User
from app.security import hash_password
from app.services.importer import import_episodes


def seed_users(db: Session, users: list[dict]) -> tuple[int, int]:
    """Create missing users. Existing accounts are left alone so a re-seed never resets passwords."""
    created = existing = 0
    for u in users:
        email = u["email"].strip().lower()
        if db.scalars(select(User).where(User.email == email)).first():
            existing += 1
            continue
        db.add(User(
            email=email,
            name=u["name"],
            organisation=u.get("organisation"),
            role=Role(u["role"]),
            password_hash=hash_password(u["password"]),
            is_active=True,
        ))
        created += 1
    db.commit()
    return created, existing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed-users", help="create the seed accounts")
    seed.add_argument("--file", default="seed/users.json", type=Path)
    imp = sub.add_parser("import-episodes", help="import an episodes CSV export (safe to re-run)")
    imp.add_argument("path", type=Path)
    imp.add_argument("--summary", action="store_true", help="print counts only, not every skipped row")
    args = parser.parse_args(argv)

    with SessionLocal() as db:
        if args.command == "seed-users":
            created, existing = seed_users(db, json.loads(args.file.read_text(encoding="utf-8")))
            print(json.dumps({"created": created, "already_existed": existing}))
        else:
            with args.path.open(encoding="utf-8-sig", newline="") as fh:
                report = import_episodes(db, fh, filename=args.path.name)
            if args.summary:
                report = {k: v for k, v in report.items() if k not in ("skipped_rows", "warnings")}
            print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
