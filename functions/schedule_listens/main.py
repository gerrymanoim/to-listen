import os

from google.cloud import firestore
from google.cloud import pubsub_v1 as pubsub

db = firestore.Client()

publisher = pubsub.PublisherClient()
topic_path = publisher.topic_path(os.getenv("GCP_PROJECT"), "process_listens_for_email")


def schedule_listens(event, context):
    """Triggered from a message on a Cloud Pub/Sub topic.
    Args:
         event (dict): Event payload.
         context (google.cloud.functions.Context): Metadata for the event.
    """
    users = db.collection("spotify_playlist").stream()
    for user in users:
        publisher.publish(topic_path, user.id.encode())
