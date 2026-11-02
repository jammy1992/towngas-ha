"""Constants for the TownGas Meter integration.

通用版：不再把任何一家分公司的 OAuth clientId 写死在代码里，
改为 「clientId = 前缀 + 分公司 orgCode（去掉下划线）」 的规则 + 添加时下拉选择分公司。
"""

DOMAIN = "hztowngas"

CONF_AUTH_CODE = "auth_code"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_SUBS_ID = "subs_id"
CONF_SUBS_CODE = "subs_code"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_TOKEN_REFRESH_INTERVAL = "token_refresh_interval"
CONF_TOKEN_EXPIRES_IN = "token_expires_in"
CONF_TOKEN_CREATE_TIME = "token_create_time"
CONF_CLIENT_ID = "client_id"
CONF_ORG_CODE = "org_code"
CONF_ORG_NAME = "org_name"
CONF_DEFAULT_PRICE = "default_price"

DEFAULT_SCAN_INTERVAL = 21600          # seconds — data fetch every 6 hours
DEFAULT_TOKEN_REFRESH_INTERVAL = 1800  # seconds — token refresh every 30 min (keepalive)
DEFAULT_PRICE = 3.45                   # 元/m³，接口未返回单价时使用

DEFAULT_HOST = "weixin.towngasvcc.com"
OAUTH_PATH = "/vcc-oauth"
API_PATH = "/nv1/vcc-cbs"

# 微信 OAuth 的 clientId 规律：固定前缀 + 分公司 orgCode（下划线需去掉）
# 例：杭州余杭 YH0105 → pe92a8wechatYH0105
CLIENT_ID_PREFIX = "pe92a8wechat"

# v1.x（clientId 写死在代码里）升级上来、令牌仍然有效的旧条目不会再走 OAuth，
# 因此这里留空即可；只有需要重新扫码时才要求选择分公司。
DEFAULT_CLIENT_ID = ""

SIGN_SALT = "hbasesoft.com-prod"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 NetType/WIFI "
    "MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090c33) XWEB/14315 Flue"
)


def build_client_id(org_code: str) -> str:
    """由分公司 orgCode 推导微信 OAuth clientId。"""
    code = (org_code or "").strip().replace("_", "")
    if not code:
        return ""
    if code.startswith(CLIENT_ID_PREFIX):
        return code  # 用户直接填了完整 clientId
    return f"{CLIENT_ID_PREFIX}{code}"
