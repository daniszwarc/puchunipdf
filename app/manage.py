import getpass
import os
import sys

import psycopg

import auth


def ask_password() -> str:
    pw = getpass.getpass("Password: ")
    if len(pw) < 12:
        sys.exit("Password must be at least 12 characters")
    if getpass.getpass("Repeat password: ") != pw:
        sys.exit("Passwords do not match")
    return pw


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("create-user", "set-password"):
        sys.exit("usage: python manage.py create-user|set-password <username>")
    cmd, user = sys.argv[1], sys.argv[2].strip()
    if not user or len(user) > 150:
        sys.exit("Invalid username")
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        conn.execute(auth.USERS_DDL)
        exists = conn.execute("SELECT 1 FROM users WHERE username = %s", (user,)).fetchone()
        if cmd == "create-user" and exists:
            sys.exit("User already exists")
        if cmd == "set-password" and not exists:
            sys.exit("No such user")
        hashed = auth.hasher.hash(ask_password())
        if cmd == "create-user":
            conn.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", (user, hashed))
        else:
            conn.execute("UPDATE users SET password_hash = %s WHERE username = %s", (hashed, user))
    print(f"{cmd} ok: {user}")


if __name__ == "__main__":
    main()
