"""Constants for the Smart Property Software Bootstrap integration."""

from __future__ import annotations

DOMAIN = "smartproperty_bootstrap"
AGENT_VERSION = "0.1.3"

CONF_CONTROL_PLANE_URL = "control_plane_url"
CONF_PAIR_CODE = "pair_code"
CONF_INSTANCE_NAME = "instance_name"
CONF_INSTANCE_ID = "instance_id"
CONF_TOKEN = "token"
CONF_SIGNING_KEY = "signing_key"
CONF_CLIENT_ID = "client_id"
CONF_POLL_INTERVAL = "poll_interval_seconds"
CONF_SYNC_INTERVAL = "sync_interval_seconds"

DEFAULT_CONTROL_PLANE_URL = "https://connect.smartpropertysoftware.com"
DEFAULT_POLL_INTERVAL = 30
DEFAULT_SYNC_INTERVAL = 60

MIN_POLL_INTERVAL = 10
MAX_POLL_INTERVAL = 600
