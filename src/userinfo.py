import logging
import re
import requests

from global_vars import config

logger = logging.getLogger('pkg.auth.oauth2')
logger.setLevel('DEBUG')


SSL_VERIFY = True
OAUTH_USERINFO_URL = config['oauth']['userInfoUrl']
OAUTH_COOKIE_NAME = config['oauth']['cookieName']


def userinfo(access_token) -> dict:
    try:
        resp = requests.get(url=OAUTH_USERINFO_URL,
                            verify=SSL_VERIFY,
                            cookies={"_oauth2_proxy": access_token})
        resp.raise_for_status()
        user = resp.json()

        roles = []
        for grp in user['groups']:
            roles.append(grp)

        # sub = user['preferredUsername'].replace('@', '').replace('.', '')
        email = user['email']
        username = re.sub(r'[^a-zA-Z0-9]', '', email)

        # TODO: Hoping that oauth2-proxy enhances support for providing arbitrary token claims from OIDC
        # See https://github.com/oauth2-proxy/oauth2-proxy/issues/834
        return {
            'email': user['email'],
            'groups': roles,
            # 'family_name': user['family_name'],
            # 'given_name': user['given_name'],
            'sub': username
        }
    except Exception as e:
        logger.debug(f'OAuth2 token verification failed for token={str(access_token)}')
        logger.warning(f'OAuth2 token verification failed: {str(e)}')
        pass


def get_token_from_request_cookies(request):
    token = request.cookies[OAUTH_COOKIE_NAME].value if OAUTH_COOKIE_NAME in request.cookies else None
    logger.debug('Found get_token: ' + str(token))
    return token

# TODO: required scopes currently ignored
def validate_auth_cookie(request, required_scopes=[]):
    token = get_token_from_request_cookies(request)
    logger.debug('Found validate_token: ' + str(token))
    return userinfo(token)
