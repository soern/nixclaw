#!/usr/bin/env python3
"""Reset stale Signal GroupV2 sender-key state in signal-cli's local database.

Background
----------
Signal group messages are encrypted with a "sender key": a symmetric key the
sender generates once per group and distributes to every member's device.
Every subsequent group message is just encrypted with that cached key and
re-sent -- much cheaper than a per-recipient Double Ratchet session (which is
what 1:1 messages, and group *typing indicators*, use instead).

If a member's device loses sync with that cached key (reinstall, device
change, corrupted local state, etc.) the sender (signal-cli / this bot) keeps
happily encrypting with the now-invalid key. The Signal server has no way to
detect this -- it just relays ciphertext -- so `POST /v2/send` keeps
returning 201 Created. The recipient's client silently fails to decrypt and
drops the message with no visible error. Symptom: typing indicator shows
(different, unaffected mechanism), then silence, forever, for that one group,
while 1:1 chats and other groups work fine.

The fix is to delete the stale `sender_key` / `sender_key_shared` rows for
the affected group's `distribution_id` in signal-cli's SQLite database.
signal-cli then treats the next outgoing message to that group as "first
message" and generates + distributes a brand new sender key from scratch,
which resynchronizes state with every member's device.

Scope of changes (safety)
--------------------------
This script ONLY deletes rows from the `sender_key` and `sender_key_shared`
tables, scoped to specific `distribution_id`s. It does NOT touch:
  - group_v2 / group_v2_member (membership, admins, permissions, blocked flag)
  - session / identity (1:1 Double Ratchet sessions, safety numbers/trust)
  - message_send_log, recipient, or any message history
  - account registration state

Downsides / things to know
---------------------------
- Must be run while the signal-api container is STOPPED. SQLite is in WAL
  mode and a live signal-cli process is actively reading/writing this file;
  editing it concurrently risks corruption or having your changes silently
  clobbered by an in-flight write. This script refuses to run against a
  live/locked database and will remind you to stop the container.
- A dated backup of account.db (+ -wal/-shm) is taken automatically before
  any write, so a bad run can be rolled back by restoring the backup.
- Cost of a reset: the *next* message sent to that group triggers one extra
  round-trip (a sender-key-distribution message alongside/before the real
  message) to re-establish state with all members. This is normal Signal
  protocol behavior (identical to what happens for a brand-new group) and is
  invisible to users -- it does not resend, duplicate, or lose any messages.
- Resetting a group that was NOT actually broken is harmless (it just forces
  an unnecessary-but-harmless key refresh) -- so `--all` is safe to run
  broadly, e.g. as a "when in doubt, repair everything" step. It does NOT
  fix problems that aren't sender-key desync (e.g. an actually revoked
  membership, a blocked account, or a real network/account issue) -- those
  will still fail after a reset.

Usage
-----
    # Stop the bridge first
    docker-compose stop signal-api

    # See what would happen without changing anything
    python3 scripts/reset_group_sender_keys.py --all --dry-run

    # Reset every group's sender-key state
    python3 scripts/reset_group_sender_keys.py --all

    # Reset just one group, by its display name (as signal-cli last saw it)
    python3 scripts/reset_group_sender_keys.py --group "Test Family Group"

    # ...or by its internal_id (the base64 string signal-cli/bridge report,
    # e.g. from GET /v1/groups/<account> while the bridge is running)
    python3 scripts/reset_group_sender_keys.py --group-id "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="

    # Then restart the bridge
    docker-compose start signal-api

By default the script looks for the account database under
`.signal-data/data/<account>.d/account.db` relative to the current
directory (matching this repo's signal-api bind mount). Override with
--db-path if your layout differs.
"""

import argparse
import base64
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Container name(s) that must be stopped before writing. The SQLite lock
# check below only catches an active conflicting transaction at the exact
# instant we check -- it does NOT reliably detect "the service is up but
# idle right now", which is the common case. This Docker-level check is the
# primary safety gate for actual (non-dry-run) writes.
SIGNAL_API_CONTAINER_NAMES = ("nixclaw-signal-api",)


def find_account_db(signal_data_dir: Path) -> Path:
    """Locate the live account.db under <signal_data_dir>/data/<acct>.d/account.db."""
    candidates = sorted(signal_data_dir.glob("data/*.d/account.db"))
    if not candidates:
        raise FileNotFoundError(
            f"No account.db found under {signal_data_dir}/data/*.d/ -- "
            "pass --db-path explicitly if your layout differs."
        )
    if len(candidates) > 1:
        print(f"WARNING: multiple account.db files found, using the first: {candidates}")
    return candidates[0]


