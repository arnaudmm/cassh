#!/usr/bin/env python

"""
Sign a user's SSH public key.
"""
from __future__ import print_function
from argparse import ArgumentParser
from datetime import datetime, timedelta
from json import dumps
from os import remove, stat
from random import choice
from re import compile as re_compile
from shutil import move
from string import ascii_lowercase
from tempfile import NamedTemporaryFile
from time import time
from urllib import unquote_plus

# Third party library imports
from configparser import ConfigParser, NoOptionError
from ldap import open as ldap_open, SCOPE_SUBTREE
from psycopg2 import connect, OperationalError, ProgrammingError
from requests import Session
from requests.exceptions import ConnectionError
from web import application, config, ctx, data, httpserver
from web.wsgiserver import CherryPyWSGIServer

# Own library
from ssh_utils import Authority, get_fingerprint

# DEBUG
# from pdb import set_trace as st

STATES = {
    0: 'ACTIVE',
    1: 'REVOKED',
    2: 'PENDING',
}

URLS = (
    '/admin/([a-z]+)', 'Admin',
    '/ca', 'Ca',
    '/client', 'Client',
    '/client/status', 'ClientStatus',
    '/cluster/updatekrl', 'ClusterUpdateKRL',
    '/cluster/status', 'ClusterStatus',
    '/health', 'Health',
    '/krl', 'Krl',
    '/ping', 'Ping',
    '/test_auth', 'TestAuth',
)

VERSION = '1.7.0'

REQ_HEADERS = {
    'User-Agent': 'CASSH-SERVER v%s' % VERSION,
    'SERVER_VERSION': VERSION,
}
REQ_TIMEOUT = 2

PARSER = ArgumentParser()
PARSER.add_argument('-c', '--config', action='store', help='Configuration file')
PARSER.add_argument('-v', '--verbose', action='store_true', default=False, help='Add verbosity')
ARGS = PARSER.parse_args()

if not ARGS.config:
    PARSER.error('--config argument is required !')

CONFIG = ConfigParser()
CONFIG.read(ARGS.config)
SERVER_OPTS = {}
SERVER_OPTS['ca'] = CONFIG.get('main', 'ca')
SERVER_OPTS['krl'] = CONFIG.get('main', 'krl')
SERVER_OPTS['port'] = CONFIG.get('main', 'port')

try:
    SERVER_OPTS['admin_db_failover'] = CONFIG.get('main', 'admin_db_failover')
except NoOptionError:
    SERVER_OPTS['admin_db_failover'] = False
SERVER_OPTS['ldap'] = False
SERVER_OPTS['ssl'] = False

if CONFIG.has_section('postgres'):
    try:
        SERVER_OPTS['db_host'] = CONFIG.get('postgres', 'host')
        SERVER_OPTS['db_name'] = CONFIG.get('postgres', 'dbname')
        SERVER_OPTS['db_user'] = CONFIG.get('postgres', 'user')
        SERVER_OPTS['db_password'] = CONFIG.get('postgres', 'password')
    except NoOptionError:
        if ARGS.verbose:
            print('Option reading error (postgres).')
        exit(1)

if CONFIG.has_section('ldap'):
    try:
        SERVER_OPTS['ldap'] = True
        SERVER_OPTS['ldap_host'] = CONFIG.get('ldap', 'host')
        SERVER_OPTS['ldap_bind_dn'] = CONFIG.get('ldap', 'bind_dn')
        SERVER_OPTS['ldap_admin_cn'] = CONFIG.get('ldap', 'admin_cn')
        SERVER_OPTS['filterstr'] = CONFIG.get('ldap', 'filterstr')
    except NoOptionError:
        if ARGS.verbose:
            print('Option reading error (ldap).')
        exit(1)

if CONFIG.has_section('ssl'):
    try:
        SERVER_OPTS['ssl'] = True
        SERVER_OPTS['ssl_private_key'] = CONFIG.get('ssl', 'private_key')
        SERVER_OPTS['ssl_public_key'] = CONFIG.get('ssl', 'public_key')
    except NoOptionError:
        if ARGS.verbose:
            print('Option reading error (ssl).')
        exit(1)

# Cluster mode is used for revocation
try:
    SERVER_OPTS['cluster'] = CONFIG.get('main', 'cluster').split(',')
