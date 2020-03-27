import os
import logging as log
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

import requests
from google.cloud import firestore
from google.cloud import secretmanager_v1beta1 as secretmanager

db = firestore.Client()
secrets_client = secretmanager.SecretManagerServiceClient()

TOKEN_URL = "https://accounts.spotify.com/api/token"
DELETE_URL = "https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
PLAYED_URL = "https://api.spotify.com/v1/me/player/recently-played?limit=50"


def process_listens(event, context):
    """Triggered from a message on a Cloud Pub/Sub topic.
    Args:
         event (dict): Event payload.
         context (google.cloud.functions.Context): Metadata for the event.
    """
    uid = b64decode(event["data"]).decode("utf-8")
    playlist = get_spotify_playlist(uid)

    songs = get_played_songs(uid)
    if not songs:
        log.info("No listens, nothing to do")
        # No listens, nothing to do
        return
    tokens = get_spotify_auth(uid)
    tracks = [{"uri": song["track"]["uri"]} for song in songs]

    r = requests.delete(
        DELETE_URL.format(playlist_id=playlist["id"]),
        json={"tracks": tracks},
        headers={"Authorization": "Bearer " + tokens["access_token"]},
    )
    if not r.ok:
        log.error("Error deleting from playlist %s: %s", r, r.text)
        raise RuntimeError("Error deleting from playlist %s", r)
    store_spotify_playlist(uid, {"last_run": datetime.now(timezone.utc)})


def get_played_songs(uid: str) -> List[Dict]:
    tokens = get_spotify_auth(uid)
    r = requests.get(
        PLAYED_URL, headers={"Authorization": "Bearer " + tokens["access_token"]}
    )
    if not r.ok:
        log.error("Error getting songs %r: %r", r, r.text)
        raise RuntimeError("Error getting Songs", r)

    songs = r.json()
    log.info("Got %s songs", len(songs["items"]))

    return songs["items"]


def refresh_tokens(uid: str, tokens: dict) -> dict:
    client_id, client_secret = get_api_secrets()
    client_info = f"{client_id}:{client_secret}".encode("utf-8")
    b64client = b64encode(client_info)
    r = requests.post(
        TOKEN_URL,
        data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        headers={"Authorization": "Basic " + b64client.decode("utf-8")},
    )
    if not r.ok:
        log.error("Error refreshing token %s: %s", r, r.text)
        return "Error getting token"

    new_tokens = r.json()
    store_spotify_token(uid, new_tokens)
    return new_tokens


def get_spotify_auth(uid: str) -> Dict:
    auth = db.collection("spotify").document(uid)
    tokens = auth.get().to_dict()
    # handle token refresh if needed
    func_run_time = datetime.now(timezone.utc)-timedelta(seconds=10)
    if func_run_time > tokens["expires_at"]:
        tokens = refresh_tokens(uid, tokens)
    return tokens


def get_spotify_user_profile(uid: str) -> Dict:
    user = db.collection("spotify_profile").document(uid)
    return user.get().to_dict()


def get_spotify_playlist(uid: str) -> Dict:
    playlist = db.collection("spotify_playlist").document(uid)
    return playlist.get().to_dict()


def store_spotify_playlist(uid: str, spotify_data: dict):
    playlist = db.collection("spotify_playlist").document(uid)
    playlist.set(spotify_data, merge=True)


def get_api_secrets() -> Tuple[str, str]:
    client_id_name = secrets_client.secret_version_path(
        os.getenv("GCP_PROJECT"), "spotify_client_id", "latest"
    )
    response = secrets_client.access_secret_version(client_id_name)
    client_id = response.payload.data.decode("UTF-8")

    client_secret_name = secrets_client.secret_version_path(
        os.getenv("GCP_PROJECT"), "spotify_client_secret", "latest"
    )
    response = secrets_client.access_secret_version(client_secret_name)
    client_secret = response.payload.data.decode("UTF-8")

    return (client_id, client_secret)


def store_spotify_auth(uid: str, spotify_data: dict):
    user = db.collection("spotify").document(uid)
    user.set(spotify_data, merge=True)


def store_spotify_token(uid: str, token: dict):
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=token["expires_in"])
    store_spotify_auth(uid, {**token, "expires_at": expires_at})
