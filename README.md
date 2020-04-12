# To Listen

A `To Listen` Queue Manager and a GCloud test Project

## What?

![Home Page](/img/screenshot.png?raw=true)

To listen tracks what you're listenign to on spotify and removes things you've listened to from a playlist.

## How?

There are three components:

1. A Flask application running on Google App Engine that manages the user facing website, registration, playlist selection, and authorization callback. 
2. A Cloud Function which runs on a schedule and publishes a PubSub message for every playlist that needs to be  processed. 
3. A Cloud Function, triggered on PubSub messages, which grabs listened songs for a user and removes them from a playlist