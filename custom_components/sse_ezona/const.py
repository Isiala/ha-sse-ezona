"""SSE eZóna integration constants."""
from zoneinfo import ZoneInfo

DOMAIN = "sse_ezona"
NAME = "SSE eZóna"
BASE = "https://ezona-zds.sse.sk/sse-gw"
METER_TZ = ZoneInfo("Europe/Bratislava")
CONF_EIC = "eic"
CONF_FETCH_HOUR = "fetch_hour"
CONF_PROXY_URL = "proxy_url"
CONF_PROXY_USERNAME = "proxy_username"
CONF_PROXY_PASSWORD = "proxy_password"
DEFAULT_FETCH_HOUR = 12
SEED_FILE = "sse_ezona_seed.json"
