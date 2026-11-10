"""Constants for the BTicino C100X Scrypted plugin.

Upstream Door Entry / Classe 100X app constants (subscription key, client id,
certificate secret) are intentionally kept to match the official mobile app
protocol. Owner decision: do not rotate or remove them.
"""

from __future__ import annotations

DOMAIN = "bticino_c100x"

CONF_HOME_ID = "home_id"
CONF_GATEWAY_ID = "gateway_id"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_ENABLE_TALKBACK = "enable_talkback"

API_BASE = "https://api.developer.legrand.com"
API_SUBSCRIPTION_KEY = "f36968e522bf4ec3877fa491109d3d14"
CERTIFICATE_TENANT_ID = "199686b5-bef4-4960-8786-7a6b1888fee3"
CERTIFICATE_CLIENT_ID = "c7d272d9-e76a-41b7-824f-a988ad964cf8"
CERTIFICATE_CLIENT_SECRET = "6COxJs+avzPS1/kwr0+DWnNqmej4cw8GArR5AGJtgMc="

B2C_BASE = "https://eliotclouduamprd.b2clogin.com"
B2C_TENANT = "EliotClouduamprd.onmicrosoft.com"
B2C_POLICY = "B2C_1_DoorEliot-C100X-SignUporSignIn"
B2C_CLIENT_ID = "7d11af71-ab98-4832-aa62-6b00bff3bcc8"
B2C_REDIRECT_URI = "com.legrandgroup.c100x://oauth2redirect"
B2C_SCOPE = "openid offline_access https://EliotClouduamprd.onmicrosoft.com/security/access.full"
B2C_USER_AGENT = "NetatmoApp(DoorEntry/v1.8.4) Android(13/Google/sdk_gphone64_arm64)"

SIP_SERVER = "vdesip.bs.iotleg.com"
SIP_PORT = 5228
SIP_REGISTER_EXPIRES = 600
# Refresh below the Azure LB idle timeout (~4 min); Expires stays 600.
SIP_REREGISTER_SECONDS = 180
SIP_REGISTER_TIMEOUT_SECONDS = 15
SIP_RECONNECT_SECONDS = 10
# RFC 5626 CRLF keepalive: ping every 30 s to keep the LB flow alive.
# Legrand/Azure does not reliably answer with a CRLF pong; missing pongs
# must not trigger a reconnect (REGISTER refresh is the health check).
SIP_KEEPALIVE_SECONDS = 30
SIP_KEEPALIVE_TIMEOUT_SECONDS = 10
SIP_KEEPALIVE_PING = b"\r\n\r\n"
SIP_KEEPALIVE_PONG = b"\r\n"
# Short reconnect backoff after refresh/socket failure (seconds).
SIP_RECONNECT_BACKOFF = (1, 2, 5, 30)
SIP_USER_AGENT = "scrypted-bticino-c100x/0.1"
SIP_CLIENT_NAME = "Scrypted"

# Certificate profile used by the official Classe 100X application.
CERTIFICATE_TEMPLATE = "sipuser"
CERTIFICATE_ORGANIZATIONAL_UNIT = "C100X"
CERTIFICATE_RENEWAL_DAYS = 30

RING_ACTIVE_SECONDS = 5
STORAGE_VERSION = 1

STUN_SERVER = ("stun.linphone.org", 3478)
STUN_COOKIE = 0x2112A442
STUN_BINDING_REQUEST = 0x0001
STUN_BINDING_SUCCESS = 0x0101
STUN_XOR_MAPPED_ADDRESS = 0x0020

MEDIA_IDLE_SECONDS = 30
RING_DECLINE_DELAY_SECONDS = 2