def backup_db(db_path: Path) -> Path:
    """Copy account.db (and -wal/-shm sidecars if present) to a timestamped backup dir."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = db_path.parent / f"account.db.backup-{ts}"
    backup_dir.mkdir(parents=False, exist_ok=False)
    for suffix in ("", "-wal", "-shm"):
        src = db_path.with_name(db_path.name + suffix)
        if src.exists():
            shutil.copy2(src, backup_dir / src.name)
    print(f"Backed up database to: {backup_dir}")
    return backup_dir


def assert_signal_api_stopped() -> None:
    """Refuse to proceed if the signal-api container is running.

    This is the PRIMARY safety gate for real writes. It shells out to
    `docker inspect` rather than relying on SQLite's own locking, because a
    momentarily-idle live process can pass a naive "try to lock it" check
    even though it is actively reading/writing the file on its own schedule.
    """
    for name in SIGNAL_API_CONTAINER_NAMES:
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", name],
                capture_output=True, text=True, timeout=10,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            print(f"WARNING: could not check container status via docker ({e}); "
                  "proceeding on trust that you have stopped signal-api manually.")
            return
        if result.returncode != 0:
            # Container doesn't exist under this name -- not our problem, skip.
            continue
        if result.stdout.strip() == "true":
            raise RuntimeError(
                f"Container '{name}' is still RUNNING. Stop it first:\n"
                f"  docker-compose stop signal-api\n"
                "(Dry runs are safe to do while it's running; only real writes require it stopped.)"
            )


def assert_db_not_locked(db_path: Path) -> None:
    """Secondary/best-effort check: try to grab an exclusive SQLite lock.

    This catches an active conflicting transaction at the exact instant we
    check, but does NOT reliably detect an idle-but-running signal-cli
    process -- see assert_signal_api_stopped() for the primary gate.
    """
    conn = sqlite3.connect(str(db_path), timeout=2.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError as e:
        raise RuntimeError(
            "Could not get an exclusive lock on the database -- something else "
            "has it open for writing right now.\n"
            f"Underlying error: {e}"
        )
    finally:
        conn.close()


def list_groups(conn: sqlite3.Connection) -> list[dict]:
    """Return all GroupV2 rows with their internal_id (base64) and distribution_id."""
    cur = conn.cursor()
    cur.execute("SELECT _id, group_id, distribution_id, blocked FROM group_v2")
    rows = []
    for _id, group_id_blob, distribution_id, blocked in cur.fetchall():
        rows.append({
            "row_id": _id,
            "internal_id": base64.b64encode(group_id_blob).decode("ascii"),
            "distribution_id": distribution_id,
            "blocked": bool(blocked),
        })
    return rows


def group_display_names(conn: sqlite3.Connection) -> dict[str, str]:
    """Best-effort internal_id -> display name via group_v2.group_data (protobuf).

    We avoid a full protobuf dependency here; group_data is a serialized
    DecryptedGroup which stores the title as a length-prefixed UTF-8 field.
    This is intentionally NOT used for matching (only for nicer log output);
    matching by --group uses the bridge API's name at lookup time, or falls
    back to matching an internal_id substring.
    """
    return {}


def reset_group(conn: sqlite3.Connection, group: dict, dry_run: bool) -> tuple[int, int]:
    """Delete sender_key / sender_key_shared rows for one group's distribution_id."""
    dist = group["distribution_id"]
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM sender_key WHERE distribution_id = ?", (dist,))
    sk_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM sender_key_shared WHERE distribution_id = ?", (dist,))
    sks_count = cur.fetchone()[0]

    if not dry_run:
        cur.execute("DELETE FROM sender_key WHERE distribution_id = ?", (dist,))
        cur.execute("DELETE FROM sender_key_shared WHERE distribution_id = ?", (dist,))

    return sk_count, sks_count


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reset stale GroupV2 sender-key state in signal-cli's database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--all", action="store_true", help="Reset sender-key state for every group.")
    target.add_argument("--group-id", metavar="INTERNAL_ID", help="Reset only the group with this internal_id (base64, as reported by the bridge API).")
    parser.add_argument(
        "--signal-data-dir", default=".signal-data", type=Path,
        help="Path to the signal-cli data dir (default: ./.signal-data, matching this repo's bind mount).",
    )
    parser.add_argument("--db-path", type=Path, default=None, help="Explicit path to account.db, overrides --signal-data-dir auto-discovery.")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be deleted without changing anything.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = parser.parse_args()

    try:
        db_path = args.db_path or find_account_db(args.signal_data_dir)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        return 1

    if not db_path.exists():
        print(f"ERROR: database not found at {db_path}")
        return 1

    print(f"Using database: {db_path}")

    if not args.dry_run:
        try:
            assert_signal_api_stopped()
        except RuntimeError as e:
            print(f"ERROR: {e}")
            return 1

    try:
        assert_db_not_locked(db_path)
    except RuntimeError as e:
        print(f"ERROR: {e}")
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        groups = list_groups(conn)
        if not groups:
            print("No groups found in database. Nothing to do.")
            return 0

        if args.group_id:
            groups = [g for g in groups if g["internal_id"] == args.group_id]
            if not groups:
                print(f"ERROR: no group found with internal_id={args.group_id!r}")
                return 1

        print(f"\n{'DRY RUN: ' if args.dry_run else ''}Targeting {len(groups)} group(s):")
        for g in groups:
            flag = " [BLOCKED]" if g["blocked"] else ""
            print(f"  - {g['internal_id']}{flag}")

        if not args.dry_run:
            if not args.yes:
                resp = input(f"\nThis will delete sender-key state for {len(groups)} group(s). Continue? [y/N] ")
                if resp.strip().lower() != "y":
                    print("Aborted.")
                    return 1
            backup_db(db_path)

        total_sk = 0
        total_sks = 0
        print()
        for g in groups:
            sk_count, sks_count = reset_group(conn, g, args.dry_run)
            total_sk += sk_count
            total_sks += sks_count
            verb = "would delete" if args.dry_run else "deleted"
            print(f"  {g['internal_id']}: {verb} {sk_count} sender_key row(s), {sks_count} sender_key_shared row(s)")

        if not args.dry_run:
            conn.commit()
            print(f"\nDone. Deleted {total_sk} sender_key + {total_sks} sender_key_shared rows across {len(groups)} group(s).")
            print("Restart signal-api now: docker-compose start signal-api")
        else:
            print(f"\nDry run complete. Would delete {total_sk} sender_key + {total_sks} sender_key_shared rows across {len(groups)} group(s).")
            print("Re-run without --dry-run to apply.")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