except NoOptionError:
    # Standalone mode
    PROTO = 'http'
    if SERVER_OPTS['ssl']:
        PROTO = 'https'
    SERVER_OPTS['cluster'] = ['%s://localhost:%s' % (PROTO, SERVER_OPTS['port'])]

try:
    SERVER_OPTS['clustersecret'] = CONFIG.get('main', 'clustersecret')
except NoOptionError:
    # Standalone mode
    SERVER_OPTS['clustersecret'] = randomString(32)


# INDEPENDANT FUNCTIONS
def get(url):
    """
    Rebuilt GET function for CASSH purpose
    """
    session = Session()
    try:
        req = session.get(url,
                          headers=REQ_HEADERS,
                          timeout=REQ_TIMEOUT,
                          stream=True)
    except ConnectionError:
        print('Connection error : %s' % url)
        req = None
    return req

def post(url, payload):
    """
    Rebuilt POST function for CASSH purpose
    """
    session = Session()
    try:
        req = session.post(url,
                           data=payload,
                           headers=REQ_HEADERS,
                           timeout=REQ_TIMEOUT)
    except ConnectionError:
        print('Connection error : %s' % url)
        req = None
    return req

def randomString(stringLength=10):
    """Generate a random string of fixed length """
    letters = ascii_lowercase
    return ''.join(choice(letters) for i in range(stringLength))

def cluster_alived():
    """
    This function returns a subset of pingeable node
    """
    alive_nodes = list()
    dead_nodes = list()
    for node in SERVER_OPTS['cluster']:
        req = get("%s/ping" % node)
        if req is not None and req.text == 'pong':
            alive_nodes.append(node)
        else:
            dead_nodes.append(node)
    return alive_nodes, dead_nodes

def cluster_last_krl():
    """
    This functions download the biggest KRL of the cluster.
    It's not perfect...
    """
    subset, _ = cluster_alived()
    cluster_id = 0
    for node in subset:
        req = get("%s/krl" % node)
        with open('/tmp/%s.krl' % cluster_id, 'wb') as cluster_file:
            for chunk in req.iter_content(chunk_size=1024):
                if chunk: # filter out keep-alive new chunks
                    cluster_file.write(chunk)
        cluster_id += 1

    top_size = stat(SERVER_OPTS['krl']).st_size
    top_cluster_id = 'local'
    for i in range(cluster_id):
        if stat('/tmp/%s.krl' % i).st_size >= top_size:
            top_cluster_id = i

    print('%s KRL is the best' % top_cluster_id)

    if top_cluster_id != 'local':
        move('/tmp/%s.krl' % top_cluster_id, SERVER_OPTS['krl'])
    else:
        cluster_updatekrl(None, update_only=True)

    return True


def cluster_updatekrl(username, update_only=False):
    """
    This function send the revokation of a user to the cluster
    """
    subset, _ = cluster_alived()
    reqs = list()
    payload = {"clustersecret": SERVER_OPTS['clustersecret']}
    if not update_only:
        payload.update({"username": username})
    for node in subset:
        req = post("%s/cluster/updatekrl" % node, payload)
        reqs.append(req)
    return reqs

def data2map():
    """
    Returns a map from data POST
    """
    data_map = {}
    if data() == '':
        return data_map
    for key in data().split('&'):
        data_map[key.split('=')[0]] = '='.join(key.split('=')[1:])
    return data_map

def get_principals(sql_result, username, shell=False):
    """
    Transform sql principals into readable one
    """
    if sql_result is None or sql_result == '':
        if shell:
            return username
        return [username]
    else:
        if shell:
            return sql_result
        return sql_result.split(',')

def unquote_custom(string):
    """
    Returns True is the string is quoted
    """
    if ' ' not in string:
        string = unquote_plus(string)
        if '+' in string and '%20' in string:
            # Old custom quote
            string = string.replace('%20', ' ')
    return string

def pg_connection(
        dbname=SERVER_OPTS['db_name'],
        user=SERVER_OPTS['db_user'],
        host=SERVER_OPTS['db_host'],
        password=SERVER_OPTS['db_password']):
    """
    Return a connection to the db.
    """
    message = ''
    try:
        pg_conn = connect("dbname='%s' user='%s' host='%s' password='%s'"\
            % (dbname, user, host, password))
    except OperationalError:
        return None, 'Error : Server cannot connect to database'
    try:
        pg_conn.cursor().execute("""SELECT * FROM USERS""")
    except ProgrammingError:
        return None, 'Error : Server cannot connect to table in database'
    return pg_conn, message

