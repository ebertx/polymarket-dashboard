#!/usr/bin/env python3
"""
Generate a bcrypt password hash for the dashboard authentication.

Usage:
    python scripts/generate_password_hash.py
    python scripts/generate_password_hash.py --password "your-password"
"""

import argparse
import secrets
import sys

try:
    from passlib.hash import bcrypt
except ImportError:
    print("Error: passlib not installed. Run: pip install passlib[bcrypt]")
    sys.exit(1)


def generate_hash(password: str) -> str:
    """Generate a bcrypt hash of the password."""
    return bcrypt.hash(password)


def generate_jwt_secret() -> str:
    """Generate a secure random JWT secret key."""
    return secrets.token_urlsafe(32)


def main():
    parser = argparse.ArgumentParser(description="Generate password hash for dashboard auth")
    parser.add_argument("--password", "-p", help="Password to hash (will prompt if not provided)")
    args = parser.parse_args()

    password = args.password
    if not password:
        import getpass
        password = getpass.getpass("Enter password: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Error: Passwords do not match")
            sys.exit(1)

    password_hash = generate_hash(password)
    jwt_secret = generate_jwt_secret()

    print("\n" + "=" * 60)
    print("Add these to your .env file:")
    print("=" * 60)
    print(f"\nAUTH_PASSWORD_HASH={password_hash}")
    print(f"JWT_SECRET_KEY={jwt_secret}")
    print("\n" + "=" * 60)


if __name__ == "__main__":
    main()
