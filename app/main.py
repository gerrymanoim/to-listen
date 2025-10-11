from __future__ import annotations

import logging as log
from base64 import b64encode
from datetime import datetime, timedelta, timezone
from random import randint
from typing import Any, Dict, List, Optional, Tuple

import google.oauth2.id_token
import requests
import tomllib
from flask import Flask, redirect, render_template, request, url_for
from google.auth.transport import requests as ga_requests
from google.cloud import firestore
from google.cloud import secretmanager_v1beta1 as secretmanager

secrets_client = secretmanager.SecretManagerServiceClient()
firebase_request_adapter = ga_requests.Request()

with open("cfg.toml", "rb") as f:
    data = tomllib.load(f)

# If `entrypoint` is not defined in app.yaml, App Engine will look for an app
# called `app` in `main.py`.
app = Flask(__name__)


def render_index(*, auth_url=None, user_data=None, error_message=None):
    return render_template(
        "index.html",
        auth_url=auth_url,
        user_data=user_data,
        error_message=error_message,
    )


@app.route("/")
def main():
    try:
        user = User.from_request_token(request.cookies.get("token"))
    except ValueError as e:
        return render_index(error_message=str(e))
    state = randint(10000, 99999)
    user["spotify"] = {"state": state}
    return render_index(
        auth_url=build_auth_url(state), user_data=user["claims"],
    )


@app.route("/auth_callback")
def auth_callback():
    if r_error := request.args.get("error"):
        return f"Error: {r_error}"
    try:
        user = User.from_request_token(request.cookies.get("token"))
    except ValueError as e:
        return f"Auth Error: {e}"

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

    user["spotify"] = r.json()
    spotify = Spotify.from_user(user)
    user["spotify_profile"] = spotify.get_profile()

    return redirect(url_for("user_info"))


@app.route("/user_info")
def user_info():
    try:
        user = User.from_request_token(request.cookies.get("token"))
        spotify = Spotify.from_user(user)
    except ValueError as e:
        return f"Auth Error: {e}"

    playlists = spotify.get_all_playlists()

    return render_template(
        "user_info.html",
        playlists=playlists,
        saved_playlist=user["spotify_playlist"],
        user_data=user["claims"],
    )


@app.route("/save_playlist", methods=["POST"])
def save_playlist():
    try:
        user = User.from_request_token(request.cookies.get("token"))
        spotify = Spotify.from_user(user)
    except ValueError as e:
        return f"Auth Error: {e}"

    playlist_id = request.form["playlist_id"]
    playlist = spotify.get_single_playlist(playlist_id)
    user["spotify_playlist"] = playlist
    return redirect(url_for("user_info"))


# Supporting code


class TokenExpiredException(Exception):
    def __init__(self, message, refresh_token):
        self.message = message
        self.refresh_token = refresh_token


class User:

    valid_keys = ("claims", "spotify", "spotify_playlist", "spotify_profile")

    def __init__(self, uid: str):
        self.uid = uid
        self.client = firestore.Client()

    @classmethod
    def from_request_token(cls, token: str) -> User:
        """
        Verify the token we got on the request. If valid, construct a DB object to
        perform ops. Otherwise raise an error.

        Note this verifies on each page load instead of keeping the token in a Flask
        session.
        """
        try:
            claims = google.oauth2.id_token.verify_firebase_token(
                token, firebase_request_adapter
            )
        except ValueError:
            # This is raised by the auth if token is expired or verification fails
            raise
        c = cls(claims["sub"])
        c["claims"] = claims
        return c

    def _set(self, collection: str, data: dict, merge=True):
        user_document = self.client.collection(collection).document(self.uid)
        user_document.set(data, merge=merge)

    def __setitem__(self, k: str, v: dict):
        if k == "spotify":
            if "expires_in" in v:
                expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=v["expires_in"]
                )
                v = {**v, "expires_at": expires_at}
            self._set(k, v)
        elif k == "spotify_profile":
            self._set(k, v)
        elif k == "claims":
            now = datetime.now(timezone.utc)
            self._set(k, {**v, "last_used": now})
        elif k == "spotify_playlist":
            self._set(k, v, merge=False)
        else:
            raise KeyError(
                "Attempt to set invalid key {k}. Must be one of {self.valid_keys}"
            )

    def _get(self, collection: str) -> Dict[str, Any]:
        user_document = self.client.collection(collection).document(self.uid)
        return user_document.get().to_dict()

    def __getitem__(self, k: str) -> Dict[str, Any]:
        if k == "spotify":
            auth = self._get("spotify")
            if datetime.now(timezone.utc) > auth["expires_at"]:
                raise TokenExpiredException(
                    f'Auth Token expired at {auth["expires_at"]}',
                    auth["refresh_token"],
                )
            return auth
        elif k in self.valid_keys:
            return self._get(k)
        else:
            raise KeyError(
                "Attempt to get invalid key {k}. Must be one of {self.valid_keys}"
            )


