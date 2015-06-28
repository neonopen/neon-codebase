'''
Akamai Netstorage API
'''


import os
import os.path
import sys
base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] <> base_path:
    sys.path.insert(0, base_path)

import base64
import json
import hmac
import hashlib
import logging
import random
import time
import tornado.gen
import tornado.httpclient
import urllib
import utils.http
import utils.logs
import utils.neon
import utils.sync

from utils.http import RequestPool

_log = logging.getLogger(__name__)

HTTP_METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE']    

### Helper classes ###

class NoG2OAuth(Exception): pass

class G2OInvalidVersion(object): pass

# This gets raised if you specify both fd and srcfile in chunked_upload().
class MultipleUploadSources(Exception): pass

class G2OAuth(object):
    '''G2OAuth: Object which contains the G2O secret, with methods to generate
    sign strings as used by the Akamai NetStorage HTTP Content Management API'''

    def __init__(self, key, nonce, version=5, client=None, server=None):
        '''__init__(): G2OAuth constructor.  You should create one of these for
        each request, as it fills in the time for you.
        Params:
        key: the value of the G2O secret 
        nonce: the "nonce" (or username) associated with the G2O secret.

        Other fields:
        version: 4 = SHA1, 5 = SHA256.
        client and server are their respective IPs, but currently reserved
        fields, both always "0.0.0.0" 
        time: the Epoch time associated with the request.
        id: a unique id number with some randomness which will guarantee
        uniqueness for the headers we will generate. 
        '''
        self.key = key
        self.nonce = nonce
        self.version = int(version)
        if self.version < 3 or self.version > 5:
            raise G2OInvalidVersion
        # We'll probably use these eventually, currently they must be "0.0.0.0"
        #self.client = client
        #self.server = server
        self.client = "0.0.0.0"
        self.server = "0.0.0.0"
        self.time = str(int(time.time()))
        self.header_id = str(random.getrandbits(32))
        self._auth_data = None

    def get_auth_data(self):
        '''Returns just the value portion of the X-Akamai-G2O-Auth-Data header,
        from the fields of the object.  ''' 
        fmt = "%s, %s, %s, %s, %s, %s"
        if not self._auth_data:
            self._auth_data = fmt % (self.version, self.server, self.client, 
                                     self.time, self.header_id, self.nonce)
        return self._auth_data

    def get_auth_sign(self, uri, action):
        '''use our key to produce a sign string from the value of the
        X-Akamai-G2O-Auth-Data header and the URI of the request.  '''
        lf = '\x0a' # line feed (note, NOT '\x0a\x0d')
        label = 'x-akamai-acs-action:'
        authd = self.get_auth_data()
        sign_string = authd + uri + lf + label + action + lf
        
        # Convert the key to String
        self.key = str(self.key)

        # version is guaranteed to be in (4,5) by the constructor
        # Version 3 is deprecated and will be removed, don't support it.
        if self.version == 3:
            d = hmac.new(self.key)
        if self.version == 4:
            d = hmac.new(self.key, digestmod=hashlib.sha1)
        if self.version == 5:
            d = hmac.new(self.key, digestmod=hashlib.sha256)
        d.update(sign_string)
        return base64.b64encode(d.digest())

    # TODO: Consider removing everything below here for the final version.
    # They're here mainly for testing, though may provide some utility.
    def get_time(self):
        '''Return the string representing the number of seconds since Epoch time
        associated with the request time of the object'''
        return self.time

    def _set_id(self, header_id):
        # Allow the auth data unique ID to be set manually, for testing only
        self.header_id = header_id

    def _set_time(self, time):
        # Allow the request time to be set manually, for testing only
        self.time = str(time)


