import yaml
import os
import logging

# Define all global constants
STATUS_OK = 'ok'
STATUS_ERROR = 'error'

HTTP_OK = 200
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 403
HTTP_NOT_FOUND = 404
HTTP_SERVER_ERROR = 500

# UWS Schema: https://www.ivoa.net/documents/UWS/20161024/REC-UWS-1.1-20161024.html#UWSSchema
VALID_JOB_STATUSES = [
    'pending',
    'queued',
    'executing',
    'completed',
    'error',
    'unknown',
    'held',
    'suspended',
    'aborted',
]

## Load configuration file
with open('/etc/config/server.yaml', "r") as conf_file:
    config = yaml.load(conf_file, Loader=yaml.FullLoader)

## Load secrets from env vars (compatible with Kubernetes Secrets)
config['db']['host'] = os.environ.get('MARIADB_HOST', config['db']['host'])
config['db']['user'] = os.environ.get('MARIADB_USER', config['db']['user'])
config['db']['pass'] = os.environ.get('MARIADB_PASSWORD', config['db']['pass'])
config['db']['database'] = os.environ.get('MARIADB_DATABASE', config['db']['database'])
config['hcaptcha']['secret'] = os.environ.get('HCAPTCHA_SECRET', config['hcaptcha']['secret'])
config['oauth']['userInfoUrl'] = os.environ.get('OAUTH_USERINFO_URL', config['oauth']['userInfoUrl'])
config['oauth']['cookieName'] = os.environ.get('OAUTH_COOKIE_NAME', config['oauth']['cookieName'])

# Configure logging
logging.basicConfig(
    format='%(asctime)s [%(name)-12s] %(levelname)-8s %(message)s',
)
log = logging.getLogger(__name__)
try:
    log.setLevel(config['server']['logLevel'].upper())
except:
    log.setLevel('WARNING')

## Load secret configuration

def import_secret_config(in_conf: dict, secret_conf: dict):
    conf = dict(in_conf)
    for key in secret_conf:
        if key in conf:
            if isinstance(conf[key], dict) and isinstance(secret_conf[key], dict):
                conf[key] = import_secret_config(conf[key], secret_conf[key])
            elif conf[key] == secret_conf[key]:
                pass
            else:
                log.warning(f'''Duplicate key "{key}" with conflicting value found in secret config. Overriding initial value.''')
                conf[key] = secret_conf[key]
        else:
            conf[key] = secret_conf[key]
    return conf

secret_config = {}
if os.path.isfile('/etc/config/secret.yaml'):
    try:
        with open('/etc/config/secret.yaml', "r") as conf_file:
            secret_config = yaml.load(conf_file, Loader=yaml.FullLoader)
    except Exception as e:
        log.error(f'''Error reading secret configuration: {e}''')
try:
    config = import_secret_config(config, secret_config)
    # log.debug(f'''conf: {yaml.dump(config, indent=2)}''')
except Exception as e:
    log.error(f'''Error importing secret configuration: {e}''')

