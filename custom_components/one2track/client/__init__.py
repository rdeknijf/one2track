from aiohttp import ClientSession, CookieJar

from .gps_client import GpsClient
from .client_types import One2TrackConfig, AuthenticationError, TrackerDevice


def get_client(config: One2TrackConfig, session: ClientSession | None = None) -> GpsClient:
    # Use a dedicated session with its own cookie jar to avoid conflicts
    # with HA's shared session cookie management
    if session is None:
        session = ClientSession(cookie_jar=CookieJar(unsafe=True))
    return GpsClient(config, session)
