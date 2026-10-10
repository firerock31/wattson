import unittest

from rivian.exceptions import RivianInvalidOTP, RivianUnauthenticated

from auth import authenticate, build_tokens, get_current_user, validate_otp


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


class FakeClient:
    def __init__(self, *, auth_error=None, otp_error=None, payload=None):
        self.auth_error = auth_error
        self.otp_error = otp_error
        self.payload = payload or {}

    async def authenticate(self, email, password):
        if self.auth_error:
            raise self.auth_error

    async def validate_otp(self, email, code):
        if self.otp_error:
            raise self.otp_error

    async def get_user_information(self):
        return FakeResponse(self.payload)


class AuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_error_does_not_expose_request_payload(self):
        secret = "synthetic-secret"
        client = FakeClient(
            auth_error=RivianUnauthenticated(
                200,
                {"errors": [{"message": "unauthenticated"}]},
                {},
                {"variables": {"password": secret}},
            )
        )

        with self.assertRaisesRegex(SystemExit, "Rivian rejected the login") as ctx:
            await authenticate(client, "driver@example.test", secret)

        self.assertNotIn(secret, str(ctx.exception))

    async def test_invalid_otp_does_not_expose_code(self):
        code = "123456"
        client = FakeClient(
            otp_error=RivianInvalidOTP(
                200,
                {"errors": [{"message": "invalid OTP"}]},
                {},
                {"variables": {"otp": code}},
            )
        )

        with self.assertRaisesRegex(SystemExit, "Rivian rejected the OTP") as ctx:
            await validate_otp(client, "driver@example.test", code)

        self.assertNotIn(code, str(ctx.exception))

    async def test_current_user_is_read_from_graphql_response(self):
        current_user = {"vehicles": [{"id": "vehicle-id"}]}
        client = FakeClient(payload={"data": {"currentUser": current_user}})

        self.assertEqual(await get_current_user(client), current_user)


class TokenPayloadTests(unittest.TestCase):
    def test_vehicle_id_and_nested_model_are_persisted(self):
        vehicle = {
            "id": "vehicle-id",
            "vin": "synthetic-vin",
            "name": "Adventure Vehicle",
            "vehicle": {"model": "R2"},
        }

        tokens = build_tokens(
            "driver@example.test",
            "synthetic-session-token",
            vehicle,
        )

        self.assertEqual(tokens["vehicle_id"], "vehicle-id")
        self.assertEqual(tokens["model"], "R2")


if __name__ == "__main__":
    unittest.main()
