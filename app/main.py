from base64 import b64encode
from datetime import datetime, timedelta, timezone
import logging as log
from random import randint
from typing import Any, List, Dict, Optional, Tuple

import requests
import toml

import google.oauth2.id_token
from google.cloud import firestore
from google.cloud import secretmanager_v1beta1 as secretmanager
from flask import Flask, redirect, render_template, request, url_for
from google.auth.transport import requests as ga_requests

cfg = toml.load("cfg.toml")

firebase_request_adapter = ga_requests.Request()
db = firestore.Client()
secrets_client = secretmanager.SecretManagerServiceClient()

# If `entrypoint` is not defined in app.yaml, App Engine will look for an app
# called `app` in `main.py`.
app = Flask(__name__)


@app.route("/")
def main():
    id_token = request.cookies.get("token")
    claims, error_message = verify_login(id_token)
    if claims:
        auth_url = get_auth_url(claims["sub"])
    else:
        auth_url = None

    return render_template(
        "index.html", auth_url=auth_url, user_data=claims, error_message=error_message
    )


@app.route("/auth_callback")
def auth_callback():
    id_token = request.cookies.get("token")
    claims, error_message = verify_login(id_token)
    maybe_error = request.args.get("error")
    if maybe_error:
        return f"Error: {maybe_error}"
    if error_message:
        return f"Auth Error: {error_message}"

    code = request.args.get("code")
    client_id, client_secret = get_api_secrets()
    r = requests.post(
        cfg["token_url"],
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg["redirect_uri"],
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if not r.ok:
        log.error("Error Getting token %s: %s", r, r.text)
        return "Error getting token"

    token = r.json()
    store_spotify_token(claims["sub"], token)
    user_profile = get_user_profile(claims["sub"])
    store_spotify_user_profile(claims["sub"], user_profile)
    return redirect(url_for("user_info"))


@app.route("/user_info")
def user_info():
    id_token = request.cookies.get("token")
    claims, error_message = verify_login(id_token)
    if error_message:
        return f"Auth Error: {error_message}"

    playlists = get_user_playlists(claims["sub"])

    return render_template("user_info.html", playlists=playlists, user_data=claims)


@app.route("/save_playlist", methods=["POST"])
def save_playlist():
    id_token = request.cookies.get("token")
    claims, error_message = verify_login(id_token)
    if error_message:
        return f"Auth Error: {error_message}"
    playlist_id = request.form["playlist_id"]
    user = db.collection("spotify_playlist").document(claims["sub"])
    user.set({"id": playlist_id})
    return redirect(url_for("user_info"))


@app.route("/testing")
def testing() -> str:
    """Just a test."""
    return "Hello World!"


def get_user_profile(uid: str) -> Dict[str, Any]:
    tokens = get_spotify_auth(uid)
    r = requests.get(
        cfg["user_url"], headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    if not r.ok:
        log.error("Error getting user profile %s: %s", r, r.text)
    return r.json()


def get_played_songs(uid: str, from_time: int) -> List[Dict]:
    tokens = get_spotify_auth(uid)
    out = []
    before = int(datetime.now(timezone.utc).timestamp() * 1000)  # milliseconds
    query_url = cfg["recently_played_url"]
    while before > from_time:
        songs = requests.get(
            query_url, headers={"Authorization": "Bearer " + tokens["access_token"]}
        ).json()
        out.extend(songs["items"])
        query_url = songs["next"]
        before = int(songs["cursors"]["before"])

    return out


def get_user_playlists(uid: str) -> List[Dict]:
    tokens = get_spotify_auth(uid)
    user = get_spotify_user_profile(uid)
    query_url = cfg["user_playlists_url"].format(user_id=user["id"])
    out = []
    while True:
        r = requests.get(
            query_url, headers={"Authorization": "Bearer " + tokens["access_token"]}
        )
        if not r.ok:
            log.error("Trouble gettings user playlists %s: %s", r, r.text)
        playlists = r.json()
        out.extend([{"id": p["id"], "name": p["name"]} for p in playlists["items"]])
        if not playlists["next"]:
            break
        query_url = playlists["next"]

    return out


def refresh_tokens(uid: str, tokens: dict) -> dict:
    client_id, client_secret = get_api_secrets()
    client_info = f"{client_id}:{client_secret}".encode("utf-8")
    b64client = b64encode(client_info)
    r = requests.post(
        cfg["token_url"],
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        headers={"Authorization": "Basic " + b64client.decode("utf-8")},
    )
    if not r.ok:
        log.error("Error refreshing token %s: %s", r, r.text)
        return "Error getting token"

    new_tokens = r.json()
    store_spotify_token(uid, new_tokens)
    return new_tokens


def verify_login(id_token: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Verify token we got on the request"""
    error_message = None
    claims = None

    if id_token:
        try:
            # Verify the token against the Firebase Auth API. This example
            # verifies the token on each page load. For improved performance,
            # some applications may wish to cache results in an encrypted
            # session store (see for instance
            # http://flask.pocoo.org/docs/1.0/quickstart/#sessions).
            claims = google.oauth2.id_token.verify_firebase_token(
                id_token, firebase_request_adapter
            )
            store_claims(claims)
        except ValueError as exc:
            # This will be raised if the token is expired or any other
            # verification checks fail.
            error_message = str(exc)

    return claims, error_message


def get_auth_url(uid: str) -> str:
    """Get the spotify auth url we need the user to approve"""
    state = randint(10000, 99999)
    store_spotify_auth(uid, {"state": state})
    client_id, client_secret = get_api_secrets()
    auth_url = (
        "{auth_url_base}"
        "client_id={client_id}&"
        "response_type=code&"
        "redirect_uri={redirect_uri}&"
        "state={state}&"
        "scope={scopes_str}"
    ).format(
        **cfg, state=state, client_id=client_id, scopes_str=" ".join(cfg["scopes"])
    )
    return auth_url


def store_spotify_auth(uid: str, spotify_data: dict):
    user = db.collection("spotify").document(uid)
    user.set(spotify_data, merge=True)


def store_spotify_user_profile(uid: str, spotify_data: dict):
    user = db.collection("spotify_profile").document(uid)
    user.set(spotify_data, merge=True)


def store_spotify_token(uid: str, token: dict):
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=token["expires_in"])
    store_spotify_auth(uid, {**token, "expires_at": expires_at})


def get_spotify_auth(uid: str) -> dict:
    user = db.collection("spotify").document(uid)
    tokens = user.get().to_dict()
    # handle token refresh if needed
    if datetime.now(timezone.utc) > tokens["expires_at"]:
        tokens = refresh_tokens(uid, tokens)
    return tokens


def get_spotify_user_profile(uid: str) -> Dict:
    user = db.collection("spotify_profile").document(uid)
    return user.get().to_dict()


def get_spotify_playlist(uid: str) -> Dict:
    playlist = db.collection("spotify_playlist").document(uid)
    return playlist.get().to_dict()


def is_valid_state(uid: str, recieved_state: int):
    return get_spotify_auth(uid)["state"] == recieved_state


def store_claims(claims: dict):
    user = db.collection("claims").document(claims["sub"])
    user.set({**claims, **{"last_used": datetime.now(timezone.utc)}}, merge=True)


def get_api_secrets() -> Tuple[str, str]:
    client_id_name = secrets_client.secret_version_path(
        cfg["project_id"], "spotify_client_id", "latest"
    )
    response = secrets_client.access_secret_version(client_id_name)
    client_id = response.payload.data.decode("UTF-8")

    client_secret_name = secrets_client.secret_version_path(
        cfg["project_id"], "spotify_client_secret", "latest"
    )
    response = secrets_client.access_secret_version(client_secret_name)
    client_secret = response.payload.data.decode("UTF-8")

    return (client_id, client_secret)


if __name__ == "__main__":
    # This is used when running locally only. When deploying to Google App
    # Engine, a webserver process such as Gunicorn will serve the app. This
    # can be configured by adding an `entrypoint` to app.yaml.
    app.run(host="127.0.0.1", port=8080, debug=True)