def pretty_ssh_key_hash(pubkey_fingerprint):
    """
    Returns a pretty json from raw pubkey
    KEY_BITS KEY_HASH [JERK] (AUTH_TYPE)
    """
    try:
        key_bits = int(pubkey_fingerprint.split(' ')[0])
    except ValueError:
        key_bits = 0
    except IndexError:
        key_bits = 0

    try:
        key_hash = pubkey_fingerprint.split(' ')[1]
    except IndexError:
        key_hash = pubkey_fingerprint

    try:
        auth_type = pubkey_fingerprint.split('(')[-1].split(')')[0]
    except IndexError:
        auth_type = 'Unknown'

    rate = 'UNKNOWN'
    if auth_type == 'DSA':
        rate = 'VERY LOW'
    elif (auth_type == 'RSA' and key_bits >= 4096) or (auth_type == 'ECDSA' and key_bits >= 256):
        rate = 'HIGH'
    elif auth_type == 'RSA' and key_bits >= 2048:
        rate = 'MEDIUM'
    elif auth_type == 'RSA' and key_bits < 2048:
        rate = 'LOW'
    elif auth_type == 'ED25519' and key_bits >= 256:
        rate = 'VERY HIGH'

    return {'bits': key_bits, 'hash': key_hash, 'auth_type': auth_type, 'rate': rate}

def str2date(string):
    """
    change xd => seconds
    change xh => seconds
    """
    delta = 0
    if 'd' in string:
        delta = timedelta(days=int(string.split('d')[0])).total_seconds()
    elif 'h' in string:
        delta = timedelta(hours=int(string.split('h')[0])).total_seconds()
    return delta


# OTHER FUNCTION
def ldap_authentification(admin=False):
    """
    Return True if user is well authentified
        realname=xxxxx@domain.fr
        password=xxxxx
    """
    if SERVER_OPTS['ldap']:
        credentials = data2map()
        if credentials.has_key('realname'):
            realname = unquote_plus(credentials['realname'])
        else:
            return False, 'Error: No realname option given.'
        if credentials.has_key('password'):
            password = unquote_plus(credentials['password'])
        else:
            return False, 'Error: No password option given.'
        if password == '':
            return False, 'Error: password is empty.'
        ldap_conn = ldap_open(SERVER_OPTS['ldap_host'])
        try:
            ldap_conn.bind_s(realname, password)
        except Exception as e:
            return False, 'Error: %s' % e
        if admin:
            memberof_admin_list = ldap_conn.search_s(
                SERVER_OPTS['ldap_bind_dn'],
                SCOPE_SUBTREE,
                filterstr='(&(%s=%s)(memberOf=%s))' % (
                    SERVER_OPTS['filterstr'],
                    realname,
                    SERVER_OPTS['ldap_admin_cn']))
            if not memberof_admin_list:
                return False, 'Error: user %s is not an admin.' % realname
    return True, 'OK'

def list_keys(username=None, realname=None):
    """
    Return all keys.
    """
    pg_conn, message = pg_connection()
    if pg_conn is None:
        ctx.status = '503 Service Unavailable'
        return message
    cur = pg_conn.cursor()
    is_list = False

    if realname is not None:
        cur.execute('SELECT * FROM USERS WHERE REALNAME=lower((%s))', (realname,))
        result = cur.fetchone()
    elif username is not None:
        cur.execute('SELECT * FROM USERS WHERE NAME=(%s)', (username,))
        result = cur.fetchone()
    else:
        cur.execute('SELECT * FROM USERS')
        result = cur.fetchall()
        is_list = True
    cur.close()
    pg_conn.close()
    return sql_to_json(result, is_list=is_list)

def sign_key(tmp_pubkey_name, username, expiry, principals, db_cursor=None):
    """
    Sign a key and return cert_contents
    """
    # Load SSH CA
    ca_ssh = Authority(SERVER_OPTS['ca'], SERVER_OPTS['krl'])

    # Sign the key
    try:
        cert_contents = ca_ssh.sign_public_user_key(\
            tmp_pubkey_name, username, expiry, principals)
        if db_cursor is not None:
            db_cursor.execute('UPDATE USERS SET STATE=0, EXPIRATION=(%s) WHERE NAME=(%s)', \
                (time() + str2date(expiry), username))
    except:
        cert_contents = 'Error : signing key'
    return cert_contents