class Spotify:
    def __init__(self, uid: str, access_token: str):
        self.uid = uid
        self.access_token = access_token

    @classmethod
    def from_user(cls, user: User) -> Spotify:
        try:
            auth = user["spotify"]
        except TokenExpiredException as exc:
            auth = cls._refresh_tokens(exc.refresh_token)
            user["spotify"] = auth
        return cls(user.uid, auth["access_token"])

    def _get(self, url: str):
        r = requests.get(
            url, headers={"Authorization": "Bearer " + self.access_token}
        )
        if not r.ok:
            log.error(f"Error in GET {url}: {r.text} - {r}")
            # raise?
            return None  # ?
        return r.json()

    def get_profile(self) -> Optional[dict]:
        return self._get(cfg["profile_url"])

    def get_single_playlist(self, playlist_id: str):
        url = cfg["single_playlist_url"].format(playlist_id=playlist_id)
        return self._get(url)

    def get_all_playlists(self) -> List[Dict]:
        user = self.get_profile()
        url = cfg["all_playlists_url"].format(user_id=user["id"])
        out = []
        while True:
            playlists = self._get(url)
            # TODO - handle error
            out.extend(
                [
                    {"id": p["id"], "name": p["name"]}
                    for p in playlists["items"]
                ]
            )
            if not playlists["next"]:
                break
            url = playlists["next"]

        return out

    @staticmethod
    def _refresh_tokens(refresh_token: str) -> Dict[str, str]:
        client_id, client_secret = get_api_secrets()
        client_info = f"{client_id}:{client_secret}".encode("utf-8")
        b64client = b64encode(client_info)
        r = requests.post(
            cfg["token_url"],
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Authorization": "Basic " + b64client.decode("utf-8")},
        )
        if not r.ok:
            log.error("Error refreshing token %s: %s", r, r.text)
            return "Error getting token"

        return r.json()


def build_auth_url(state: int) -> str:
    """Get the spotify auth url we need the user to approve"""
    client_id, _ = get_api_secrets()
    auth_url = (
        "{auth_url_base}"
        "client_id={client_id}&"
        "response_type=code&"
        "redirect_uri={redirect_uri}&"
        "state={state}&"
        "scope={scopes_str}"
    ).format(
        **cfg,
        state=state,
        client_id=client_id,
        scopes_str=" ".join(cfg["scopes"]),
    )
    return auth_url


def get_api_secrets() -> Tuple[str, str]:
    response = secrets_client.access_secret_version(
        request={
            "name": f'projects/{cfg["project_id"]}/secrets/spotify_client_id/versions/latest'
        }
    )
    client_id = response.payload.data.decode("UTF-8")

    response = secrets_client.access_secret_version(
        request={
            "name": f'projects/{cfg["project_id"]}/secrets/spotify_client_secret/versions/latest'
        }
    )
    client_secret = response.payload.data.decode("UTF-8")

    return (client_id, client_secret)


if __name__ == "__main__":
    # This is used when running locally only. When deploying to Google App
    # Engine, a webserver process such as Gunicorn will serve the app. This
    # can be configured by adding an `entrypoint` to app.yaml.
    app.run(host="127.0.0.1", port=8080, debug=True)