class AkamaiNetstorage(object):
    
    '''
    ak = AkamaiNetstorage()
    ak.upload(req, body)
    '''

    def __init__(self, host, netstorage_key, netstorage_name, baseurl):
        self.host = host
        self.g2o = None
        self.md5 = None
        self.sha1 = None
        self.sha256 = None
        self.version = 1 # API Version
        self.key = netstorage_key
        self.name = netstorage_name
        self.baseurl = baseurl # The base directory to work in. i.e. '/764573'

    def _get_hashes(self, body):
        '''
        # This is a helper function that returns the size of the specified body, with
        # its cryptographic hashes, as a tuple.
        '''

        m = hashlib.md5(body)
        md5 = m.hexdigest()
        s = hashlib.sha1(body)
        sha1 = s.hexdigest()
        sh = hashlib.sha256(body)
        sha256 = sh.hexdigest()
        return (len(body), md5, sha1, sha256)

    # set the Akamai ACS authentication header values
    def prepare_g2o(self, version=5):
        '''
        prepare_g2o(): set the Akamai ACS authentication header values.
        fields:
        key: a string containing the G2O key (password)
        name: the "nonce" (key name or username associated with the key).
        version: version of G2O auth to use; selects the hashing algorithm.
        
        The version field must be one of (3, 4, 5) and selects the hashing
        algorithm as follows:
          3: md5
          4: sha1
          5: sha256
        '''
        self.g2o = G2OAuth(self.key, self.name, version)

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def delete(self, url):
        '''Delete a relative url from akamai'

        Inputs:
        @url : baseURL or the filename relative to the host

        Return: HTTPResponse object
        '''
        response = yield self._update_action(url, 'delete')
        raise tornado.gen.Return(response)

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def upload(self, url, body, index_zip=None, mtime=None, size=None,
               md5=None, sha1=None, sha256=None):
        '''
        Upload data to Akamai
        @url : baseURL or the filename relative to host
        @body : string of file contents to upload if applicable
        @index_zip: Boolean which sets whether to enable az2z processing to index
                   uploaded .zip archive files for the "serve-from-zip" feature
        @mtime: String of decimal digits representing the Unix Epoch time to
               which the modification time of the file should be set.
        @size: Enforce that the uploaded file has the specified size
        @md5: Endforce that the uploaded file has the specified MD5 sum.
        @sha1: Enforce that the uploaded file has the specified SHA1 hash.
        @sha256: Enforce that the uploaded file has the specified SHA256 hash.
        
        Return: HTTPResponse object
        '''
        if md5 is None:
            m = hashlib.md5(body)
            md5 = m.hexdigest()

        response = yield self._update_action(
            url, 'upload', body, index_zip, mtime, size,
            md5, sha1, sha256)
        raise tornado.gen.Return(response)

    @tornado.gen.coroutine
    def _read_only_action(self, url, action):
        # This internal function implements all of the read-only actions.  They
        # are all essentially identical, aside from the action name itself, and
        # of course the output.  But the output is returned via the same type of
        # object, regardless of its form.  Read-only actions must use the "GET"
        # method, and all such actions require the "format=xml" key-value pair.
        self.prepare_g2o()

        url = self.baseurl + url
        
        fmt = "version=%s"
        if action:
            fmt += "&action=%s"
            if action != 'download':
                fmt += "&format=xml"
            action_string = fmt % (self.version, action)
        else:
           action_string = fmt % (self.version)

        encoded_url = urllib.quote(url)
        g2o_auth_data = self.g2o.get_auth_data()
        g2o_auth_sign = self.g2o.get_auth_sign(encoded_url, action_string)
        headers = {
            'X-Akamai-ACS-Action': action_string,
            'X-Akamai-ACS-Auth-Data': g2o_auth_data,
            'X-Akamai-ACS-Auth-Sign': g2o_auth_sign
        }
        req = tornado.httpclient.HTTPRequest(
            url=self.host + encoded_url,
            method='GET',
            headers=headers,
            request_timeout=10.0,
            connect_timeout=5.0)
        response = yield tornado.gen.Task(utils.http.send_request, req)

        raise tornado.gen.Return(response)

    @tornado.gen.coroutine
    def _update_action(self, url, action, body='', index_zip=None, mtime=None,                       size=None, md5=None, sha1=None, sha256=None, 
                       destination=None, target=None, qd_confirm=None,
                       field=None):
        # This internal function implements all of the update actions.  Each has
        # optional or required arguments; whether or not they are present when
        # required is enforced by the wrapper method interface.  Update-actions
        # require the "POST" or "PUT" method, which we treat equivalently.
        # Unlike read-only actions, "format=xml" is not required or used.

        self.prepare_g2o()
        url = self.baseurl + url

        # Assemble log message
        msg = ""
        if index_zip:
            msg += 'index_zip="%s"' % index_zip
        if mtime:
            msg += 'mtime="%s"' % mtime
        if size:
            msg += 'size="%s"' % size
        if md5:
            msg += 'md5="%s"' % md5
        if sha1:
            msg += 'sha1="%s"' % sha1
        if sha256:
            msg += 'sha256="%s"' % sha256
        if destination:
            msg += 'destination="%s"' % urllib.quote_plus(destination)
        if target:
            msg += 'target="%s"' % urllib.quote_plus(target)
        if qd_confirm:
            msg += 'qd_confirm="%s"' % qd_confirm

        # assemble action string
        fmt = "version=%s"
        if action:
            fmt += "&action=%s"
            if action != 'download':
                fmt += "&format=xml"
                action_string = fmt % (self.version, action)
        else:
            action_string = fmt % (self.version)
        if index_zip:
            action_string += "&index-zip=%s" % index_zip
        if mtime != None:
            action_string += "&mtime=%s" % mtime
        if size:
            action_string += "&size=%s" % size
        if md5:
            action_string += "&md5=%s" % md5
        if sha1:
            action_string += "&sha1=%s" % sha1
        if sha256:
            action_string += "&sha256=%s" % md5
        if destination:
            action_string += "&destination=%s" % urllib.quote_plus(destination)
        if target:
            action_string += "&target=%s" % urllib.quote_plus(target)
        if qd_confirm:
            action_string += "&quick-delete=%s" % qd_confirm

        # Do g2o and send the request
        encoded_url = urllib.quote(url)
        g2o_auth_data = self.g2o.get_auth_data()
        g2o_auth_sign = self.g2o.get_auth_sign(encoded_url, action_string)
        headers = {
            'X-Akamai-ACS-Action': action_string,
            'X-Akamai-ACS-Auth-Data': g2o_auth_data,
            'X-Akamai-ACS-Auth-Sign': g2o_auth_sign,
            'Accept-Encoding': 'identity'
        }

        length = 0
        if (body):
            length = len(body)
        headers['Content-Length'] = length
        req = tornado.httpclient.HTTPRequest(
            url=self.host + encoded_url,
            method="POST",
            body=body,
            headers=headers,
            request_timeout=10.0,
            connect_timeout=5.0)
        response = yield tornado.gen.Task(
            utils.http.send_request, req) 
                        
        raise tornado.gen.Return(response)


if __name__ == "__main__" :
    utils.neon.InitNeon()
    host = "http://fbnneon-nsu.akamaihd.net"
    key = "kx6L370D6gcHP17emUs8f1203io6DhvjDGu88H1KEa9230uwPn"
    name = "fbneon"
    baseURL = "/344611"
    ak = AkamaiNetstorage(host, key, name, baseURL)
    r = ak.upload("/test3", "foo bar tornado2")
    print r
    print ak.delete("/test3")
