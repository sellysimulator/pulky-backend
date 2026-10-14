"""Firebase verification and the auth dependencies (spec 13.2, 13.4)."""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.core import firebase
from tests.conftest import FirebaseTokens
from tests.test_skeleton.helpers import patch_settings

SIGN_IN = "Sign in to continue."
UNAVAILABLE = "Sign-in is not available right now."


@pytest.fixture
def auth_client() -> TestClient:
    probe = FastAPI()

    @probe.get("/me")
    async def me(user: dict = Depends(deps.get_current_firebase_user)) -> dict:
        return user

    @probe.get("/maybe")
    async def maybe(
        user: dict | None = Depends(deps.get_optional_firebase_user),
    ) -> Any:
        return {"user": user}

    return TestClient(probe)


def test_unconfigured_firebase_verifies_nothing() -> None:
    """Failure mode 2 / AC8: never a claims dict while unconfigured."""
    assert firebase.init_firebase() is None
    assert deps.verify_firebase_id_token("anything") is None
    assert deps.verify_firebase_id_token("") is None


def test_init_firebase_unparseable_json_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_settings(monkeypatch, FIREBASE_SERVICE_ACCOUNT_JSON="{not json")
    assert firebase.init_firebase() is None
    assert deps.verify_firebase_id_token("anything") is None


@pytest.fixture
def fake_service_account(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    import firebase_admin
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    account = {
        "type": "service_account",
        "project_id": "pulky-test",
        "private_key_id": "abc",
        "private_key": pem,
        "client_email": "test@pulky-test.iam.gserviceaccount.com",
        "client_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    patch_settings(monkeypatch, FIREBASE_SERVICE_ACCOUNT_JSON=json.dumps(account))
    yield
    try:
        firebase_admin.delete_app(firebase_admin.get_app())
    except ValueError:
        pass


def test_init_firebase_with_a_valid_account_returns_the_app_once(
    fake_service_account: None,
) -> None:
    first = firebase.init_firebase()
    assert first is not None
    assert firebase.init_firebase() is first


def test_configured_verification_returns_claims_and_rejects_failures(
    monkeypatch: pytest.MonkeyPatch, fake_service_account: None
) -> None:
    from firebase_admin import auth

    def fake_verify(token: str, *args: Any, **kwargs: Any) -> dict:
        if token == "good":
            return {"uid": "u1"}
        raise ValueError("expired")

    monkeypatch.setattr(auth, "verify_id_token", fake_verify)
    assert deps.verify_firebase_id_token("good") == {"uid": "u1"}
    assert deps.verify_firebase_id_token("bad") is None


def test_firebase_tokens_fixture_verifies_exactly_its_keys(
    firebase_tokens: FirebaseTokens,
) -> None:
    firebase_tokens["t1"] = {"uid": "u1"}
    assert deps.verify_firebase_id_token("t1") == {"uid": "u1"}
    assert deps.verify_firebase_id_token("t2") is None
    assert firebase.init_firebase() is not None


def test_current_user_401_without_header(
    auth_client: TestClient, firebase_tokens: FirebaseTokens
) -> None:
    response = auth_client.get("/me")
    assert response.status_code == 401
    assert response.json() == {"detail": SIGN_IN}


@pytest.mark.parametrize(
    "header", ["Basic abc", "Bearer", "Bearer ", "token-without-scheme", "Bearer a b"]
)
def test_current_user_401_for_a_non_bearer_header(
    auth_client: TestClient, firebase_tokens: FirebaseTokens, header: str
) -> None:
    firebase_tokens["a"] = {"uid": "u"}
    response = auth_client.get("/me", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json() == {"detail": SIGN_IN}


def test_current_user_401_for_a_bad_token(
    auth_client: TestClient, firebase_tokens: FirebaseTokens
) -> None:
    firebase_tokens["good"] = {"uid": "u1"}
    response = auth_client.get("/me", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json() == {"detail": SIGN_IN}


def test_current_user_returns_the_verified_claims(
    auth_client: TestClient, firebase_tokens: FirebaseTokens
) -> None:
    firebase_tokens["good"] = {"uid": "u1", "email": "a@example.com"}
    response = auth_client.get("/me", headers={"Authorization": "Bearer good"})
    assert response.status_code == 200
    assert response.json() == {"uid": "u1", "email": "a@example.com"}


def test_current_user_503_when_firebase_is_unconfigured(
    auth_client: TestClient,
) -> None:
    response = auth_client.get("/me", headers={"Authorization": "Bearer anything"})
    assert response.status_code == 503
    assert response.json() == {"detail": UNAVAILABLE}


def test_optional_user_is_none_without_a_header(
    auth_client: TestClient, firebase_tokens: FirebaseTokens
) -> None:
    response = auth_client.get("/maybe")
    assert response.status_code == 200
    assert response.json() == {"user": None}


def test_optional_user_is_none_without_a_header_when_unconfigured(
    auth_client: TestClient,
) -> None:
    assert auth_client.get("/maybe").json() == {"user": None}


def test_optional_user_401_for_an_invalid_token(
    auth_client: TestClient, firebase_tokens: FirebaseTokens
) -> None:
    response = auth_client.get("/maybe", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json() == {"detail": SIGN_IN}


def test_optional_user_returns_claims_for_a_valid_token(
    auth_client: TestClient, firebase_tokens: FirebaseTokens
) -> None:
    firebase_tokens["good"] = {"uid": "u1"}
    response = auth_client.get("/maybe", headers={"Authorization": "Bearer good"})
    assert response.json() == {"user": {"uid": "u1"}}