def sql_to_json(result, is_list=False):
    """
    This function prettify a sql result into json
    """
    if result is None:
        return None
    if is_list:
        d_result = {}
        for res in result:
            d_sub_result = {}
            d_sub_result['username'] = res[0]
            d_sub_result['realname'] = res[1]
            d_sub_result['status'] = STATES[res[2]]
            d_sub_result['expiration'] = datetime.fromtimestamp(res[3]).strftime('%Y-%m-%d %H:%M:%S')
            d_sub_result['ssh_key_hash'] = pretty_ssh_key_hash(res[4])
            d_sub_result['expiry'] = res[6]
            d_sub_result['principals'] = get_principals(res[7], res[0])
            d_result[res[0]] = d_sub_result
        return dumps(d_result, indent=4, sort_keys=True)
    d_result = {}
    d_result['username'] = result[0]
    d_result['realname'] = result[1]
    d_result['status'] = STATES[result[2]]
    d_result['expiration'] = datetime.fromtimestamp(result[3]).strftime('%Y-%m-%d %H:%M:%S')
    d_result['ssh_key_hash'] = pretty_ssh_key_hash(result[4])
    d_result['expiry'] = result[6]
    d_result['principals'] = get_principals(result[7], result[0])
    return dumps(d_result, indent=4, sort_keys=True)

class Admin():
    """
    Class admin to action or revoke keys.
    """
    def GET(self, username='DEPRECATED'):
        """
        DEPRECATED Status
        """
        del username
        return "Error: DEPRECATED option. Update your CASSH >= 1.5.0"

    def POST(self, username):
        """
        Revoke or Active keys.
        /admin/<username>
            revoke=true/false => Revoke user
            status=true/false => Display status
        """
        # LDAP authentication
        is_admin_auth, message = ldap_authentification(admin=True)
        if not is_admin_auth:
            ctx.status = '401 Unauthorized'
            return message

        payload = data2map()

        if payload.has_key('revoke'):
            do_revoke = payload['revoke'].lower() == 'true'
        else:
            do_revoke = False
        if payload.has_key('status'):
            do_status = payload['status'].lower() == 'true'
        else:
            do_status = False

        pg_conn, message = pg_connection()
        if pg_conn is None:
            ctx.status = '503 Service Unavailable'
            return message
        cur = pg_conn.cursor()

        if username == 'all' and do_status:
            return list_keys()

        # Search if key already exists
        cur.execute('SELECT * FROM USERS WHERE NAME=(%s)', (username,))
        user = cur.fetchone()
        # If user dont exist
        if user is None:
            cur.close()
            pg_conn.close()
            message = "User '%s' does not exists." % username
        elif do_revoke:
            cur.execute('UPDATE USERS SET STATE=1 WHERE NAME=(%s)', (username,))
            pg_conn.commit()
            message = 'Revoke user=%s.' % username
            cluster_updatekrl(username)
            cur.close()
            pg_conn.close()
        # Display status
        elif do_status:
            return list_keys(username=username)
        # If user is in PENDING state
        elif user[2] == 2:
            cur.execute('UPDATE USERS SET STATE=0 WHERE NAME=(%s)', (username,))
            pg_conn.commit()
            cur.close()
            pg_conn.close()
            message = 'Active user=%s. SSH Key active but need to be signed.' % username
        # If user is in REVOKE state
        elif user[2] == 1:
            cur.execute('UPDATE USERS SET STATE=0 WHERE NAME=(%s)', (username,))
            pg_conn.commit()
            cur.close()
            pg_conn.close()
            message = 'Active user=%s. SSH Key active but need to be signed.' % username
        else:
            cur.close()
            pg_conn.close()
            message = 'user=%s already active. Nothing done.' % username
        return message

    def PATCH(self, username):
        """
        Set the first founded value.
        /admin/<username>
            key=value => Set the key value. Keys are in status output.
        """
        # LDAP authentication
        is_admin_auth, message = ldap_authentification(admin=True)
        if not is_admin_auth:
            ctx.status = '401 Unauthorized'
            return message

        pg_conn, message = pg_connection()
        if pg_conn is None:
            ctx.status = '503 Service Unavailable'
            return message
        cur = pg_conn.cursor()

        payload = data2map()

        for key, value in payload.items():
            if key == 'expiry':
                pattern = re_compile('^\\+([0-9]+)+[dh]$')
                if pattern.match(value) is None:
                    ctx.status = '400 Bad Request'
                    return 'ERROR: Value %s is malformed. Should match pattern ^\\+([0-9]+)+[dh]$' \
                        % value
                cur.execute('UPDATE USERS SET EXPIRY=(%s) WHERE NAME=(%s)', (value, username))
                pg_conn.commit()
                cur.close()
                pg_conn.close()
                return 'OK: %s=%s for %s' % (key, value, username)
            elif key == 'principals':
                value = unquote_plus(value)
                pattern = re_compile("^([a-zA-Z]+)$")
                for principal in value.split(','):
                    if pattern.match(principal) is None:
                        ctx.status = '400 Bad Request'
                        return 'ERROR: Value %s is malformed. Should match pattern ^([a-zA-Z]+)$' \
                            % principal
                cur.execute('UPDATE USERS SET PRINCIPALS=(%s) WHERE NAME=(%s)', (value, username))
                pg_conn.commit()
                cur.close()
                pg_conn.close()
                return 'OK: %s=%s for %s' % (key, value, username)

        return 'WARNING: No key found...'

    def DELETE(self, username):
        """
        Delete keys (but DOESN'T REVOKE)
        /admin/<username>
        """
        # LDAP authentication
        is_admin_auth, message = ldap_authentification(admin=True)
        if not is_admin_auth:
            ctx.status = '401 Unauthorized'
            return message

        pg_conn, message = pg_connection()
        if pg_conn is None:
            ctx.status = '503 Service Unavailable'
            return message
        cur = pg_conn.cursor()

        # Search if key already exists
        cur.execute('DELETE FROM USERS WHERE NAME=(%s)', (username,))
        pg_conn.commit()
        cur.close()
        pg_conn.close()
        return 'OK'


