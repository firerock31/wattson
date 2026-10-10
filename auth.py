"""One-time interactive linker: exchange Rivian credentials for session tokens.

Usage:  .venv/bin/python auth.py
Prompts for email/password (and OTP code if Rivian requires it), then stores
ONLY the encrypted session tokens. The password is never written to disk.

Run this with the account owner present - it needs the password and possibly
an OTP code from their email.
"""
import asyncio
import getpass

import aiohttp
from rivian import Rivian
from rivian.exceptions import RivianApiException, RivianInvalidOTP

from config import FLAG_REAUTH, TOKENS_PATH
from crypto import seal, write_private


async def authenticate(client, email: str, password: str) -> None:
    """Authenticate without exposing request details from client exceptions."""
    try:
        await client.authenticate(email, password)
    except RivianApiException:
        raise SystemExit(
            "Rivian rejected the login. Verify the email and password, "
            "then try again."
        ) from None


async def validate_otp(client, email: str, code: str) -> None:
    """Validate an OTP without exposing the code or request payload."""
    try:
        await client.validate_otp(email, code)
    except RivianInvalidOTP:
        raise SystemExit(
            "Rivian rejected the OTP. Request a new code and try again."
        ) from None
    except RivianApiException:
        raise SystemExit(
            "Rivian could not validate the OTP. Try again later."
        ) from None


async def get_current_user(client) -> dict:
    """Parse the current rivian-python-client response shape."""
    response = await client.get_user_information()
    payload = await response.json()
    return payload.get("data", {}).get("currentUser", {})


def build_tokens(email: str, user_session_token: str, vehicle: dict) -> dict:
    """Build the encrypted token payload for the selected vehicle."""
    return {
        "email": email,
        "user_session_token": user_session_token,
        "vehicle_id": vehicle["id"],
        "vin": vehicle["vin"],
        "vehicle_name": vehicle.get("name"),
        "model": (vehicle.get("vehicle") or {}).get("model"),
    }


async def main() -> None:
    email = input("Rivian account email: ").strip()
    password = getpass.getpass("Rivian password (used once, never stored): ")

    # trust_env=True so standard HTTPS_PROXY environment variables are honored.
    async with aiohttp.ClientSession(trust_env=True) as session:
        client = Rivian(session=session)
        await client.create_csrf_token()
        await authenticate(client, email, password)
        # Drop the password from memory as early as possible.
        del password

        if client._otp_needed:
            print("Rivian sent an OTP code to your email.")
            code = input("OTP code: ").strip()
            await validate_otp(client, email, code)
            del code

        info = await get_current_user(client)
        vehicles = info.get("vehicles", [])
        if not vehicles:
            raise SystemExit("No vehicles found on this Rivian account.")

        if len(vehicles) == 1:
            vehicle = vehicles[0]
        else:
            print("Vehicles on this account:")
            for i, v in enumerate(vehicles):
                print(f"  [{i}] {v.get('name')} ({v.get('vin')})")
            vehicle = vehicles[int(input("Which vehicle to track? [0] ") or "0")]

        tokens = build_tokens(email, client._user_session_token, vehicle)

    write_private(TOKENS_PATH, seal(tokens))
    FLAG_REAUTH.unlink(missing_ok=True)   # resume polling after a successful re-link
    print("Vehicle linked. Encrypted tokens saved.")


if __name__ == "__main__":
    asyncio.run(main())
