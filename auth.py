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

from config import FLAG_REAUTH, TOKENS_PATH
from crypto import seal, write_private


async def main() -> None:
    email = input("Rivian account email: ").strip()
    password = getpass.getpass("Rivian password (used once, never stored): ")

    # trust_env=True so the sandbox egress proxy (HTTPS_PROXY) is honored.
    async with aiohttp.ClientSession(trust_env=True) as session:
        client = Rivian(session=session)
        await client.create_csrf_token()
        await client.authenticate(email, password)
        # Drop the password from memory as early as possible.
        del password

        if client._otp_needed:
            print("Rivian sent an OTP code to your email.")
            code = input("OTP code: ").strip()
            await client.validate_otp(email, code)
            del code

        info = await client.get_user_information()
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

        tokens = {
            "email": email,
            "user_session_token": client._user_session_token,
            "vin": vehicle["vin"],
            "vehicle_name": vehicle.get("name"),
            "model": vehicle.get("model"),
        }

    write_private(TOKENS_PATH, seal(tokens))
    FLAG_REAUTH.unlink(missing_ok=True)   # resume polling after a successful re-link
    print(f"Linked {tokens['vehicle_name']} ({tokens['vin']}). Tokens saved.")


if __name__ == "__main__":
    asyncio.run(main())