class Ca():
    """
    Class CA.
    """
    def GET(self):
        """
        Return ca.
        """
        return open(SERVER_OPTS['ca'] + '.pub', 'rb')

class ClientStatus():
    """
    ClientStatus main class.
    """
    def POST(self):
        """
        Get client key status.
        /client/status
        """
        # LDAP authentication
        is_auth, message = ldap_authentification()
        if not is_auth:
            ctx.status = '401 Unauthorized'
            return message

        payload = data2map()

        if payload.has_key('realname'):
            realname = unquote_plus(payload['realname'])
        else:
            ctx.status = '400 Bad Request'
            return "Error: No realname option given."

        return list_keys(realname=realname)

class Client():
    """
    Client main class.
    """
    def GET(self, username='DEPRECATED'):
        """
        DEPRECATED Status
        """
        del username
        ctx.status = '299 Deprecated'
        return "Error: DEPRECATED option. Update your CASSH >= 1.5.0"

    def POST(self):
        """
        Ask to sign pub key.
        /client
            username=xxxxxx          => Unique username. Used by default to connect on server.
            realname=xxxxx@domain.fr => This LDAP/AD user.

            # Optionnal
            admin_force=true|false
        """
        # LDAP authentication
        is_auth, message = ldap_authentification()
        if not is_auth:
            ctx.status = '401 Unauthorized'
            return message

        # Check if user is an admin and want to force signature when db fail
        force_sign = False

        # LDAP ADMIN authentication
        is_admin_auth, _ = ldap_authentification(admin=True)

        payload = data2map()

        if is_admin_auth and SERVER_OPTS['admin_db_failover'] \
            and payload.has_key('admin_force') and payload['admin_force'].lower() == 'true':
            force_sign = True

        # Get username
        if payload.has_key('username'):
            username = payload['username']
        else:
            ctx.status = '400 Bad Request'
            return "Error: No username option given. Update your CASSH >= 1.3.0"
        username_pattern = re_compile("^([a-z]+)$")
        if username_pattern.match(username) is None or username == 'all':
            ctx.status = '400 Bad Request'
            return "Error: Username %s doesn't match pattern %s" \
                % (username, username_pattern.pattern)

        # Get realname
        if payload.has_key('realname'):
            realname = unquote_plus(payload['realname'])
        else:
            ctx.status = '400 Bad Request'
            return "Error: No realname option given."

        # Get public key
        if payload.has_key('pubkey'):
            pubkey = unquote_custom(payload['pubkey'])
        else:
            ctx.status = '400 Bad Request'
            return "Error: No pubkey given."
        tmp_pubkey = NamedTemporaryFile(delete=False)
        tmp_pubkey.write(pubkey)
        tmp_pubkey.close()

        pubkey_fingerprint = get_fingerprint(tmp_pubkey.name)
        if pubkey_fingerprint == 'Unknown':
            remove(tmp_pubkey.name)
            ctx.status = '422 Unprocessable Entity'
            return 'Error : Public key unprocessable'

        pg_conn, message = pg_connection()
        # Admin force signature case
        if pg_conn is None and force_sign:
            cert_contents = sign_key(tmp_pubkey.name, username, '+12h', username)
            remove(tmp_pubkey.name)
            return cert_contents
        # Else, if db is down it fails.
        elif pg_conn is None:
            remove(tmp_pubkey.name)
            ctx.status = '503 Service Unavailable'
            return message
        cur = pg_conn.cursor()

        # Search if key already exists
        cur.execute('SELECT * FROM USERS WHERE SSH_KEY=(%s) AND NAME=lower(%s)', (pubkey, username))
        user = cur.fetchone()
        if user is None:
            cur.close()
            pg_conn.close()
            remove(tmp_pubkey.name)
            return 'Error : User or Key absent, add your key again.'

        if username != user[0] or realname != user[1]:
            cur.close()
            pg_conn.close()
            remove(tmp_pubkey.name)
            ctx.status = '401 Unauthorized'
            return 'Error : (username, realname) couple mismatch.'

        status = user[2]
        expiry = user[6]
        principals = get_principals(user[7], username, shell=True)

        if status > 0:
            cur.close()
            pg_conn.close()
            remove(tmp_pubkey.name)
            return "Status: %s" % STATES[user[2]]

        cert_contents = sign_key(tmp_pubkey.name, username, expiry, principals, db_cursor=cur)

        remove(tmp_pubkey.name)
        pg_conn.commit()
        cur.close()
        pg_conn.close()
        return cert_contents

    def PUT(self):
        """
        This function permit to add or update a ssh public key.
        /client
            username=xxxxxx          => Unique username. Used by default to connect on server.
            realname=xxxxx@domain.fr => This LDAP/AD user.
        """
        # LDAP authentication
        is_auth, message = ldap_authentification()
        if not is_auth:
            ctx.status = '401 Unauthorized'
            return message

        payload = data2map()

        if payload.has_key('username'):
            username = payload['username']
        else:
            ctx.status = '400 Bad Request'
            return "Error: No username option given."

        username_pattern = re_compile("^([a-z]+)$")
        if username_pattern.match(username) is None or username == 'all':
            ctx.status = '400 Bad Request'
            return "Error: Username %s doesn't match pattern %s" \
                % (username, username_pattern.pattern)

        if payload.has_key('realname'):
            realname = unquote_plus(payload['realname'])
        else:
            ctx.status = '400 Bad Request'
            return "Error: No realname option given."

        # Get public key
        if payload.has_key('pubkey'):
            pubkey = unquote_custom(payload['pubkey'])
        else:
            ctx.status = '400 Bad Request'
            return 'Error: No pubkey given.'
        tmp_pubkey = NamedTemporaryFile(delete=False)
        tmp_pubkey.write(pubkey)
        tmp_pubkey.close()

        pubkey_fingerprint = get_fingerprint(tmp_pubkey.name)
        if pubkey_fingerprint == 'Unknown':
            remove(tmp_pubkey.name)
            ctx.status = '422 Unprocessable Entity'
            return 'Error : Public key unprocessable'

        pg_conn, message = pg_connection()
        if pg_conn is None:
            remove(tmp_pubkey.name)
            ctx.status = '503 Service Unavailable'
            return message
        cur = pg_conn.cursor()

        # Search if key already exists
        cur.execute('SELECT * FROM USERS WHERE NAME=(%s)', (username,))
        user = cur.fetchone()

        # CREATE NEW USER
        if user is None:
            cur.execute('INSERT INTO USERS VALUES \
                ((%s), (%s), (%s), (%s), (%s), (%s), (%s), (%s))', \
                (username, realname, 2, 0, pubkey_fingerprint, pubkey, '+12h', ''))
            pg_conn.commit()
            cur.close()
            pg_conn.close()
            remove(tmp_pubkey.name)
            ctx.status = '201 Created'
            return 'Create user=%s. Pending request.' % username
        else:
            # Check if realname is the same
            cur.execute('SELECT * FROM USERS WHERE NAME=(%s) AND REALNAME=lower((%s))', \
                (username, realname))
            if cur.fetchone() is None:
                pg_conn.commit()
                cur.close()
                pg_conn.close()
                remove(tmp_pubkey.name)
                ctx.status = '401 Unauthorized'
                return 'Error : (username, realname) couple mismatch.'
            # Update entry into database
            cur.execute('UPDATE USERS SET SSH_KEY=(%s), SSH_KEY_HASH=(%s), STATE=2, EXPIRATION=0 \
                WHERE NAME=(%s)', (pubkey, pubkey_fingerprint, username))
            pg_conn.commit()
            cur.close()
            pg_conn.close()
            remove(tmp_pubkey.name)
            return 'Update user=%s. Pending request.' % username


class ClusterUpdateKRL():
    """
    ClusterUpdateKRL main class.
    """
    def POST(self):
        """
        /cluster/updatekrl
        If a username is present => Revoke this user
        Else                     => Update the KRL
        """
        payload = data2map()

        # Check clustersecret
        if payload.has_key('clustersecret'):
            if payload['clustersecret'] != SERVER_OPTS['clustersecret']:
                ctx.status = '401 Unauthorized'
                return 'Unauthorized'
        else:
            ctx.status = '401 Unauthorized'
            return 'Unauthorized'

        # Get username
        if payload.has_key('username'):
            username = payload['username']
        else:
            # It means I need to update my krl to the latest version
            cluster_last_krl()
            return 'Update Complete'

        pg_conn, message = pg_connection()
        if pg_conn is None:
            ctx.status = '503 Service Unavailable'
            return message
        cur = pg_conn.cursor()

        message = 'Revoke user=%s.' % username
        # Load SSH CA and revoke key
        ca_ssh = Authority(SERVER_OPTS['ca'], SERVER_OPTS['krl'])
        cur.execute('SELECT SSH_KEY FROM USERS WHERE NAME=(%s)', (username,))
        pubkey = cur.fetchone()[0]
        tmp_pubkey = NamedTemporaryFile(delete=False)
        tmp_pubkey.write(pubkey)
        tmp_pubkey.close()
        ca_ssh.update_krl(tmp_pubkey.name)
        cur.close()
        pg_conn.close()
        remove(tmp_pubkey.name)

        return 'Revoke Complete'

class ClusterStatus():
    """
    ClusterStatus main class.
    """
    def GET(self):
        """
        /cluster/status
        """
        message = dict()

        alive_nodes, dead_nodes = cluster_alived()

        for node in alive_nodes:
            message.update({node: {'status': 'OK'}})
        for node in dead_nodes:
            message.update({node: {'status': 'KO'}})

        return dumps(message)


class Health():
    """
    Class Health
    """
    def GET(self):
        """
        Return a health check
        """
        health = {}
        health['name'] = 'cassh'
        health['version'] = VERSION
        return dumps(health, indent=4, sort_keys=True)


class Krl():
    """
    Class KRL.
    """
    def GET(self):
        """
        Return krl.
        """
        return open(SERVER_OPTS['krl'], 'rb')


class Ping():
    """
    Class Ping
    """
    def GET(self):
        """
        Return a pong
        """
        return 'pong'


class TestAuth():
    """
    Test authentication
    """
    def POST(self):
        """
        Test authentication
        """
        # LDAP authentication
        is_auth, message = ldap_authentification()
        if not is_auth:
            ctx.status = '401 Unauthorized'
            return message
        return 'OK'


class MyApplication(application):
    """
    Can change port or other stuff
    """
    def run(self, port=int(SERVER_OPTS['port']), *middleware):
        func = self.wsgifunc(*middleware)
        return httpserver.runsimple(func, ('0.0.0.0', port))

if __name__ == "__main__":
    if SERVER_OPTS['ssl']:
        CherryPyWSGIServer.ssl_certificate = SERVER_OPTS['ssl_public_key']
        CherryPyWSGIServer.ssl_private_key = SERVER_OPTS['ssl_private_key']
    if ARGS.verbose:
        print('SSL: %s' % SERVER_OPTS['ssl'])
        print('LDAP: %s' % SERVER_OPTS['ldap'])
        print('Admin DB Failover: %s' % SERVER_OPTS['admin_db_failover'])
    APP = MyApplication(URLS, globals())
    config.debug = False
    cluster_last_krl()
    APP.run()
