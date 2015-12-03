#!/usr/bin/env python
'''
Data Model classes 

Defines interfaces for Neon User Account, Platform accounts
Account Types
- NeonUser
- BrightcovePlatform
- YoutubePlatform

Api Request Types
- Neon, Brightcove, youtube

This module can also be called as a script, in which case you get an
interactive console to talk to the database with.

'''
import os
import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

from api import ooyala_api
import base64
import binascii
import cmsdb.cdnhosting
import code
import collections
import concurrent.futures
import contextlib
import copy
import cv.imhash_index
import datetime
import errno
import hashlib
import itertools
import simplejson as json
import logging
import multiprocessing
from passlib.hash import sha256_crypt
from PIL import Image
import random
import re
import redis as blockingRedis
import redis.exceptions
import socket
import string
from StringIO import StringIO
import tornado.ioloop
import tornado.gen
import tornado.httpclient
import threading
import time
import api.brightcove_api #coz of cyclic import 
import api.youtube_api
import utils.botoutils
import utils.logs
from utils.imageutils import PILImageUtils
import utils.neon
from utils.options import define, options
from utils import statemon
import utils.sync
import utils.s3
import utils.http 
import urllib
import urlparse
import warnings
import uuid


_log = logging.getLogger(__name__)

define("thumbnailBucket", default="host-thumbnails", type=str,
        help="S3 bucket to Host thumbnails ")

define("accountDB", default="0.0.0.0", type=str, help="")
define("videoDB", default="0.0.0.0", type=str, help="")
define("thumbnailDB", default="0.0.0.0", type=str ,help="")
define("dbPort", default=6379, type=int, help="redis port")
define("watchdogInterval", default=3, type=int, 
        help="interval for watchdog thread")
define("maxRedisRetries", default=5, type=int,
       help="Maximum number of retries when sending a request to redis")
define("baseRedisRetryWait", default=0.1, type=float,
       help="On the first retry of a redis command, how long to wait in seconds")
define("video_server", default="127.0.0.1", type=str, help="Neon video server")
define('async_pool_size', type=int, default=10,
       help='Number of processes that can talk simultaneously to the db')

## Parameters for thumbnail perceptual hashing
define("hash_type", default="dhash", type=str,
       help="Type of perceptual hash function to use. ahash, phash or dhash")
define("hash_size", default=64, type=int,
       help="Size of the perceptual hash in bits")

# Other parameters
define('send_callbacks', default=1, help='If 1, callbacks are sent')

define('isp_host', default='isp-usw-388475351.us-west-2.elb.amazonaws.com',
       help=('Host address to get to the ISP that is checked for if images '
             'are there'))

statemon.define('subscription_errors', int)
statemon.define('pubsub_errors', int)
statemon.define('sucessful_callbacks', int)
statemon.define('callback_error', int)
statemon.define('invalid_callback_url', int)

#constants 
BCOVE_STILL_WIDTH = 480

class ThumbDownloadError(IOError):pass
class DBStateError(ValueError):pass
class DBConnectionError(IOError):pass

def _get_db_address(class_name, is_writeable=True):
    '''Function that returns the address to the database for an object.

    Inputs:
    class_name - Class name of the object to lookup
    is_writeable - If true, the connection can write to the database.
                   Otherwise it's read only

    Returns: (host, port)
    '''
    #TODO: Add the functionlity to talk to read only database slaves

    # This function can get called a lot, so all the options lookups
    # are done without introspection.
    host = options.get('cmsdb.neondata.accountDB')
    port = options.get('cmsdb.neondata.dbPort')
    if class_name:
        if class_name == "VideoMetadata":
            host = options.get('cmsdb.neondata.videoDB')
        elif class_name in ["ThumbnailMetadata", "ThumbnailURLMapper",
                            "ThumbnailServingURLs"]:
            host = options.get('cmsdb.neondata.thumbnailDB')
    return (host, port)

def _object_to_classname(otype=None):
    '''Returns the class name of an object.

    otype can be a class object, an instance object or the class name
    as a string.
    '''
    cname = None
    if otype is not None:
        if isinstance(otype, basestring):
            cname = otype
        else:
            #handle the case for classmethod
            cname = otype.__class__.__name__ \
              if otype.__class__.__name__ != "type" else otype.__name__
    return cname
    

class DBConnection(object):
    '''Connection to the database.

    There is one connection for each object type, so to get the
    connection, please use the get() function and don't create it
    directly.
    '''

    #Note: Lock for each instance, currently locks for any instance creation
    __singleton_lock = threading.Lock() 
    _singleton_instance = {} 

    def __init__(self, class_name):
        '''Init function.

        DO NOT CALL THIS DIRECTLY. Use the get() function instead
        '''
        self.conn = RedisAsyncWrapper(class_name, socket_timeout=10)
        self.blocking_conn = RedisRetryWrapper(class_name, socket_timeout=10)

    def __del__(self):
        self.close()

    def close(self):
        self.conn.close()
        self.blocking_conn.close()

    def fetch_keys_from_db(self, pattern='*', keys_per_call=1000,
                           set_name=None, callback=None):
        '''Gets a list of keys that match a pattern.

        Uses SCAN to do it and not block the database

        Inputs:
        pattern - wildcard pattern of key to look for
        keys_per_call - Max number of keys to return per scan call
        set_name - If fetching from a set, what is that set's name
        callback - Optional callback to get an asynchronous call
        '''

        conn = self.conn if callback else self.blocking_conn
        scan_func = conn.scan
        if set_name:
            scan_func = lambda **kw: conn.sscan(set_name, **kw)
            
        keys = set([])
        cursor = '0'
        cnt = keys_per_call

        def _handle_scan_result(result, cnt=keys_per_call):
            cursor, data = result
            keys.update(data)
            if len(data) < (keys_per_call / 2):
                cnt *= 2
            if cursor == 0:
                # We're done
                callback(keys)
            else:
                scan_func(cursor=cursor, match=pattern,
                          count=cnt,
                          callback=lambda x:_handle_scan_result(x, cnt))

        if callback:
            scan_func(cursor=cursor, match=pattern,
                      count=cnt,
                      callback=_handle_scan_result)
        else:
            while cursor != 0:
                cursor, data = scan_func(cursor=cursor,
                                         match=pattern,
                                         count=cnt)
                if len(data) < (keys_per_call /2):
                    cnt *= 2
                keys.update(data)
            return list(keys)

    def clear_db(self):
        '''Erases all the keys in the database.

        This should really only be used in test scenarios.
        '''
        self.blocking_conn.flushdb()

    @classmethod
    def update_instance(cls, cname):
        ''' Method to update the connection object in case of 
        db config update '''
        if cls._singleton_instance.has_key(cname):
            with cls.__singleton_lock:
                if cls._singleton_instance.has_key(cname):
                    cls._singleton_instance[cname] = cls(cname)

    @classmethod
    def get(cls, otype=None):
        '''Gets a DB connection for a given object type.

        otype - The object type to get the connection for.
                Can be a class object, an instance object or the class name 
                as a string.
        '''
        cname = _object_to_classname(otype)
        
        if not cls._singleton_instance.has_key(cname):
            with cls.__singleton_lock:
                if not cls._singleton_instance.has_key(cname):
                    cls._singleton_instance[cname] = \
                      DBConnection(cname)
        return cls._singleton_instance[cname]

    @classmethod
    def clear_singleton_instance(cls):
        '''
        Clear the singleton instance for each of the classes

        NOTE: To be only used by the test code
        '''
        with cls.__singleton_lock:
            for k in cls._singleton_instance.keys():
                cls._singleton_instance[k].close()
                del cls._singleton_instance[k]

class RedisRetryWrapper(object):
    '''Wraps a redis client so that it retries with exponential backoff.

    You use this class exactly the same way that you would use the
    StrctRedis class. 

    Calls on this object are blocking.

    '''

    def __init__(self, class_name, **kwargs):
        self.conn_kwargs = kwargs
        self.conn_address = None
        self.class_name = class_name
        self.client = None
        self.connection = None
        self._connect()

    def __del__(self):
        self._disconnect()

    def close(self):
        self._disconnect()

    def _connect(self):
        db_address = _get_db_address(self.class_name)
        if db_address != self.conn_address:
            # Reconnect to database because the address has changed
            self._disconnect()
            
            self.connection = blockingRedis.ConnectionPool(
                host=db_address[0], port=db_address[1],
                **self.conn_kwargs)
            self.client = blockingRedis.StrictRedis(
                connection_pool=self.connection)
            self.conn_address = db_address

    def _disconnect(self):
        if self.client is not None:
            self.connection.disconnect()
            self.connection = None
            self.client = None
            self.conn_address = None

    def _get_wrapped_retry_func(self, attr):
        '''Returns an blocking retry function wrapped around the given func.
        '''
        def RetryWrapper(*args, **kwargs):
            cur_try = 0
            busy_count = 0
            
            while True:
                try:
                    self._connect()
                    func = getattr(self.client, attr)
                    return func(*args, **kwargs)
                except redis.exceptions.BusyLoadingError as e:
                    # Redis is busy, so wait
                    _log.warn_n('Redis is busy on attempt %i. Waiting' %
                                busy_count, 5)
                    delay = (1 << busy_count) * 0.2
                    busy_count += 1
                    time.sleep(delay)
                except Exception as e:
                    _log.error('Error talking to sync redis on attempt %i'
                               ' for function %s: %s' % 
                               (cur_try, attr, e))
                    cur_try += 1
                    if cur_try == options.maxRedisRetries:
                        raise

                    # Do an exponential backoff
                    delay = (1 << cur_try) * options.baseRedisRetryWait # in seconds
                    time.sleep(delay)
        return RetryWrapper

    def __getattr__(self, attr):
        '''Allows us to wrap all of the redis-py functions.'''
        
        if hasattr(self.client, attr):
            if hasattr(getattr(self.client, attr), '__call__'):
                return self._get_wrapped_retry_func(
                    attr)
                
        raise AttributeError(attr)

    def pubsub(self, **kwargs):
        self._connect()
        return self.client.pubsub(**kwargs)

class RedisAsyncWrapper(object):
    '''
    Replacement class for tornado-redis 
    
    This is a wrapper class which does redis operation
    in a background thread and on completion transfers control
    back to the tornado ioloop. If you wrap this around gen/Task,
    you can write db operations as if they were synchronous.
    
    usage: 
    value = yield tornado.gen.Task(RedisAsyncWrapper().get, key)


    #TODO: see if we can completely wrap redis-py calls, helpful if
    you can get the callback attribue as well when call is made
    '''

    _thread_pools = {}
    _pool_lock = multiprocessing.RLock()
    
    def __init__(self, class_name, **kwargs):
        self.conn_kwargs = kwargs
        self.conn_address = None
        self.class_name = class_name
        self.client = None
        self.connection = None
        self._lock = threading.RLock()
        self._connect()

    def __del__(self):
        self._disconnect()

    def close(self):
        self._disconnect()

    def _connect(self):
        db_address = _get_db_address(self.class_name)
        if db_address != self.conn_address:
            with self._lock:
                # Reconnect to database because the address has changed
                self._disconnect()
            
                self.connection = blockingRedis.ConnectionPool(
                    host=db_address[0], port=db_address[1],
                    **self.conn_kwargs)
                self.client = blockingRedis.StrictRedis(
                    connection_pool=self.connection)
                self.conn_address = db_address

    def _disconnect(self):
        with self._lock:
            if self.client is not None:
                self.connection.disconnect()
                self.connection = None
                self.client = None
                self.conn_address = None

    @classmethod
    def _get_thread_pool(cls):
        '''Get the thread pool for this process.'''
        with cls._pool_lock:
            try:
                return cls._thread_pools[os.getpid()]
            except KeyError:
                pool = concurrent.futures.ThreadPoolExecutor(
                    options.async_pool_size)
                cls._thread_pools[os.getpid()] = pool
                return pool

    def _get_wrapped_async_func(self, attr):
        '''Returns an asynchronous function wrapped around the given func.

        The asynchronous call has a callback keyword added to it
        '''
        def AsyncWrapper(*args, **kwargs):
            # Find the callback argument
            try:
                callback = kwargs['callback']
                del kwargs['callback']
            except KeyError:
                if len(args) > 0 and hasattr(args[-1], '__call__'):
                    callback = args[-1]
                    args = args[:-1]
                else:
                    raise AttributeError('A callback is necessary')
                    
            io_loop = tornado.ioloop.IOLoop.current()
            
            def _cb(future, cur_try=0, busy_count=0):
                if future.exception() is None:
                    callback(future.result())
                    return
                elif isinstance(future.exception(),
                                redis.exceptions.BusyLoadingError):
                    _log.warn_n('Redis is busy on attempt %i. Waiting' %
                                busy_count)
                    delay = (1 << busy_count) * 0.2
                    busy_count += 1
                else:
                    _log.error('Error talking to async redis on attempt %i for'
                               ' call %s: %s' % 
                               (cur_try, attr, future.exception()))
                    cur_try += 1
                    if cur_try == options.maxRedisRetries:
                        raise future.exception()

                    delay = (1 << cur_try) * options.baseRedisRetryWait # in seconds
                self._connect()
                func = getattr(self.client, attr)
                io_loop.add_timeout(
                    time.time() + delay,
                    lambda: io_loop.add_future(
                        RedisAsyncWrapper._get_thread_pool().submit(
                            func, *args, **kwargs),
                        lambda x: _cb(x, cur_try, busy_count)))

            self._connect()
            func = getattr(self.client, attr)
            future = RedisAsyncWrapper._get_thread_pool().submit(
                func, *args, **kwargs)
            io_loop.add_future(future, _cb)
        return AsyncWrapper
        

    def __getattr__(self, attr):
        '''Allows us to wrap all of the redis-py functions.'''
        if hasattr(self.client, attr):
            if hasattr(getattr(self.client, attr), '__call__'):
                return self._get_wrapped_async_func(attr)
                
        raise AttributeError(attr)
    
    def pipeline(self):
        ''' pipeline '''
        #TODO(Sunil) make this asynchronous
        self._connect()
        return self.client.pipeline()

def _erase_all_data():
    '''Erases all the data from the redis databases.

    This should only be used for testing purposes.
    '''
    _log.warn('Erasing all the data. I hope this is a test.')
    AbstractPlatform._erase_all_data()
    ThumbnailMetadata._erase_all_data()
    ThumbnailURLMapper._erase_all_data()
    VideoMetadata._erase_all_data()

class PubSubConnection(threading.Thread):
    '''Handles a pubsub connection.

    The thread, when running, will service messages on the channels
    subscribed to.
    '''

    __singleton_lock = threading.RLock()
    _singleton_instance = {}

    def __init__(self, class_name):
        '''Init function.

        DO NOT CALL THIS DIRECTLY. Use the get() function instead
        '''
        super(PubSubConnection, self).__init__(name='PubSubConnection[%s]' 
                                               % class_name)
        self.class_name = class_name
        self._client = None
        self._pubsub = None
        self.connected = False
        self._address = None

        self._publock = threading.RLock()
        self._running = threading.Event()
        self._exit = False

        # Futures to keep track of pending subscribe and unsubscribe
        # responses. Keyed by channel name.
        self._sub_futures = {}
        self._unsub_futures = {}

        # The channels subscribed to. pattern => function
        self._channels = {}

        self.daemon = True

        self.connect()

    def __del__(self):
        self.close()
        self.stop()

    def connect(self):
        '''Connects to the database. This is a blocking call.'''
        with self._publock:
            address = _get_db_address(self.class_name)
            _log.info(
                'Connecting to redis at %s for subscriptions of class %s' %
                (address, self.class_name))
            self._client = blockingRedis.StrictRedis(address[0],
                                                     address[1])
            self._pubsub = self._client.pubsub(ignore_subscribe_messages=False)

            self.connected = True
            self._address = address

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def _resubscribe(self):
        '''Resubscribes to channels.'''
        
        # Re-subscribe to channels
        error = None
        for pattern, func in self._channels.items():
            self._running.set()
            if not self.is_alive():
                self.start()
            for i in range(options.maxRedisRetries):
                try:
                    if self._pubsub is None:
                        return
                    yield self._subscribe_impl(func, pattern)
                    break
                except DBConnectionError as e:
                    _log.error('Error subscribing to channel %s: %s' %
                               (pattern, e))
                    error = e
                    delay = (1 << i) * options.baseRedisRetryWait # in seconds
                    yield tornado.gen.sleep(delay)
            if error is not None:
                raise error

    def reconnect(self):
        '''Reconnects to the database.'''
        self.close()
        self.connect()

    def subscribed(self):
        '''Returns true if we are subscribed to something.'''
        return len(self._channels) > 0 or len(self._unsub_futures) > 0

    def run(self):
        error_count = 0
        while self._running.wait() and not self._exit:            
            try:
                with self._publock:
                    if not self.subscribed():
                        # There are no more subscriptions, so wait
                        self._running.clear()
                        continue

                    if self._address != _get_db_address(self.class_name):
                        self.reconnect()
                        # Resubscribe asynchronously because this
                        # thread has to handle the subscription acks
                        thread = threading.Thread(target=self._resubscribe,
                                                  name='resubscribe')
                        thread.daemon = True
                        thread.start()

                    if self._pubsub.connection is not None:
                        # This will cause any callbacks that aren't
                        # subscribe/unsubscribe messages to be called.
                        msg = self._pubsub.get_message()

                        self._handle_sub_unsub_messages(msg)

                        # Look for any subscription or unsubscription timeouts
                        self._handle_timedout_futures(self._unsub_futures)
                        self._handle_timedout_futures(self._sub_futures)

                        error_count = 0
                
            except Exception as e:
                _log.exception('Error in thread listening to objects %s. '
                           ': %s' %
                           (self.__class__.__name__, e))
                self.connected = False
                time.sleep((1<<error_count) * 1.0)
                error_count += 1
                statemon.state.increment('pubsub_errors')

                # Force reconnection
                self.reconnect()
                # Resubscribe asynchronously because this
                # thread has to handle the subscription acks
                thread = threading.Thread(target=self._resubscribe,
                                          name='resubscribe')
                thread.daemon = True
                thread.start()
                        
            time.sleep(0.05)

    def close(self):
        with self._publock:
            if self._pubsub is not None:
                self._pubsub.close()
                self._pubsub = None
                self._client = None
                self.connected = False

    def stop(self):
        '''Stops the thread. It cannot be restarted.'''
        self._exit = True
        self._running.set()

    def _handle_sub_unsub_messages(self, msg):
        '''Handle a subscribe or unsubscribe messages.

        Triggers their callbacks
        '''
        if msg is None:
            return
        
        future = None
        if msg['type'] in \
          blockingRedis.client.PubSub.UNSUBSCRIBE_MESSAGE_TYPES:
            future = self._unsub_futures.pop(msg['channel'], None)
        elif msg['type'] not in \
          blockingRedis.client.PubSub.PUBLISH_MESSAGE_TYPES:
            future = self._sub_futures.pop(msg['channel'], None)

        if future is not None:
            if future[0].set_running_or_notify_cancel():
                _log.debug('Changed subscription state to %s' % msg['channel'])
                future[0].set_result(msg)

    def _handle_timedout_futures(self, future_dict):
        '''Handle any futures that have timed out.'''
        timed_out = []
        for channel in future_dict:
            future, deadline = future_dict.get(channel)
            if time.time() > deadline :
                if future.set_running_or_notify_cancel():
                    future.set_exception(DBConnectionError(
                    'Timeout when changing connection state to channel '
                    '%s' % channel))
                    statemon.state.increment('subscription_errors')
                timed_out.append(channel)

        for channel in timed_out:
            del future_dict[channel]

    def get_parsed_message(self):
        '''Return a parsed message from the channel(s).'''
        with self._publock:
            return self._pubsub.parse_response(block=False)

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def subscribe(self, func, pattern='*', timeout=10.0):
        '''Subscribe to channel(s)

        func - Function to run with each data point
        pattern - Channel to subscribe to

        returns nothing
        '''
        with self._publock:
            self._channels[pattern] = func

        error = None
        for i in range(options.maxRedisRetries):
            try:
                pool = concurrent.futures.ThreadPoolExecutor(1)
                sub_future = yield pool.submit(
                    lambda: self._subscribe_impl(func, pattern,
                                                 timeout=timeout))

                # Start the thread so that we can receive and service messages
                self._running.set()
                if not self.is_alive():
                    self.start()

                yield sub_future
                return
            except DBConnectionError as e:
                error = e
                _log.error('Error subscribing to %s on try %d: %s' %
                           (pattern, i, e))
                delay = (1 << i) * options.baseRedisRetryWait # in seconds
                yield tornado.gen.sleep(delay)

        with self._publock:
            try:
                del self._channels[pattern]
            except KeyError:
                pass

        raise error
                

    def _subscribe_impl(self, func, pattern='*', timeout=10.0):
        try:
            with self._publock:
                if '*' in pattern:
                    self._pubsub.psubscribe(**{pattern: func})
                else:
                    self._pubsub.subscribe(**{pattern: func})

                if pattern in self._sub_futures:
                    return self._sub_futures[pattern][0]
                future = concurrent.futures.Future()
                self._sub_futures[pattern] = (future, time.time() + timeout)
                return future
                
        except redis.exceptions.RedisError as e:
            msg = 'Error subscribing to channel %s: %s' % (pattern, e)
            _log.error(msg)
            statemon.state.increment('subscription_errors')
            raise DBConnectionError(msg)
        except socket.error as e:
            msg = 'Socket error subscribing to channel %s: %s' % (pattern, e)
            _log.error(msg) 
            statemon.state.increment('subscription_errors')
            raise DBConnectionError(msg)

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def unsubscribe(self, channel=None, timeout=10.0):
        '''Unsubscribe from channel.

        channel - Channel to unsubscribe from
        '''
        def _remove_channel():
            with self._publock:
                try:
                    if channel is None:
                        self._channels = {}
                    else:
                        del self._channels[channel]
                except KeyError:
                    return
        

        error = None
        for i in range(options.maxRedisRetries):
            try:
                pool = concurrent.futures.ThreadPoolExecutor(1)
                unsub_future = yield pool.submit(
                    lambda: self._unsubscribe_impl(channel, timeout))
                yield unsub_future
                return

            except DBConnectionError as e:
                error = e
                _log.error('Error unsubscribing from %s on try %d: %s' %
                           (channel, i, e))
                delay = (1 << i) * options.baseRedisRetryWait # in seconds
                yield tornado.gen.sleep(delay)

        raise error

    def _unsubscribe_impl(self, channel=None, timeout=10.0):
        try:
            with self._publock:
                if channel is None:
                    self._pubsub.unsubscribe()
                    self._pubsub.punsubscribe()
                elif '*' in channel:
                    self._pubsub.punsubscribe([channel])
                else:
                    self._pubsub.unsubscribe([channel])
                self._running.set()
                if channel in self._unsub_futures:
                    return self._unsub_futures[channel][0]
                future = concurrent.futures.Future()
                self._unsub_futures[channel] = (future, time.time() + timeout)
                return future
        except redis.exceptions.RedisError as e:
            msg = 'Error unsubscribing to channel %s: %s' % (channel, e)
            _log.error(msg)
            statemon.state.increment('subscription_errors')
            raise DBConnectionError(msg)
        except socket.error as e:
            msg = 'Socket error unsubscribing to channel %s: %s' % (channel, e)
            _log.error(msg) 
            statemon.state.increment('subscription_errors')
            raise DBConnectionError(msg)


    @classmethod
    def get(cls, otype=None):
        '''
        Gets a connection for a given object type.

        otype - The object type to get the connection for.
                Can be a class object, an instance object or the class name 
                as a string.
        '''
        cname = _object_to_classname(otype)
        
        if not cls._singleton_instance.has_key(cname):
            with cls.__singleton_lock:
                if not cls._singleton_instance.has_key(cname):
                    cls._singleton_instance[cname] = \
                      PubSubConnection(cname)
        return cls._singleton_instance[cname]

    @classmethod
    def clear_singleton_instance(cls):
        '''
        Clear the singleton instance for each of the classes

        NOTE: To be only used by the test code
        '''
        with cls.__singleton_lock:
            for inst in cls._singleton_instance.values():
                if inst is not None:
                    inst.stop()
                    inst.close()
            cls._singleton_instance = {}
    

##############################################################################

def id_generator(size=32, 
            chars=string.ascii_lowercase + string.digits):
    ''' Generate a random alpha numeric string to be used as 
        unique ids
    '''
    retval = ''.join(random.choice(chars) for x in range(size))

    return retval

##############################################################################
## Enum types
############################################################################## 

class ThumbnailType(object):
    ''' Thumbnail type enumeration '''
    NEON        = "neon"
    CENTERFRAME = "centerframe"
    BRIGHTCOVE  = "brightcove" # DEPRECATED. Will be DEFAULT instead
    OOYALA      = "ooyala" # DEPRECATED. Will be DEFAULT instead
    RANDOM      = "random"
    FILTERED    = "filtered"
    DEFAULT     = "default" #sent via api request
    CUSTOMUPLOAD = "customupload" #uploaded by the customer/editor

class ExperimentState:
    '''A class that acts like an enum for the state of the experiment.'''
    UNKNOWN = 'unknown'
    RUNNING = 'running'
    COMPLETE = 'complete'
    DISABLED = 'disabled'
    OVERRIDE = 'override' # Experiment has be manually overridden

class MetricType:
    '''The different kinds of metrics that we care about.'''
    LOADS = 'loads'
    VIEWS = 'views'
    CLICKS = 'clicks'
    PLAYS = 'plays'

class IntegrationType(object): 
    BRIGHTCOVE = 'brightcove'
    OOYALA = 'ooyala'
    OPTIMIZELY = 'optimizely'

class DefaultSizes(object): 
    WIDTH = 160 
    HEIGHT = 90 

class ServingControllerType(object): 
    IMAGEPLATFORM = 'imageplatform'

class AccessLevels(object):
    NONE = 0 
    READ = 1 
    UPDATE = 2 
    CREATE = 4 
    DELETE = 8
    ALL_NORMAL_RIGHTS = READ | UPDATE | CREATE | DELETE 
    ADMIN = 16 
    GLOBAL_ADMIN = 32

##############################################################################
class StoredObject(object):
    '''Abstract class to represent an object that is stored in the database.

    Fields can be either native types or other StoreObjects

    This contains common routines for interacting with the data.
    TODO: Convert all the objects to use this consistent interface.
    ''' 
    def __init__(self, key):
        self.key = str(key)
        self.created = self.updated = str(datetime.datetime.utcnow()) 

    def __str__(self):
        return "%s: %s" % (self.__class__.__name__, self.__dict__)

    def __repr__(self):
        return str(self)

    def __cmp__(self, other):
        classcmp = cmp(self.__class__, other.__class__) 
        if classcmp:
            return classcmp

        obj_one = set(self.__dict__).difference(('created', 'updated'))
        obj_two = set(other.__dict__).difference(('created', 'updated')) 
        classcmp = obj_one == obj_two and all(self.__dict__[k] == other.__dict__[k] for k in obj_one)

        if classcmp: 
            return 0
 
        return cmp(self.__dict__, other.__dict__)

    @classmethod
    def key2id(cls, key):
        '''Converts a key to an id'''
        return key

    def _set_keyname(self):
        '''Returns the key in the database for the set that holds this object.

        The result of the key lookup will be a set of objects for this class
        '''
        raise NotImplementedError()

    @classmethod
    def format_key(cls, key):
        return key

    @classmethod
    def is_valid_key(cls, key):
        return True

    def to_dict(self):
        return {
            '_type': self.__class__.__name__,
            '_data': self.__dict__
            }

    def to_json(self):
        '''Returns a json version of the object'''
        return json.dumps(self, default=lambda o: o.to_dict())

    def save(self, callback=None):
        '''Save the object to the database.'''
        db_connection = DBConnection.get(self)
        if not hasattr(self, 'created'): 
            self.created = str(datetime.datetime.utcnow())
        self.updated = str(datetime.datetime.utcnow())
        value = self.to_json()
         
        def _save_and_add2set(pipe):
            pipe.sadd(self._set_keyname(), self.key)
            pipe.set(self.key, value)
            return True
            
        if self.key is None:
            raise Exception("key not set")
        if callback:
            db_connection.conn.transaction(_save_and_add2set,
                                           self._set_keyname(),
                                           self.key,
                                           value_from_callable=True,
                                           callback=callback)
        else:
            return db_connection.blocking_conn.transaction(
                _save_and_add2set,
                self._set_keyname(),
                self.key,
                value_from_callable=True)


    @classmethod
    def _create(cls, key, obj_dict):
        '''Create an object from a dictionary that was created by save().

        Returns None if the object could not be created.
        '''
        if obj_dict:
            # Get the class type to create
            try:
                data_dict = obj_dict['_data']
                classname = obj_dict['_type']
                try:
                    classtype = globals()[classname]
                except KeyError:
                    _log.error('Unknown class of type %s in database key %s'
                               % (classname, key))
                    return None
            except KeyError:
                # For backwards compatibility, we didn't store the
                # type in the databse, so assume that the class is cls
                classtype = cls
                data_dict = obj_dict
            
            # create basic object using the "default" constructor
            obj = classtype(key)

            #populate the object dictionary
            try:
                for k, value in data_dict.iteritems():
                    obj.__dict__[str(k)] = cls._deserialize_field(k, value)
            except ValueError:
                return None
        
            return obj


    @classmethod
    def _deserialize_field(cls, key, value):
        '''Deserializes a field by creating a StoredObject as necessary.'''
        if isinstance(value, dict):
            if '_type' in value and '_data' in value:
                # It is a stored object, so unpack it
                try:
                    classtype = globals()[value['_type']]
                    return classtype._create(key, value)
                except KeyError:
                    _log.error('Unknown class of type %s' % value['_type'])
                    raise ValueError('Bad class type %s' % value['_type'])
            else:
                # It is a dictionary do deserialize each of the fields
                for k, v in value.iteritems():
                    value[str(k)] = cls._deserialize_field(k, v)
        elif hasattr(value, '__iter__'):
            # It is iterable to treat it like a list
            value = [cls._deserialize_field(None, x) for x in value]
        return value

    def get_id(self):
        '''Return the non-namespaced id for the object.'''
        return self.key

    @classmethod
    def get(cls, key, create_default=False, log_missing=True,
            callback=None):
        '''Retrieve this object from the database.

        Inputs:
        key - Key for the object to retrieve
        create_default - If true, then if the object is not in the database, 
                         return a default version. Otherwise return None.
        log_missing - Log if the object is missing in the database.

        Returns the object
        '''
        db_connection = DBConnection.get(cls)

        def cb(result):
            if result:
                obj = cls._create(key, json.loads(result))
                callback(obj)
            else:
                if log_missing:
                    _log.warn('No %s for id %s in db' % (cls.__name__, key))
                if create_default:
                    callback(cls(key))
                else:
                    callback(None)

        if callback:
            db_connection.conn.get(key, cb)
        else:
            jdata = db_connection.blocking_conn.get(key)
            if jdata is None:
                if log_missing:
                    _log.warn('No %s for %s' % (cls.__name__, key))
                if create_default:
                    return cls(key)
                else:
                    return None
            return cls._create(key, json.loads(jdata))

    @classmethod
    def get_many(cls, keys, create_default=False, log_missing=True,
                 callback=None):
        ''' Get many objects of the same type simultaneously

        This is more efficient than one at a time.

        Inputs:
        keys - List of keys to get
        create_default - If true, then if the object is not in the database, 
                         return a default version. Otherwise return None.
        log_missing - Log if the object is missing in the database.
        callback - Optional callback function to call

        Returns:
        A list of cls objects or None depending on create_default settings
        '''
        return cls._get_many_with_raw_keys(keys, create_default, log_missing,
                                           callback=callback)

    @classmethod
    def get_many_with_pattern(cls, pattern, callback=None):
        '''Returns many objects that match a pattern.

        Note, this can be a slow call because getting the keys is slow

        Inputs:
        pattern - A pattern, usually with a * to match keys

        Outputs:
        A list of cls objects
        '''
        retval = []
        db_connection = DBConnection.get(cls)

        def filtered_callback(data_list):
            callback([x for x in data_list if x is not None])

        def process_keylist(keys):
            cls._get_many_with_raw_keys(keys, callback=filtered_callback)
            
        if callback:
            db_connection.fetch_keys_from_db(pattern, keys_per_call=10000,
                                             callback=process_keylist)
        else:
            keys = db_connection.fetch_keys_from_db(pattern,
                                                    keys_per_call=10000)
            return  [x for x in 
                     cls._get_many_with_raw_keys(keys)
                     if x is not None]

    @classmethod
    def _get_many_with_raw_keys(cls, keys, create_default=False,
                                log_missing=True, callback=None):
        '''Gets many objects with raw keys instead of namespaced ones.
        '''
        #MGET raises an exception for wrong number of args if keys = []
        if len(keys) == 0:
            if callback:
                callback([])
                return
            else:
                return []

        db_connection = DBConnection.get(cls)

        def _process(results):
            mappings = [] 
            for key, item in zip(keys, results):
                if item:
                    obj = cls._create(key, json.loads(item))
                else:
                    if log_missing:
                        _log.warn('No %s for %s' % (cls.__name__, key))
                    if create_default:
                        obj = cls(key)
                    else:
                        obj = None
                mappings.append(obj)
            return mappings

        if callback:
            db_connection.conn.mget(
                keys,
                callback=lambda items:callback(_process(items)))
        else:
            items = db_connection.blocking_conn.mget(keys)
            return _process(items)

    
    @classmethod
    def modify(cls, key, func, create_missing=False, callback=None):
        '''Allows you to modify the object in the database atomically.

        While in func, you have a lock on the object so you are
        guaranteed for it not to change. It is automatically saved at
        the end of func.
        
        Inputs:
        func - Function that takes a single parameter (the object being edited)
        key - The key of the object to modify
        create_missing - If True, create the default object if it doesn't exist

        Returns: A copy of the updated object or None, if the object wasn't
                 in the database and thus couldn't be updated.

        Example usage:
        StoredObject.modify('thumb_a', lambda thumb: thumb.update_phash())
        '''
        def _process_one(d):
            
            val = d[key]
            if val is not None:
                func(val)

        if callback:
            return StoredObject.modify_many(
                [key], _process_one, create_missing=create_missing,
                create_class=cls,
                callback=lambda d: callback(d[key]))
        else:
            updated_d = StoredObject.modify_many(
                [key], _process_one,
                create_missing=create_missing,
                create_class=cls)
            return updated_d[key]

    @classmethod
    def modify_many(cls, keys, func, create_missing=False, create_class=None, 
                    callback=None):
        '''Allows you to modify objects in the database atomically.

        While in func, you have a lock on the objects so you are
        guaranteed for them not to change. The objects are
        automatically saved at the end of func.
        
        Inputs:
        func - Function that takes a single parameter (dictionary of key -> object being edited)
        keys - List of keys of the objects to modify
        create_missing - If True, create the default object if it doesn't exist
        create_class - The class of the object to create. If None, 
                       cls is used (which is the most common case)

        Returns: A dictionary of {key -> updated object}. The updated
        object could be None if it wasn't in the database and thus
        couldn't be modified

        Example usage:
        SoredObject.modify_many(['thumb_a'], 
          lambda d: thumb.update_phash() for thumb in d.itervalues())
        '''
        if create_class is None:
            create_class = cls
            
        def _getandset(pipe):
            # mget can't handle an empty list 
            if len(keys) == 0:
                return {}

            items = pipe.mget(keys)
            pipe.multi()

            mappings = {}
            orig_objects = {}
            key_sets = collections.defaultdict(list)
            for key, item in zip(keys, items):
                if item is None:
                    if create_missing:
                        cur_obj = create_class(key)
                        if cur_obj is not None:
                            key_sets[cur_obj._set_keyname()].append(key)
                    else:
                        _log.warn_n('Could not get redis object: %s' % key)
                        cur_obj = None
                else:
                    cur_obj = create_class._create(key, json.loads(item))
                    orig_objects[key] = create_class._create(key,
                                                             json.loads(item))
                mappings[key] = cur_obj
            try:
                func(mappings)
            finally:
                to_set = {}
                for key, obj in mappings.iteritems():
                    if obj is not None and obj != orig_objects.get(key, None):
                        to_set[key] = obj.to_json()

                to_set['updated'] = str(datetime.datetime.utcnow()) 

                if len(to_set) > 0:
                    pipe.mset(to_set)
                for set_key, cur_keys in key_sets.iteritems():
                    pipe.sadd(set_key, *cur_keys)
            return mappings

        db_connection = DBConnection.get(create_class)
        if callback:
            return db_connection.conn.transaction(_getandset, *keys,
                                                  callback=callback,
                                                  value_from_callable=True)
        else:
            return db_connection.blocking_conn.transaction(
                _getandset, *keys, value_from_callable=True)
            
    @classmethod
    def save_all(cls, objects, callback=None):
        '''Save many objects simultaneously'''
        db_connection = DBConnection.get(cls)
        data = {}
        key_sets = collections.defaultdict(list) # set_keyname -> [keys]
        for obj in objects:
            obj.updated = str(datetime.datetime.utcnow())
            data[obj.key] = obj.to_json()
            key_sets[obj._set_keyname()].append(obj.key)

        def _save_and_add2set(pipe):
            for set_key, keys in key_sets.iteritems():
                pipe.sadd(set_key, *keys)
            pipe.mset(data)
            return True            

        lock_keys = key_sets.keys() + data.keys()
        if callback:
            db_connection.conn.transaction(_save_and_add2set,
                                           *lock_keys,
                                           value_from_callable=True,
                                           callback=callback)
        else:
            return db_connection.blocking_conn.transaction(
                _save_and_add2set,
                value_from_callable=True,
                *lock_keys)

    @classmethod
    def _erase_all_data(cls):
        '''Clear the database that contains objects of this type '''
        db_connection = DBConnection.get(cls)
        db_connection.clear_db()

    @classmethod
    def delete(cls, key, callback=None):
        '''Delete an object from the database.

        Returns True if the object was successfully deleted
        '''
        return cls._delete_many_raw_keys([key], callback)

    @classmethod
    def delete_many(cls, keys, callback=None):
        '''Deletes many objects simultaneously

        Inputs:
        keys - List of keys to delete

        Returns:
        True if it was delete sucessfully
        '''
        return cls._delete_many_raw_keys(keys, callback)

    @classmethod
    def _delete_many_raw_keys(cls, keys, callback=None):
        '''Deletes many objects by their raw keys'''
        db_connection = DBConnection.get(cls)
        key_sets = collections.defaultdict(list) # set_keyname -> [keys]
        for key in keys:
            obj = cls(key)
            obj.key = key
            key_sets[obj._set_keyname()].append(key)

        def _del_and_remfromset(pipe):
            for set_key, ks in key_sets.iteritems():
                pipe.srem(set_key, *ks)
                pipe.delete(*ks)
            return True
            
        if callback:
            db_connection.conn.transaction(_del_and_remfromset,
                                           *keys,
                                           value_from_callable=True,
                                           callback=callback)
        else:
            return db_connection.blocking_conn.transaction(
                _del_and_remfromset,
                *keys,
                value_from_callable=True)
        
    @classmethod
    def _handle_all_changes(cls, msg, func, conn, get_object):
        '''Handles any changes to objects subscribed on pubsub.

        Used with subscribe_to_changes.

        Drains the channel of keys that have changed and gets them
        from the database in one big extraction instead of a ton of
        small ones. Then, for each object, func is called once.

        Inputs:
        func - The function to call with each object. 
        conn - The connection we can drain from
        msg - The message structure for the first event
        '''
        keys = [cls.key2id(msg['channel'].partition(':')[2])]
        ops = [msg['data']]
        response = conn.get_parsed_message()
        while response is not None:
            message_type = response[0]
            if message_type in blockingRedis.client.PubSub.PUBLISH_MESSAGE_TYPES:
                ops.append(response[3])
                keys.append(cls.key2id(response[2].partition(':')[2]))

            response = conn.get_parsed_message()

        # Filter out the invalid keys
        filtered = zip(*filter(lambda x: cls.is_valid_key(x[0]),
                                zip(*(keys, ops))))
        if len(filtered) == 0:
            return
        keys, ops = filtered

        if get_object:
            objs = cls.get_many(keys)
        else:
            objs = [None for x in range(len(keys))]

        for key, obj, op in zip(*(keys, objs, ops)):
            if obj is None or isinstance(obj, cls):
                try:
                    func(key, obj, op)
                except Exception as e:
                    _log.error('Unexpected exception on db change when calling'
                               ' %s with arguments %s: %s' % 
                               (func, (key, obj, op), e))

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def subscribe_to_changes(cls, func, pattern='*', get_object=True):
        '''Subscribes to changes in the database.

        When a change occurs, func is called with the key, the updated
        object and the operation. The function must be thread safe as
        it will be called in a thread in a different context.

        Inputs:
        func - The function to call with signature func(key, obj, op)
        pattern - Pattern of keys to subscribe to
        get_object - If True, the object will be grabbed from the db.
                     Otherwise, it will be passed into the function as None
        '''
        conn = PubSubConnection.get(cls)
        
        yield conn.subscribe(
            lambda x: cls._handle_all_changes(x, func, conn, get_object),
            '__keyspace@0__:%s' % cls.format_subscribe_pattern(pattern),
            async=True)

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def unsubscribe_from_changes(cls, channel):
        conn = PubSubConnection.get(cls)
        
        yield conn.unsubscribe(
            '__keyspace@0__:%s' % cls.format_subscribe_pattern(channel),
            async=True)

    @classmethod
    def format_subscribe_pattern(cls, pattern):
        return cls.format_key(pattern)

class StoredObjectIterator():
    '''An iterator that generates objects of a specific type.

    It needs a list of keys to iterate through. Also, can be used
    synchronously in the normal way, or asynchronously by:

    iter = StoredObjectIterator(cls, keys)
    while True:
        item = yield iter.next(async=True)
        if isinstance(item, StopIteration):
          break
    '''
    def __init__(self, obj_class, keys, page_size=100, max_results=None,
                 skip_missing=False):
        '''Create the iterator

        Inputs:
        cls - Type of object to return
        keys - List of keys to iterate through
        page_size - Number of entries to grab from the db at once
        max_results - Maximum number of entries to return
        skip_missing - Should missing entries be skipped on the iteration
        '''
        self.obj_class = obj_class
        self.keys = keys
        self.page_size = page_size
        self.max_results = max_results
        self.curidx = 0
        self.items_returned = 0
        self.cur_objs = []
        self.skip_missing = skip_missing

    def __iter__(self):
        self.curidx = 0
        self.items_returned = 0
        self.cur_objs = []
        return self

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def next(self):
        if (self.max_results is not None and
            self.items_returned >= self.max_results):
            e = StopIteration()
            e.value = StopIteration()
            raise e

        while len(self.cur_objs) == 0:
            if self.curidx >= len(self.keys):
                # Got all the entries
                e = StopIteration()
                e.value = StopIteration()
                raise e
            
            # Get more objects
            self.cur_objs = yield tornado.gen.Task(
                self.obj_class.get_many,
                self.keys[self.curidx:(self.curidx+self.page_size)])
            if self.skip_missing:
                self.cur_objs = [x for x in self.cur_objs if x is not None]
            self.curidx += self.page_size

        self.items_returned += 1
        raise tornado.gen.Return(self.cur_objs.pop())
        

class NamespacedStoredObject(StoredObject):
    '''An abstract StoredObject that is namespaced by the baseclass classname.

    Subclasses of this must define _baseclass_name in the base class
    of the hierarchy. 
    '''
    
    def __init__(self, key):
        super(NamespacedStoredObject, self).__init__(
            self.__class__.format_key(key))

    def get_id(self):
        '''Return the non-namespaced id for the object.'''
        return self.key2id(self.key)

    @classmethod
    def key2id(cls, key):
        '''Converts a key to an id'''
        return re.sub(cls._baseclass_name().lower() + '_', '', key)

    @classmethod
    def _baseclass_name(cls):
        '''Returns the class name of the base class of the hierarchy.

        This should be implemented in the base class as:
        return <Class>.__name__
        '''
        raise NotImplementedError()

    @classmethod
    def _set_keyname(cls):
        return 'objset:%s' % cls._baseclass_name()

    @classmethod
    def format_key(cls, key):
        ''' Format the database key with a class specific prefix '''
        if key and key.startswith(cls._baseclass_name().lower()):
            return key
        else:
            return '%s_%s' % (cls._baseclass_name().lower(), key)

    @classmethod
    def get(cls, key, create_default=False, log_missing=True, callback=None):
        '''Return the object for a given key.'''
        return super(NamespacedStoredObject, cls).get(
            cls.format_key(key),
            create_default=create_default,
            log_missing=log_missing,
            callback=callback)

    @classmethod
    def get_many(cls, keys, create_default=False, log_missing=True,
                 callback=None):
        '''Returns the list of objects from a list of keys.

        Each key must be a tuple
        '''
        return super(NamespacedStoredObject, cls).get_many(
            [cls.format_key(x) for x in keys],
            create_default=create_default,
            log_missing=log_missing,
            callback=callback)

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_all(cls):
        ''' Get all the objects in the database of this type

        Inputs:
        callback - Optional callback function to call

        Returns:
        A list of cls objects.
        '''
        retval = []
        i = cls.iterate_all()
        while True:
            item = yield i.next(async=True)
            if isinstance(item, StopIteration):
                break
            retval.append(item)

        raise tornado.gen.Return(retval)

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def iterate_all(cls, max_request_size=100, max_results=None):
        '''Return an iterator for all the ojects of this type.

        The set of keys to grab happens once so if the db changes while
        the iteration is going, so neither new or deleted objects will
        be returned.

        You can use it asynchronously like:
        iter = cls.get_iterator()
        while True:
          item = yield iter.next(async=True)
          if isinstance(item, StopIteration):
            break

        or just use it synchronously like a normal iterator.
        '''
        keys = yield cls.get_all_keys(async=True)
        raise tornado.gen.Return(
            StoredObjectIterator(cls, keys, page_size=max_request_size,
                                 max_results=max_results, skip_missing=True))

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_all_keys(cls):
        '''Return all the keys in the database for this object type.'''
        db_connection = DBConnection.get(cls)
        raw_keys = yield tornado.gen.Task(db_connection.fetch_keys_from_db,
            set_name=cls._set_keyname())

        raise tornado.gen.Return([x.partition('_')[2] for x in raw_keys if
                                  x is not None])

    @classmethod
    def modify(cls, key, func, create_missing=False, callback=None):
        return super(NamespacedStoredObject, cls).modify(
            cls.format_key(key),
            func,
            create_missing=create_missing,
            callback=callback)

    @classmethod
    def modify_many(cls, keys, func, create_missing=False, callback=None):
        def _do_modify(raw_mappings):
            # Need to convert the keys in the mapping to the ids of the objects
            mod_mappings = dict(((v.get_id(), v) for v in 
                                 raw_mappings.itervalues()))
            return func(mod_mappings)
        
        return super(NamespacedStoredObject, cls).modify_many(
            [cls.format_key(x) for x in keys],
            _do_modify,
            create_missing=create_missing,
            callback=callback)

    @classmethod
    def delete(cls, key, callback=None):
        return super(NamespacedStoredObject, cls).delete(
            cls.format_key(key),
            callback=callback)

    @classmethod
    def delete_many(cls, keys, callback=None):
        return super(NamespacedStoredObject, cls).delete_many(
            [cls.format_key(k) for k in keys],
            callback=callback)

class DefaultedStoredObject(NamespacedStoredObject):
    '''Namespaced object where a get-like operation will never returns None.

    Instead of None, a default object is returned, so a subclass should
    specify a reasonable default constructor
    '''
    def __init__(self, key):
        super(DefaultedStoredObject, self).__init__(key)

    @classmethod
    def get(cls, key, log_missing=True, callback=None):
        return super(DefaultedStoredObject, cls).get(
            key,
            create_default=True,
            log_missing=log_missing,
            callback=callback)

    @classmethod
    def get_many(cls, keys, log_missing=True, callback=None):
        return super(DefaultedStoredObject, cls).get_many(
            keys,
            create_default=True,
            log_missing=log_missing,
            callback=callback)

    @classmethod
    def modify_many(cls, keys, func, create_missing=None, callback=None):
        return super(DefaultedStoredObject, cls).modify_many(
            keys, func, create_missing=True, callback=callback)

class AbstractHashGenerator(object):
    ' Abstract Hash Generator '

    @staticmethod
    def _api_hash_function(_input):
        ''' Abstract hash generator '''
        return hashlib.md5(_input).hexdigest()

class NeonApiKey(NamespacedStoredObject):
    ''' Static class to generate Neon API Key'''

    def __init__(self, a_id, api_key=None):
        super(NeonApiKey, self).__init__(a_id)
        self.api_key = api_key

    @classmethod
    def _baseclass_name(cls):
        '''
        Returns the class name of the base class of the hierarchy.
        '''
        return NeonApiKey.__name__
    
    @classmethod
    def id_generator(cls, size=24, 
            chars=string.ascii_lowercase + string.digits):
        return ''.join(random.choice(chars) for x in range(size))

        
    @classmethod
    def generate(cls, a_id):
        ''' generate api key hash
            if present in DB, then return it
        
        #NOTE: Generate method directly saves the key
        TODO(mdesnoyer): Make this asynchronous
        '''
        api_key = NeonApiKey.id_generator()
        obj = NeonApiKey(a_id, api_key)
        
        # Check if the api_key for the account id exists in the DB
        _api_key = cls.get_api_key(a_id)
        if _api_key is not None:
            return _api_key 
        else:
            if obj.save():
                return api_key

    def to_json(self):
        #NOTE: This is a misnomer. It is being overriden here since the save()
        # function uses to_json() and the NeonApiKey is saved as a plain string
        # in the database
        
        return self.api_key
    
    @classmethod
    def _create(cls, key, obj_dict):
        obj = NeonApiKey(key)
        obj.value = obj_dict
        return obj_dict
    
    @classmethod
    def get_api_key(cls, a_id, callback=None):
        ''' get api key from db '''

        # Use get
        api_key = cls.get(a_id, callback)
        return api_key

    @classmethod
    def get(cls, a_id, callback=None):
        #NOTE: parent get() method uses json.loads() hence overriden here 
        db_connection = DBConnection.get(cls)
        key = cls.format_key(a_id)
        if callback:
            db_connection.conn.get(key, callback) 
        else:
            return db_connection.blocking_conn.get(key) 
   
    @classmethod
    def get_many(cls, keys, callback=None):
        raise NotImplementedError()
    
    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_all(cls, keys, callback=None):
        raise NotImplementedError()

    @classmethod
    def modify(cls, key, func, create_missing=False, callback=None):
        raise NotImplementedError()
    
    @classmethod
    def modify_many(cls, keys, func, create_missing=False, callback=None):
        raise NotImplementedError()

class InternalVideoID(object):
    ''' Internal Video ID Generator '''
    NOVIDEO = 'NOVIDEO' # External video id to specify that there is no video

    VALID_EXTERNAL_REGEX = '[0-9a-zA-Z\-\.]+'
    VALID_INTERNAL_REGEX = ('[0-9a-zA-Z]+_%s' % VALID_EXTERNAL_REGEX)
    
    @staticmethod
    def generate(api_key, vid=None):
        ''' external platform vid --> internal vid '''
        if vid is None:
            vid = InternalVideoID.NOVIDEO
        key = '%s_%s' % (api_key, vid)
        return key

    @staticmethod
    def is_no_video(internal_vid):
        '''Returns true if this video id refers to there not being a video'''
        return internal_vid.partition('_')[2] == InternalVideoID.NOVIDEO

    @staticmethod
    def to_external(internal_vid):
        ''' internal vid -> external platform vid'''
        
        #first part of the key doesn't have _, hence use this below to 
        #generate the internal vid. 
        #note: found later that Ooyala can have _ in their video ids
                
        if "_" not in internal_vid:
            _log.error('key=InternalVideoID msg=Invalid internal id %s' %internal_vid)
            return internal_vid

        vid = "_".join(internal_vid.split('_')[1:])
        return vid

class TrackerAccountID(object):
    ''' Tracker Account ID generation '''
    @staticmethod
    def generate(_input):
        ''' Generate a CRC 32 for Tracker Account ID'''
        return str(abs(binascii.crc32(_input)))

class TrackerAccountIDMapper(NamespacedStoredObject):
    '''
    Maps a given Tracker Account ID to API Key 

    This is needed to keep the tracker id => api_key
    '''
    STAGING = "staging"
    PRODUCTION = "production"

    def __init__(self, tai, api_key=None, itype=None):
        super(TrackerAccountIDMapper, self).__init__(tai)
        self.value = api_key 
        self.itype = itype

    def get_tai(self):
        '''Retrieves the TrackerAccountId of the object.'''
        return self.key.partition('_')[2]

    @classmethod
    def _baseclass_name(cls):
        '''Returns the class name of the base class of the hierarchy.
        '''
        return TrackerAccountIDMapper.__name__
    
    @classmethod
    def get_neon_account_id(cls, tai, callback=None):
        '''
        returns tuple of api_key, type(staging/production)
        '''
        def format_tuple(result):
            ''' format result tuple '''
            if result:
                return result.value, result.itype

        if callback:
            cls.get(tai, lambda x: callback(format_tuple(x)))
        else:
            return format_tuple(cls.get(tai))

class User(NamespacedStoredObject): 
    ''' User 
    
    These are users that can used across multiple systems most notably 
    the API and the current UI. 

    Each of these can be attached to a NeonUserAccount (misnamed, but this 
    is our Application/Customer layer). This will grant the User access to 
    anything the NeonUserAccount can access.  
        
    Users can be associated to many NeonUserAccounts     
    ''' 
    def __init__(self, 
                 username, 
                 password='password', 
                 access_level=AccessLevels.ALL_NORMAL_RIGHTS):
 
        super(User, self).__init__(username)

        # here for the conversion to postgres, not used yet  
        self.user_id = uuid.uuid1().hex

        # the users username, chosen by them, redis key 
        self.username = username

        # the users password_hash, we don't store plain text passwords 
        self.password_hash = sha256_crypt.encrypt(password)

        # short-lived JWtoken that will give user access to API calls 
        self.access_token = None

        # longer-lived JWtoken that will allow a user to refresh a token
        # this token should only be sent over HTTPS to the auth endpoints
        # for now this is not encrypted 
        self.refresh_token = None

        # access level granted to this user, uses class AccessLevels 
        self.access_level = access_level 
 
    @classmethod
    def _baseclass_name(cls):
        '''Returns the class name of the base class of the hierarchy.
        '''
        return User.__name__
        
class NeonUserAccount(NamespacedStoredObject):
    ''' NeonUserAccount

    Every user in the system has a neon account and all other integrations are 
    associated with this account. 

    @videos: video id / jobid map of requests made directly through neon api
    @integrations: all the integrations associated with this acccount

    '''
    def __init__(self, 
                 a_id, 
                 api_key=None, 
                 default_size=(DefaultSizes.WIDTH,DefaultSizes.HEIGHT), 
                 name=None, 
                 abtest=True, 
                 serving_enabled=True, 
                 serving_controller=ServingControllerType.IMAGEPLATFORM, 
                 users=[]):

        # Account id chosen/or generated by the api when account is created 
        self.account_id = a_id 
        splits = '_'.split(a_id)
        if api_key is None and len(splits) == 2:
            api_key = splits[1]
        self.neon_api_key = self.get_api_key() if api_key is None else api_key
        super(NeonUserAccount, self).__init__(self.neon_api_key)
        self.tracker_account_id = TrackerAccountID.generate(self.neon_api_key)
        self.staging_tracker_account_id = \
                TrackerAccountID.generate(self.neon_api_key + "staging") 
        self.videos = {} #phase out,should be stored in neon integration
        # a mapping from integration id -> get_ovp() string
        self.integrations = {}
        # name of the individual who owns the account, mainly for internal use 
        # so we know who it is 
        self.name = name

        # The default thumbnail (w, h) to serve for this account
        self.default_size = default_size
        
        # Priority Q number for processing, currently supports {0,1}
        self.processing_priority = 1

        # Default thumbnail to show if we don't have one for a video
        # under this account.
        self.default_thumbnail_id = None
         
        # create on account creation this gives access to the API, passed via header
        self.api_v2_key = NeonApiKey.id_generator()
        
        # Boolean on wether AB tests can run
        self.abtest = abtest

        # Will thumbnails be served by our system?
        self.serving_enabled = serving_enabled

        # What controller is used to serve the image? Default to imageplatform
        self.serving_controller = serving_controller

        # What users are privy to the information assoicated to this NeonUserAccount
        # simply a list of usernames 
        self.users = users
        
    @classmethod
    def _baseclass_name(cls):
        '''Returns the class name of the base class of the hierarchy.
        '''
        return NeonUserAccount.__name__

    def get_api_key(self):
        '''
        Get the API key for the account, If already in the DB the generate method
        returns it
        '''
        # Note: On DB retrieval the object gets created again, this may lead to
        # creation of an addional api key mapping ; hence prevent it
        # Figure out a cleaner implementation
        try:
            return self.neon_api_key
        except AttributeError:
            if NeonUserAccount.__name__.lower() not in self.account_id:
                return NeonApiKey.generate(self.account_id) 
            return 'None'

    def get_processing_priority(self):
        return self.processing_priority

    def set_processing_priority(self, p):
        self.processing_priority = p

    def add_platform(self, platform):
        '''Adds a platform object to the account.'''
        if len(self.integrations) == 0:
            self.integrations = {}
        self.integrations[platform.integration_id] = platform.get_ovp()

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_platforms(self):
        ''' get all platform accounts for the user '''

        ovp_map = {}
        #TODO: Add Ooyala when necessary
         
        for plat in [NeonPlatform, BrightcovePlatform, 
                     YoutubePlatform, BrightcoveIntegration, OoyalaIntegration]:
            ovp_map[plat.get_ovp()] = plat

        calls = []
        for integration_id, ovp_string in self.integrations.iteritems():
            try:
                plat_type = ovp_map[ovp_string]
                if plat_type == NeonPlatform:
                    calls.append(tornado.gen.Task(plat_type.get,
                                                  self.neon_api_key, '0'))
                else:
                    calls.append(tornado.gen.Task(plat_type.get,
                                                  self.neon_api_key,
                                                  integration_id))
                    
            except KeyError:
                _log.error('key=get_platforms msg=Invalid ovp string: %s' % 
                           ovp_string)

            except Exception as e:
                _log.exception('key=get_platforms msg=Error getting platform '
                               '%s' % e)

        retval = yield calls
        raise tornado.gen.Return(retval)

    @classmethod
    def get_ovp(cls):
        ''' ovp string '''
        return "neon"
    
    def add_video(self, vid, job_id):
        ''' vid,job_id in to videos'''
        
        self.videos[str(vid)] = job_id
    
    def to_json(self):
        ''' to json '''
        return json.dumps(self, default=lambda o: o.__dict__)

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def add_default_thumbnail(self, image, integration_id='0', replace=False):
        '''Adds a default thumbnail to the account.

        Note that the NeonUserAccount object is saved after this change.

        Inputs:
        image - A PIL image that will be added as the default thumb
        integration_id - Used to specify the CDN hosting parameters.
                         Defaults to the one associated with the NeonPlatform
        replace - If true, then will replace the existing default thumb
        '''
        if self.default_thumbnail_id is not None and not replace:
            raise ValueError('The account %s already has a default thumbnail'
                             % self.neon_api_key)

        cur_rank = 0
        if self.default_thumbnail_id is not None:
            old_default = yield tornado.gen.Task(ThumbnailMetadata.get,
                                                 self.default_thumbnail_id)
            if old_default is None:
                raise ValueError('The old thumbnail is not in the database. '
                                 'This should never happen')
            cur_rank = old_default.rank - 1

        cdn_key = CDNHostingMetadataList.create_key(self.neon_api_key,
                                                    integration_id)
        cdn_metadata = yield tornado.gen.Task(
            CDNHostingMetadataList.get,
            cdn_key)

        tmeta = ThumbnailMetadata(
            None,
            InternalVideoID.generate(self.neon_api_key, None),
            ttype=ThumbnailType.DEFAULT,
            rank=cur_rank)
        yield tmeta.add_image_data(image, cdn_metadata=cdn_metadata, async=True)
        self.default_thumbnail_id = tmeta.key
        
        success = yield tornado.gen.Task(tmeta.save)
        if not success:
            raise IOError("Could not save thumbnail")

        success = yield tornado.gen.Task(self.save)
        if not success:
            raise IOError("Could not save account data with new default thumb")

    @classmethod
    def create(cls, json_data):
        ''' create obj from json data'''
        if not json_data:
            return None
        params = json.loads(json_data)
        a_id = params['account_id']
        api_key = params['neon_api_key']
        na = cls(a_id, api_key)
       
        for key in params:
            na.__dict__[key] = params[key]
        
        return na
   
    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_all_accounts(cls):
        ''' Get all NeonUserAccount instances '''
        retval = yield tornado.gen.Task(cls.get_all)
        raise tornado.gen.Return(retval)
    
    @classmethod
    def get_neon_publisher_id(cls, api_key):
        '''
        Get Neon publisher ID; This is also the Tracker Account ID
        '''
        na = cls.get(api_key)
        if nc:
            return na.tracker_account_id

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_internal_video_ids(self):
        '''Return the list of internal videos ids for this account.'''
        db_connection = DBConnection.get(self)
        vids = yield tornado.gen.Task(db_connection.fetch_keys_from_db,
                                      set_name='objset:%s' % self.neon_api_key)
        raise tornado.gen.Return(list(vids))

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def iterate_all_videos(self, max_request_size=100):
        '''Returns an iterator across all the videos for this account in the
        database.

        The iterator can be used asynchronously. See StoredObjectIterator

        The set of keys to grab happens once so if the db changes while
        the iteration is going, so neither new or deleted objects will
        be returned.

        Inputs:
        max_request_size - Maximum number of objects to request from
        the database at a time.
        '''
        vids = yield self.get_internal_video_ids(async=True)
        raise tornado.gen.Return(
            StoredObjectIterator(VideoMetadata, vids,
                                 page_size=max_request_size,
                                 skip_missing=True))

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_all_job_keys(self):
        '''Return a list of (job_id, api_key) of all the jobs for this account.
        '''
        db_connection = DBConnection.get(self)
        base_keys = yield tornado.gen.Task(db_connection.fetch_keys_from_db,
                                           set_name='objset:request:%s' % 
                                           self.neon_api_key)

        raise tornado.gen.Return([x.split('_')[:0:-1] for x in base_keys])

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def iterate_all_jobs(self, max_request_size=100):
        '''Returns an iterator across all the jobs for this account in the
        database.

        The iterator can be used asynchronously. See StoredObjectIterator

        The set of keys to grab happens once so if the db changes while
        the iteration is going, so neither new or deleted objects will
        be returned.

        Inputs:
        max_request_size - Maximum number of objects to request from
        the database at a time.
        '''
        keys = yield self.get_all_job_keys(async=True)
        raise tornado.gen.Return(
            StoredObjectIterator(NeonApiRequest, keys,
                                 page_size=max_request_size,
                                 skip_missing=True))

# define a ProcessingStrategy, that will dictate the behavior of the model.
class ProcessingStrategy(DefaultedStoredObject):
    '''
    Defines the model parameters with which a client wishes their data to be
    analyzed.

    NOTE: The majority of these parameters share their names with the
    parameters that are used to initialize local_video_searcher. For any
    parameter for which this is the case, see local_video_searcher.py for
    more elaborate documentation.
    '''
    def __init__(self, account_id, processing_time_ratio=2.5,
                 local_search_width=32, local_search_step=4, n_thumbs=5,
                 feat_score_weight=2.0, mixing_samples=40, max_variety=True,
                 startend_clip=0.1, adapt_improve=True, analysis_crop=None):
        super(ProcessingStrategy, self).__init__(account_id)

        # The processing time ratio dictates the maximum amount of time the
        # video can spend in processing, which is given by:
        #
        # max_processing_time = (length of video in seconds * 
        #                        processing_time_ratio)
        self.processing_time_ratio = processing_time_ratio

        # (this should rarely need to be changed)
        # Local search width is the size of the local search regions. If the
        # local_search_step is x, then for any frame which starts a local
        # search region i, the frames searched are given by
        # 
        # i : i + local_search_width in steps of x.
        self.local_search_width = local_search_width

        # (this should rarely need to be changed)
        # Local search step gives the step size between frames that undergo
        # analysis in a local search region. See the documentation for
        # local search width for the documentation.
        self.local_search_step = local_search_step

        # The number of thumbs that are desired as output from the video
        # searching process.
        self.n_thumbs = n_thumbs

        # (this should rarely need to be changed)
        # feat_score_weight is a multiplier that allows the feature score to
        # be combined with the valence score. This is given by:
        # 
        # combined score = (valence score) + 
        #                  (feat_score_weight * feature score)
        self.feat_score_weight = feat_score_weight

        # (this should rarely need to be changed)
        # Mixing samples is the number of initial samples to take to get
        # estimates for the running statistics.
        self.mixing_samples = mixing_samples

        # (this should rarely need to be changed)
        # max variety determines whether or not the model should pay attention
        # to the content of the images with respect to the variety of the top
        # thumbnails.
        self.max_variety = max_variety

        # startend clip determines how much of the video should be 'clipped'
        # prior to the analysis, to exclude things like titleframes and
        # credit rolls.
        self.startend_clip = startend_clip

        # adapt improve is a boolean that determines whether or not we should
        # be using CLAHE (contrast-limited adaptive histogram equalization) to
        # improve frames. 
        self.adapt_improve = adapt_improve

        # analysis crop dictates the region of the image that should be
        # excluded prior to the analysis. It can be expressed in three ways:
        #
        # All methods are performed by specifying floats x.
        #
        # Method one: A single float x, 0 < x <= 1.0
        #       - Takes the center (x*100)% of the image. For instance, if x 
        #         were 0.4, then 60% of the image's horizontal and vertical 
        #         would be removed (i.e., 30% off the left, 30% off the right, 
        #         30% off the top, 30% off the bottom). 
        # 
        # Method two: Two floats x y, both between 0 and 1.0 excluding 0.
        #       - Takes (1.0 - x)/2 off the top and (1.0 - x)/2 off the bottom
        #         and (1.0 -y)/2 off the left and (1.0 - y)/2 off the right.
        #
        # Method three: All sides are specified with four floats, clockwise 
        #         order from the top (top, right, bottom, left). Four floats, 
        #         as a list.
        #           NOTE:
        #         In contrast to the other methods, the floats specify how
        #         much to remove from each side (rather than how much to leave
        #         in). So they are all between 0 and 0.5 (although higher
        #         values are possible, they will no longer be with respect to
        #         the center of the image and the behavior can get wonkey). 
        #         Given x1, y1, x2, y2, crops (x1 * 100)% off the top, 
        #         (y1 * 100)% off the right, etc. 
        #         For example, to remove the bottom 1/3rd of an image, you
        #         would specify [0., 0., .3333, 0.]
        self.analysis_crop = analysis_crop

    @classmethod
    def _baseclass_name(cls):
        '''Returns the class name of the base class of the hierarchy.
        '''
        return ProcessingStrategy.__name__

class ExperimentStrategy(DefaultedStoredObject):
    '''Stores information about the experimental strategy to use.

    Keyed by account_id (aka api_key)
    '''
    SEQUENTIAL='sequential'
    MULTIARMED_BANDIT='multi_armed_bandit'
    
    def __init__(self, account_id, exp_frac=0.01,
                 holdback_frac=0.01,
                 min_conversion = 50,
                 frac_adjust_rate = 0.0,
                 only_exp_if_chosen=False,
                 always_show_baseline=True,
                 baseline_type=ThumbnailType.RANDOM,
                 chosen_thumb_overrides=False,
                 override_when_done=True,
                 experiment_type=SEQUENTIAL,
                 impression_type=MetricType.VIEWS,
                 conversion_type=MetricType.CLICKS,
                 max_neon_thumbs=None):
        super(ExperimentStrategy, self).__init__(account_id)
        # Fraction of traffic to experiment on.
        self.exp_frac = exp_frac
        
        # Fraction of traffic in the holdback experiment once
        # convergence is complete
        self.holdback_frac = holdback_frac

        # If true, an experiment will only be run if a thumb is
        # explicitly chosen. This and chosen_thumb_overrides had
        # better not both be true.
        self.only_exp_if_chosen = only_exp_if_chosen

        # minimum combined conversion numbers before calling an experiment
        # complete
        self.min_conversion = min_conversion

        # Fraction adjusting power rate. When this number is 0, it is
        # equivalent to all the serving fractions being the same,
        # while if it is 1.0, the serving fraction will be controlled
        # by the strategy.
        self.frac_adjust_rate = frac_adjust_rate

        # If True, a baseline of baseline_type will always be used in the
        # experiment. The other baseline could be an editor generated
        # one, which is always shown if it's there.
        self.always_show_baseline = always_show_baseline

        # The type of thumbnail to consider the baseline
        self.baseline_type = baseline_type

        # If true, if there is a chosen thumbnail, it automatically
        # takes 100% of the traffic and the experiment is shutdown.
        self.chosen_thumb_overrides =  chosen_thumb_overrides

        # If true, then when the experiment has converged on a best
        # thumbnail, it overrides the majority one and leaves a
        # holdback. If this is false, when the experiment is done, we
        # will only run the best thumbnail in the experiment
        # percentage. This is useful for pilots that are hidden from
        # the editors.
        self.override_when_done = override_when_done

        # The strategy used to run the experiment phase
        self.experiment_type = experiment_type

        # The types of measurements that mean an impression or a
        # conversion for this account
        self.impression_type = impression_type
        self.conversion_type = conversion_type

        # The maximum number of Neon thumbs to run in the
        # experiment. If None, all of them are used.
        self.max_neon_thumbs = max_neon_thumbs

    @classmethod
    def _baseclass_name(cls):
        '''Returns the class name of the base class of the hierarchy.
        '''
        return ExperimentStrategy.__name__
        

class CDNHostingMetadataList(DefaultedStoredObject):
    '''A list of CDNHostingMetadata objects.

    Keyed by (api_key, integration_id). Use the create_key method to
    generate it before calling a normal function like get().
    
    '''
    def __init__(self, key, cdns=None):
        super(CDNHostingMetadataList, self).__init__(key)
        if self.get_id() and len(self.get_id().split('_')) != 2:
            raise ValueError('Invalid key %s. Must be generated using '
                             'create_key()' % self.get_id())
        if cdns is None:
            self.cdns = [NeonCDNHostingMetadata()]
        else:
            self.cdns = cdns

    def __iter__(self):
        '''Iterate through the cdns.'''
        return [x for x in self.cdns if x is not None].__iter__()

    @classmethod
    def create_key(cls, api_key, integration_id):
        '''Create a key for using in this table'''
        return '%s_%s' % (api_key, integration_id)

    @classmethod
    def _baseclass_name(cls):
        '''Returns the class name of the base class of the hierarchy.
        '''
        return CDNHostingMetadataList.__name__


class CDNHostingMetadata(NamespacedStoredObject):
    '''
    Specify how to host the the images with one CDN platform.

    Currently on S3 hosting to customer bucket is well defined

    These objects are not stored directly in the database.  They are
    actually stored in CDNHostingMetadataLists. If you try to save
    them directly, you will get a NotImplementedError.
    ''' 
    
    def __init__(self, key=None, cdn_prefixes=None, resize=False, 
                 update_serving_urls=False,
                 rendition_sizes=None, source_crop=None):

        self.key = key

        # List of url prefixes to put in front of the path. If there
        # is no transport scheme, http:// will be added. Also, there
        # is no trailing slash
        cdn_prefixes = cdn_prefixes or []
        self.cdn_prefixes = map(CDNHostingMetadata._normalize_cdn_prefix,
                                cdn_prefixes)
        
        # If true, the images should be resized into all the desired
        # renditions.
        self.resize = resize

        # Should the images be added to ThumbnailServingURL object?
        self.update_serving_urls = update_serving_urls

        # source crop specifies the region of the image from which
        # the result will originate. It can be expressed in three ways:
        #
        # All methods are performed by specifying floats x.
        #
        # Method one: A single float x, 0 < x <= 1.0
        #       - Takes the center (x*100)% of the image. For instance, if x 
        #         were 0.4, then 60% of the image's horizontal and vertical 
        #         would be removed (i.e., 30% off the left, 30% off the right, 
        #         30% off the top, 30% off the bottom). 
        # 
        # Method two: Two floats x y, both between 0 and 1.0 excluding 0.
        #       - Takes (1.0 - x)/2 off the top and (1.0 - x)/2 off the bottom
        #         and (1.0 -y)/2 off the left and (1.0 - y)/2 off the right.
        #
        # Method three: All sides are specified with four floats, clockwise 
        #         order from the top (top, right, bottom, left). Four floats, 
        #         as a list.
        #           NOTE:
        #         In contrast to the other methods, the floats specify how
        #         much to remove from each side (rather than how much to leave
        #         in). So they are all between 0 and 0.5 (although higher
        #         values are possible, they will no longer be with respect to
        #         the center of the image and the behavior can get wonkey). 
        #         Given x1, y1, x2, y2, crops (x1 * 100)% off the top, 
        #         (y1 * 100)% off the right, etc. 
        #         For example, to remove the bottom 1/3rd of an image, you
        #         would specify [0., 0., .3333, 0.]
        self.source_crop = source_crop

        # A list of image rendition sizes to generate if resize is
        # True. The list is of (w, h) tuples.
        self.rendition_sizes = rendition_sizes or [
            [120, 67],
            [120, 90],
            [160, 90],
            [160, 120],
            [210, 118],
            [320, 180],
            [320, 240],
            [480, 270],
            [480, 360],
            [640, 360],
            [640, 480],
            [1280, 720]]

        # the created and updated on these objects
        # self.created = self.updated = str(datetime.datetime.utcnow())

    # TODO(sunil or mdesnoyer): Write a function to add a new
    # rendition size to the list and upload the requisite images to
    # where they are hosted. Some of the functionality will be in
    # cdnhosting, but this object will have to be saved too. We
    # probably want to update all the images in the account or it
    # could have parameters like a single image, all the images newer
    # than a date etc.

    def save(self):
        raise NotImplementedError()

    @classmethod
    def save_all(cls, *args, **kwargs):
        raise NotImplementedError()

    @classmethod
    def modify(cls, *args, **kwargs):
        raise NotImplementedError()

    @classmethod
    def modify_many(cls, *args, **kwargs):
        raise NotImplementedError()

    @classmethod
    def _create(cls, key, obj_dict):
        obj = super(CDNHostingMetadata, cls)._create(key, obj_dict)

        # Normalize the CDN prefixes
        obj.cdn_prefixes = map(CDNHostingMetadata._normalize_cdn_prefix,
                               obj.cdn_prefixes)
        
        return obj

    @staticmethod
    def _normalize_cdn_prefix(prefix):
      '''Normalizes a cdn prefix so that it starts with a scheme and does
      not end with a slash.

      e.g. http://neon.com
           https://neon.com
      '''
      prefix_split = urlparse.urlparse(prefix, 'http')
      if prefix_split.netloc == '':
        path_split = prefix_split.path.partition('/')
        prefix_split = [x for x in prefix_split]
        prefix_split[1] = path_split[0]
        prefix_split[2] = path_split[1]
      scheme_added = urlparse.urlunparse(prefix_split)
      return scheme_added.strip('/')
      
class S3CDNHostingMetadata(CDNHostingMetadata):
    '''
    If the images are to be uploaded to S3 bucket use this formatter  

    '''
    def __init__(self, key=None, access_key=None, secret_key=None, 
                 bucket_name=None, cdn_prefixes=None, folder_prefix=None,
                 resize=False, update_serving_urls=False, do_salt=True,
                 make_tid_folders=False, rendition_sizes=None, policy=None):
        '''
        Create the object
        '''
        super(S3CDNHostingMetadata, self).__init__(
            key, cdn_prefixes, resize, update_serving_urls, rendition_sizes)
        self.access_key = access_key # S3 access key
        self.secret_key = secret_key # S3 secret access key
        self.bucket_name = bucket_name # S3 bucket to host in
        self.folder_prefix = folder_prefix # Folder prefix to host in

        # Add a random named directory between folder prefix and the 
        # image name? Useful for performance when serving.
        self.do_salt = do_salt

        # make folders for easy navigation. This puts the image in the
        # form <api_key>/<video_id>/<thumb_id>.jpg
        self.make_tid_folders = make_tid_folders

        # What aws policy should the images be uploaded with
        self.policy = policy

class NeonCDNHostingMetadata(S3CDNHostingMetadata):
    '''
    Hosting on S3 using the Neon keys.
    
    This default hosting just uses pure S3, no cloudfront.
    '''
    def __init__(self, key=None,
                 bucket_name='n3.neon-images.com',
                 cdn_prefixes=None,
                 folder_prefix=None,
                 resize=True,
                 update_serving_urls=True,
                 do_salt=True,
                 make_tid_folders=False,
                 rendition_sizes=None):
        super(NeonCDNHostingMetadata, self).__init__(
            key,
            bucket_name=bucket_name,
            cdn_prefixes=(cdn_prefixes or ['n3.neon-images.com']),
            folder_prefix=folder_prefix,
            resize=resize,
            update_serving_urls=update_serving_urls,
            do_salt=do_salt,
            make_tid_folders=make_tid_folders,
            rendition_sizes=rendition_sizes,
            policy='public-read')

class PrimaryNeonHostingMetadata(S3CDNHostingMetadata):
    '''
    Primary Neon S3 Hosting
    This is where the primary copy of the thumbnails are stored
    
    @make_tid_folders: If true, _ is replaced by '/' to create folder
    '''
    def __init__(self, key=None,
                 bucket_name='host-thumbnails',
                 folder_prefix=None):
        super(PrimaryNeonHostingMetadata, self).__init__(
            key,
            bucket_name=bucket_name,
            folder_prefix=folder_prefix,
            resize=False,
            update_serving_urls=False,
            do_salt=False,
            make_tid_folders=True,
            policy='public-read')

class CloudinaryCDNHostingMetadata(CDNHostingMetadata):
    '''
    Cloudinary images
    '''

    def __init__(self, key=None):
        super(CloudinaryCDNHostingMetadata, self).__init__(
            key,
            resize=False,
            update_serving_urls=False)

class AkamaiCDNHostingMetadata(CDNHostingMetadata):
    '''
    Akamai Netstorage CDN Metadata
    '''

    def __init__(self, key=None, host=None, akamai_key=None, akamai_name=None,
                 folder_prefix=None, cdn_prefixes=None, rendition_sizes=None,
                 cpcode=None):
        super(AkamaiCDNHostingMetadata, self).__init__(
            key,
            cdn_prefixes=cdn_prefixes,
            resize=True,
            update_serving_urls=True,
            rendition_sizes=rendition_sizes)

        # Host for uploading to akamai. Can have http:// or not
        self.host = host

        # Parameters to talk to akamai
        self.akamai_key = akamai_key
        self.akamai_name = akamai_name

        # The folder prefix to prepend to where the file will be
        # stored and served from. Slashes at the beginning and end are
        # optional
        self.folder_prefix=folder_prefix

        # CPCode string for uploading to Akamai. Should be something
        # like 17645
        self.cpcode = cpcode

    @classmethod
    def _create(cls, key, obj_dict):
        obj = super(AkamaiCDNHostingMetadata, cls)._create(key, obj_dict)

        # An old object could have had a baseurl, which was smashed
        # together the folder prefix and cpcode. That was confusing,
        # but in case there's an old object around, fix it. Also, in
        # that case, the cdn_prefixes could have had the folder prefix
        # in them, so remove them.
        if hasattr(obj, 'baseurl'):
            split = obj.baseurl.strip('/').partition('/')
            obj.cpcode = split[0].strip('/')
            obj.folder_prefix = split[2].strip('/')
            obj.cdn_prefixes = [re.sub(obj.folder_prefix, '', x).strip('/')
                                for x in obj.cdn_prefixes]
            del obj.baseurl
        
        return obj

class AbstractIntegration(NamespacedStoredObject):
    ''' Abstract Integration class '''

    def __init__(self, enabled=True):
        
        integration_id = uuid.uuid1().hex
        super(AbstractIntegration, self).__init__(integration_id)
        self.integration_id = integration_id
        
        # should this integration be used 
        self.enabled = enabled

    @classmethod
    def _baseclass_name(cls):
        return AbstractIntegration.__name__


# DEPRECATED use AbstractIntegration instead
class AbstractPlatform(NamespacedStoredObject):
    ''' Abstract Platform/ Integration class

    The ids for these objects are tuples of (type, api_key, i_id)
    type can be None, in which case it becomes cls._baseclass_name()
    '''

    def __init__(self, api_key, i_id=None, abtest=False, enabled=True, 
                serving_enabled=True,
                serving_controller=ServingControllerType.IMAGEPLATFORM):
        super(AbstractPlatform, self).__init__((None, api_key, i_id))
        self.neon_api_key = api_key 
        self.integration_id = i_id 
        self.videos = {} # External video id (Original Platform VID) => Job ID
        self.abtest = abtest # Boolean on wether AB tests can run
        self.enabled = enabled # Account enabled for auto processing of videos 

        # Will thumbnails be served by our system?
        self.serving_enabled = serving_enabled

        # What controller is used to serve the image? Default to imageplatform
        self.serving_controller = serving_controller 

    @classmethod
    def format_key(cls, key):
        if isinstance(key, basestring):
            # It's already the proper key
            return key

        if len(key) == 2:
            typ = None
            api_key, i_id = key
        else:
            typ, api_key, i_id = key
        if typ is None:
            typ = cls._baseclass_name().lower()
        api_splits = api_key.split('_')
        if len(api_splits) > 1:
            api_key, i_id = api_splits[1:]
        return '_'.join([typ, api_key, i_id])

    @classmethod
    def key2id(cls, key):
        '''Converts a key to an id'''
        return key.split('_')

    @classmethod
    def _baseclass_name(cls):
        return cls.__name__

    @classmethod
    def _set_keyname(cls):
        return 'objset:%s' % cls._baseclass_name()
   
    @classmethod
    def _create(cls, key, obj_dict):
        def __get_type(key):
            '''
            Get the platform type
            '''
            platform_type = key.split('_')[0]
            typemap = {
                'neonplatform' : NeonPlatform,
                'brightcoveplatform' : BrightcovePlatform,
                'ooyalaplatform' : OoyalaPlatform,
                'youtubeplatform' : YoutubePlatform
                }
            try:
                platform = typemap[platform_type]
                return platform.__name__
            except KeyError, e:
                _log.exception("Invalid Platform Object")
                raise ValueError() # is this the right exception to throw?

        if obj_dict:
            if not '_type' in obj_dict or not '_data' in obj_dict:
                obj_dict = {
                    '_type': __get_type(obj_dict['key']),
                    '_data': copy.deepcopy(obj_dict)
                }
            
            return super(AbstractPlatform, cls)._create(cls.format_key(key),
                                                        obj_dict)

    def save(self, callback=None):
        raise NotImplementedError("To save this object use modify()")
        # since we need a default constructor with empty strings for the 
        # eval magic to work, check here to ensure apikey and i_id aren't empty
        # since the key is generated based on them
        if self.neon_api_key == '' or self.integration_id == '':
            raise Exception('Invalid initialization of AbstractPlatform or its\
                subclass object. api_key and i_id should not be empty')

        super(AbstractPlatform, self).save(callback)

    @classmethod
    def get(cls, api_key, i_id, callback=None):
        ''' get instance '''
        return super(AbstractPlatform, cls).get(
            (None, api_key, i_id), callback=callback)
    
    @classmethod
    def modify(cls, api_key, i_id, func, create_missing=False, callback=None):
        def _set_parameters(x):
            typ, api_key, i_id = x.get_id()
            x.neon_api_key = api_key
            x.integration_id = i_id
            func(x)
            
        return super(AbstractPlatform, cls).modify(
            (None, api_key, i_id),
            _set_parameters,
            create_missing=create_missing,
            callback=callback)

    @classmethod
    def modify_many(cls, keys, func, create_missing=True, callback=None):
        def _set_parameters(objs):
            for x in objs.itervalues():
                typ, api_key, i_id = x.get_id()
                x.neon_api_key = api_key
                x.integration_id = i_id
            func(objs)

        return super(AbstractPlatform, cls).modify_many(
            keys,
            _set_parameters,
            create_missing=create_missing,
            callback=callback)

    @classmethod
    def delete(cls, api_key, i_id, callback=None):
        return super(AbstractPlatform, cls).delete(
            (None, api_key, i_id),
            callback=callback)

    def to_json(self):
        ''' to json '''
        return json.dumps(self, default=lambda o: o.__dict__) 

    def add_video(self, vid, job_id):
        ''' external video id => job_id '''
        self.videos[str(vid)] = job_id

    def get_videos(self):
        ''' list of external video ids '''
        return self.videos.keys()
    
    def get_internal_video_ids(self):
        ''' return list of internal video ids for the account ''' 
        i_vids = [] 
        for vid in self.videos.keys(): 
            i_vids.append(InternalVideoID.generate(self.neon_api_key, vid))
        return i_vids

    @classmethod
    def get_ovp(cls):
        ''' ovp string '''
        raise NotImplementedError

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_all_keys(cls):
        '''The keys will be of the form (type, api_key, integration_id).'''
        
        neon_keys = yield NeonPlatform._get_all_keys_impl(async=True)
        bc_keys = yield BrightcovePlatform._get_all_keys_impl(async=True)
        oo_keys = yield OoyalaPlatform._get_all_keys_impl(async=True)
        yt_keys = yield YoutubePlatform._get_all_keys_impl(async=True)

        keys = neon_keys + bc_keys + oo_keys + yt_keys

        raise tornado.gen.Return(keys)

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def _get_all_keys_impl(cls):
        keys = yield super(AbstractPlatform, cls).get_all_keys(async=True)

        raise tornado.gen.Return([[cls._baseclass_name().lower()] + x.split('_') 
                                  for x in keys])

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def subscribe_to_changes(cls, func, pattern='*', get_object=True):
        yield [
            NeonPlatform.subscribe_to_changes(func, pattern, get_object,
                                              async=True),
            BrightcovePlatform.subscribe_to_changes(
                func, pattern, get_object, async=True),
            YoutubePlatform.subscribe_to_changes(
                func, pattern, get_object, async=True),
            OoyalaPlatform.subscribe_to_changes(
                func, pattern, get_object, async=True)]

    @classmethod
    @tornado.gen.coroutine
    def _subscribe_to_changes_impl(cls, func, pattern, get_object):
        yield super(AbstractPlatform, cls).subscribe_to_changes(
            func, pattern, get_object, async=True)

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def unsubscribe_from_changes(cls, channel):
        yield [
            NeonPlatform.unsubscribe_from_changes(channel, async=True),
            BrightcovePlatform.unsubscribe_from_changes(channel, async=True),
            YoutubePlatform.unsubscribe_from_changes(channel, async=True),
            OoyalaPlatform.unsubscribe_from_changes(channel, async=True)]

    @classmethod
    @tornado.gen.coroutine
    def _unsubscribe_from_changes_impl(cls, channel):
        yield super(AbstractPlatform, cls).unsubscribe_from_changes(
            channel, async=True)

    @classmethod
    def format_subscribe_pattern(cls, pattern):
        return '%s_%s' % (cls._baseclass_name().lower(), pattern)
    

    @classmethod
    def _erase_all_data(cls):
        ''' erase all data ''' 
        db_connection = DBConnection.get(cls)
        db_connection.clear_db()
 

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def delete_all_video_related_data(self, platform_vid,
            *args, **kwargs):
        '''
        Delete all data related to a given video

        request, vmdata, thumbs, thumb serving urls
        
        #NOTE: Don't you dare call this method unless you really want to 
        delete 
        '''
        
        do_you_want_to_delete = kwargs.get('really_delete_keys', False)
        if do_you_want_to_delete == False:
            return

        def _del_video(p_inst):
            try:
                p_inst.videos.pop(platform_vid)
            except KeyError, e:
                _log.error('no such video to delete')
                return
        
        i_vid = InternalVideoID.generate(self.neon_api_key, 
                                         platform_vid)
        vm = yield tornado.gen.Task(VideoMetadata.get, i_vid)
        # update platform instance
        yield tornado.gen.Task(self.modify,
                               self.neon_api_key, '0',
                               _del_video)

        # delete the request object
        yield tornado.gen.Task(NeonApiRequest.delete,
                               self.videos[platform_vid],
                               self.neon_api_key)

        if vm is not None:
            # delete the video object
            yield tornado.gen.Task(VideoMetadata.delete, i_vid)

            # delete the thumbnails
            yield tornado.gen.Task(ThumbnailMetadata.delete_many,
                                   vm.thumbnail_ids)

            # delete the serving urls
            yield tornado.gen.Task(ThumbnailServingURLs.delete_many,
                                   vm.thumbnail_ids)
        
class NeonPlatform(AbstractPlatform):
    '''
    Neon Integration ; stores all info about calls via Neon API
    '''
    def __init__(self, api_key, a_id=None, abtest=False):
        # By default integration ID 0 represents 
        # Neon Platform Integration (access via neon api)
        
        super(NeonPlatform, self).__init__(api_key, '0', abtest)
        self.account_id = a_id
        self.neon_api_key = api_key 
   
    @classmethod
    def get_ovp(cls):
        ''' ovp string '''
        return "neon"

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_all_keys(cls):
        keys = yield cls._get_all_keys_impl(async=True)
        raise tornado.gen.Return(keys)

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def subscribe_to_changes(cls, func, pattern='*', get_object=True):
        yield cls._subscribe_to_changes_impl(func, pattern, get_object)

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def unsubscribe_from_changes(cls, channel):
        yield cls._unsubscribe_from_changes_impl(channel)

class BrightcoveIntegration(AbstractIntegration):
    ''' Brightcove Integration class '''

    REFERENCE_ID = '_reference_id'
    BRIGHTCOVE_ID = '_bc_id'
    
    def __init__(self, i_id=None, a_id='', p_id=None, 
                rtoken=None, wtoken=None,
                last_process_date=None, abtest=False, callback_url=None,
                uses_batch_provisioning=False,
                id_field=BRIGHTCOVE_ID,
                enabled=True,
                serving_enabled=True,
                oldest_video_allowed=None):

        ''' On every request, the job id is saved '''

        super(BrightcoveIntegration, self).__init__(enabled)
        self.account_id = a_id
        self.publisher_id = p_id
        self.read_token = rtoken
        self.write_token = wtoken
        #The publish date of the last processed video - UTC timestamp seconds
        self.last_process_date = last_process_date 
        self.linked_youtube_account = False
        self.account_created = time.time() #UTC timestamp of account creation
        self.rendition_frame_width = None #Resolution of video to process
        self.video_still_width = 480 #default brightcove still width
        # the ids of playlist to create video requests from
        self.playlist_feed_ids = []
        # the url that will be called when a video is finished processing 
        self.callback_url = callback_url

        # Does the customer use batch provisioning (i.e. FTP
        # uploads). If so, we cannot rely on the last modified date of
        # videos. http://support.brightcove.com/en/video-cloud/docs/finding-videos-have-changed-media-api
        self.uses_batch_provisioning = uses_batch_provisioning

        # Which custom field to use for the video id. If it is
        # BrightcovePlatform.REFERENCE_ID, then the reference_id field
        # is used. If it is BRIGHTCOVE_ID, the 'id' field is used.
        self.id_field = id_field

        # A ISO date string of the oldest video publication date to
        # ingest even if is updated in Brightcove.
        self.oldest_video_allowed = oldest_video_allowed

    @classmethod
    def get_ovp(cls):
        ''' return ovp name'''
        return "brightcove_integration"

    def get_api(self, video_server_uri=None):
        '''Return the Brightcove API object for this platform integration.'''
        return api.brightcove_api.BrightcoveApi(
            self.neon_api_key, self.publisher_id, 
            self.read_token, self.write_token) 

    def set_rendition_frame_width(self, f_width):
        ''' Set framewidth of the video resolution to process '''
        self.rendition_frame_width = f_width

    def set_video_still_width(self, width):
        ''' Set framewidth of the video still to be used 
            when the still is updated in the brightcove account '''
        self.video_still_width = width

class CNNIntegration(AbstractIntegration):
    ''' CNN Integration class '''

    def __init__(self, 
                 account_id='',
                 api_key_ref='', 
                 enabled=True, 
                 last_process_date=None):  

        ''' On every successful processing, the last video processed date is saved '''

        super(CNNIntegration, self).__init__(enabled)
        # The publish date of the last video we looked at - ISO 8601
        self.last_process_date = last_process_date 
        # user.neon_api_key this integration belongs to 
        self.account_id = account_id
        # the api_key required to make requests to cnn api - external
        self.api_key_ref = api_key_ref

# DEPRECATED use BrightcoveIntegration instead 
class BrightcovePlatform(AbstractPlatform):
    ''' Brightcove Platform/ Integration class '''
    REFERENCE_ID = '_reference_id'
    BRIGHTCOVE_ID = '_bc_id'
    
    def __init__(self, api_key, i_id=None, a_id='', p_id=None, 
                rtoken=None, wtoken=None, auto_update=False,
                last_process_date=None, abtest=False, callback_url=None,
                uses_batch_provisioning=False,
                id_field=BRIGHTCOVE_ID,
                enabled=True,
                serving_enabled=True,
                oldest_video_allowed=None):

        ''' On every request, the job id is saved '''

        super(BrightcovePlatform, self).__init__(api_key, i_id, abtest,
                                                 enabled, serving_enabled)
        self.account_id = a_id
        self.publisher_id = p_id
        self.read_token = rtoken
        self.write_token = wtoken
        self.auto_update = auto_update 
        #The publish date of the last processed video - UTC timestamp 
        self.last_process_date = last_process_date 
        self.linked_youtube_account = False
        self.account_created = time.time() #UTC timestamp of account creation
        self.rendition_frame_width = None #Resolution of video to process
        self.video_still_width = 480 #default brightcove still width
        # the ids of playlist to create video requests from
        self.playlist_feed_ids = []
        # the url that will be called when a video is finished processing 
        self.callback_url = callback_url

        # Does the customer use batch provisioning (i.e. FTP
        # uploads). If so, we cannot rely on the last modified date of
        # videos. http://support.brightcove.com/en/video-cloud/docs/finding-videos-have-changed-media-api
        self.uses_batch_provisioning = uses_batch_provisioning

        # Which custom field to use for the video id. If it is
        # BrightcovePlatform.REFERENCE_ID, then the reference_id field
        # is used. If it is BRIGHTCOVE_ID, the 'id' field is used.
        self.id_field = id_field

        # A ISO date string of the oldest video publication date to
        # ingest even if is updated in Brightcove.
        self.oldest_video_allowed = oldest_video_allowed

    @classmethod
    def get_ovp(cls):
        ''' return ovp name'''
        return "brightcove"

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def subscribe_to_changes(cls, func, pattern='*', get_object=True):
        yield cls._subscribe_to_changes_impl(func, pattern, get_object)

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def unsubscribe_from_changes(cls, channel):
        yield cls._unsubscribe_from_changes_impl(channel)

    def get_api(self, video_server_uri=None):
        '''Return the Brightcove API object for this platform integration.'''
        return api.brightcove_api.BrightcoveApi(
            self.neon_api_key, self.publisher_id,
            self.read_token, self.write_token)

    def set_rendition_frame_width(self, f_width):
        ''' Set framewidth of the video resolution to process '''
        self.rendition_frame_width = f_width

    def set_video_still_width(self, width):
        ''' Set framewidth of the video still to be used 
            when the still is updated in the brightcove account '''
        self.video_still_width = width

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_all_keys(cls):
        keys = yield cls._get_all_keys_impl(async=True)
        raise tornado.gen.Return(keys)

class YoutubePlatform(AbstractPlatform):
    ''' Youtube platform integration '''

    # TODO(Sunil): Fix this class when Youtube is implemented 

    def __init__(self, api_key, i_id=None, a_id='', access_token=None,
                 refresh_token=None,
                expires=None, auto_update=False, abtest=False):
        super(YoutubePlatform, self).__init__(api_key, i_id, abtest)
        self.account_id = a_id
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.expires = expires
        self.generation_time = None
        self.valid_until = 0  

        #if blob is being created save the time when access token was generated
        if access_token:
            self.valid_until = time.time() + float(expires) - 50
        self.auto_update = auto_update
    
        self.channels = None

    @classmethod
    def get_ovp(cls):
        ''' ovp '''
        return "youtube"

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def subscribe_to_changes(cls, func, pattern='*', get_object=True):
        yield cls._subscribe_to_changes_impl(func, pattern, get_object)

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def unsubscribe_from_changes(cls, channel):
        yield cls._unsubscribe_from_changes_impl(channel)
    
    def get_access_token(self, callback):
        ''' Get a valid access token, if not valid -- get new one and set expiry'''
        def access_callback(result):
            if result:
                self.access_token = result
                self.valid_until = time.time() + 3550
                callback(self.access_token)
            else:
                callback(False)

        #If access token has expired
        if time.time() > self.valid_until:
            yt = api.youtube_api.YoutubeApi(self.refresh_token)
            yt.get_access_token(access_callback)
        else:
            #return current token
            callback(self.access_token)
   
    def add_channels(self, callback):
        '''
        Add a list of channels that the user has
        Get a valid access token first
        '''
        def save_channel(result):
            if result:
                self.channels = result
                callback(True)
            else:
                callback(False)

        def atoken_exec(atoken):
            if atoken:
                yt = api.youtube_api.YoutubeApi(self.refresh_token)
                yt.get_channels(atoken, save_channel)
            else:
                callback(False)

        self.get_access_token(atoken_exec)


    def get_videos(self, callback, channel_id=None):
        '''
        get list of videos from youtube
        '''

        def atoken_exec(atoken):
            if atoken:
                yt = api.youtube_api.YoutubeApi(self.refresh_token)
                yt.get_videos(atoken, playlist_id, callback)
            else:
                callback(False)

        if channel_id is None:
            playlist_id = self.channels[0]["contentDetails"]["relatedPlaylists"]["uploads"] 
            self.get_access_token(atoken_exec)
        else:
            # Not yet supported
            callback(None)

    def create_job(self):
        '''
        Create youtube api request
        '''
        pass

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_all_keys(cls):
        keys = yield cls._get_all_keys_impl(async=True)
        raise tornado.gen.Return(keys)

class OoyalaIntegration(AbstractIntegration):
    '''
    OOYALA Integration
    '''
    def __init__(self, 
                 i_id=None, 
                 a_id='', 
                 p_code=None, 
                 api_key=None, 
                 api_secret=None): 
        '''
        Init ooyala platform 
        
        Partner code, o_api_key & api_secret are essential 
        for api calls to ooyala 

        '''
        super(OoyalaIntegration, self).__init__()
        self.account_id = a_id
        self.partner_code = p_code
        self.api_key = api_key
        self.api_secret = api_secret 
 
    @classmethod
    def get_ovp(cls):
        ''' return ovp name'''
        return "ooyala_integration"

    @classmethod
    def generate_signature(cls, secret_key, http_method, 
                    request_path, query_params, request_body=''):
        ''' Generate signature for ooyala requests'''
        signature = secret_key + http_method.upper() + request_path
        for key, value in query_params.iteritems():
            signature += key + '=' + value
            signature = base64.b64encode(hashlib.sha256(signature).digest())[0:43]
            signature = urllib.quote_plus(signature)
            return signature 
    
# DEPRECATED use OoyalaIntegration instead 
class OoyalaPlatform(AbstractPlatform):
    '''
    OOYALA Platform
    '''
    def __init__(self, api_key, i_id=None, a_id='', p_code=None, 
                 o_api_key=None, api_secret=None, auto_update=False): 
        '''
        Init ooyala platform 
        
        Partner code, o_api_key & api_secret are essential 
        for api calls to ooyala 

        '''

        super(OoyalaPlatform, self).__init__(api_key, i_id)
 
        self.account_id = a_id
        self.partner_code = p_code
        self.ooyala_api_key = o_api_key
        self.api_secret = api_secret 
        self.auto_update = auto_update 
    
    @classmethod
    def get_ovp(cls):
        ''' return ovp name'''
        return "ooyala"

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def subscribe_to_changes(cls, func, pattern='*', get_object=True):
        yield cls._subscribe_to_changes_impl(func, pattern, get_object)

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def unsubscribe_from_changes(cls, channel):
        yield cls._unsubscribe_from_changes_impl(channel)
    
    @classmethod
    def generate_signature(cls, secret_key, http_method, 
                    request_path, query_params, request_body=''):
        ''' Generate signature for ooyala requests'''
        signature = secret_key + http_method.upper() + request_path
        for key, value in query_params.iteritems():
            signature += key + '=' + value
            signature = base64.b64encode(hashlib.sha256(signature).digest())[0:43]
            signature = urllib.quote_plus(signature)
            return signature 
    
    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_all_keys(cls):
        keys = yield cls._get_all_keys_impl(async=True)
        raise tornado.gen.Return(keys)

#######################
# Request Blobs 
######################

class RequestState(object):
    'Request state enumeration'

    UNKNOWN    = "unknown"
    SUBMIT     = "submit"
    PROCESSING = "processing"
    FINALIZING = "finalizing" # In the process of finalizing the request
    REQUEUED   = "requeued"
    FINISHED   = "finished"
    SERVING    = "serving" # Thumbnails are ready to be served 
    INT_ERROR  = "internal_error" # Neon had some code error
    CUSTOMER_ERROR = "customer_error" # customer request had a partial error 
    REPROCESS  = "reprocess" #new state added to support clean reprocessing

    # The following states are all DEPRECATED
    SERVING_AND_ACTIVE = "serving_active" # DEPRECATED    
    FAILED     = "failed" # DEPRECATED in favor of INT_ERROR, CUSTOMER_ERROR
    ACTIVE     = "active" # DEPRECATED. Thumbnail selected by editor; Only releavant to BC

class CallbackState(object):
    '''State enums for callbacks being sent.'''
    NOT_SENT = 'not_sent' # Callback has not been sent
    SUCESS = 'sucess' # Callback was sent sucessfully
    ERROR = 'error' # Error sending the callback

class NeonApiRequest(NamespacedStoredObject):
    '''
    Instance of this gets created during request creation
    (Neon web account, RSS Cron)
    Json representation of the class is saved in the server queue and redis  
    '''

    def __init__(self, job_id, api_key=None, vid=None, title=None, url=None, 
            request_type=None, http_callback=None, default_thumbnail=None,
            integration_type='neon', integration_id='0',
            external_thumbnail_id=None, publish_date=None,
            callback_state=CallbackState.NOT_SENT):
        splits = job_id.split('_')
        if len(splits) == 3:
            # job id was given as the raw key
            job_id = splits[2]
            api_key = splits[1]
        super(NeonApiRequest, self).__init__(
            self._generate_subkey(job_id, api_key))
        self.job_id = job_id
        self.api_key = api_key 
        self.video_id = vid #external video_id
        self.video_title = title
        self.video_url = url
        self.request_type = request_type
        # The url to send the callback response
        self.callback_url = http_callback
        self.callback_state = callback_state
        self.state = RequestState.SUBMIT
        self.fail_count = 0 # Number of failed processing tries
        
        self.integration_type = integration_type
        self.integration_id = integration_id
        self.default_thumbnail = default_thumbnail # URL of a default thumb
        self.external_thumbnail_id = external_thumbnail_id

        # The job response. Should be a dictionary defined by 
        # VideoCallbackResponse
        self.response = {}

        # API Method
        self.api_method = None
        self.api_param  = None
        self.publish_date = publish_date # ISO date format of when video is published
       
        # field used to store error message on partial error, explict error or 
        # additional information about the request
        self.msg = None

    @classmethod
    def key2id(cls, key):
        '''Converts a key to an id'''
        splits = key.split('_')
        return (splits[2], splits[1])

    @classmethod
    def _generate_subkey(cls, job_id, api_key=None):
        if job_id.startswith('request'):
            # Is is really the full key, so just return the subportion
            return job_id.partition('_')[2]
        if job_id is None or api_key is None:
            return None
        return '_'.join([api_key, job_id])

    def _set_keyname(self):
        return '%s:%s' % (super(NeonApiRequest, self)._set_keyname(),
                          self.api_key)

    @classmethod
    def _baseclass_name(cls):
        # For backwards compatibility, we don't use the classname
        return 'request'

    @classmethod
    def _create(cls, key, obj_dict):
        '''Create the object.

        Needed for backwards compatibility for old style data that
        doesn't include the classname. Instead, request_type holds
        which class to create.
        '''
        if obj_dict:
            if not '_type' in obj_dict or not '_data' in obj_dict:
                # Old style object, so adjust the object dictionary
                typemap = {
                    'brightcove' : BrightcoveApiRequest,
                    'ooyala' : OoyalaApiRequest,
                    'youtube' : YoutubeApiRequest,
                    'neon' : NeonApiRequest,
                    None : NeonApiRequest
                    }
                obj_dict = {
                    '_type': typemap[obj_dict['request_type']].__name__,
                    '_data': copy.deepcopy(obj_dict)
                    }
            obj = super(NeonApiRequest, cls)._create(key, obj_dict)

            try:
                obj.publish_date = datetime.datetime.utcfromtimestamp(
                    obj.publish_date / 1000.)
                obj.publish_date = obj.publish_date.isoformat()
            except ValueError:
                pass
            except TypeError:
                pass
            return obj

    def get_default_thumbnail_type(self):
        '''Return the thumbnail type that should be used for a default 
        thumbnail in the request.
        '''
        return ThumbnailType.DEFAULT
  
    def set_api_method(self, method, param):
        ''' 'set api method and params ''' 
        
        self.api_method = method
        self.api_param  = param

        #TODO:validate supported methods

    @classmethod
    def get(cls, job_id, api_key, log_missing=True, callback=None):
        ''' get instance '''
        return super(NeonApiRequest, cls).get(
            cls._generate_subkey(job_id, api_key),
            log_missing=log_missing,
            callback=callback)

    @classmethod
    def get_many(cls, keys, log_missing=True, callback=None):
        '''Returns the list of objects from a list of keys.

        Each key must be a tuple of (job_id, api_key)
        '''
        return super(NeonApiRequest, cls).get_many(
            [cls._generate_subkey(*k) for k in keys],
            log_missing=log_missing,
            callback=callback)

    @classmethod
    def get_all(cls):
        raise NotImplementedError()

    @classmethod
    def modify(cls, job_id, api_key, func, create_missing=False, 
               callback=None):
        return super(NeonApiRequest, cls).modify(
            cls._generate_subkey(job_id, api_key),
            func,
            create_missing=create_missing,
            callback=callback)

    @classmethod
    def modify_many(cls, keys, func, create_missing=False, callback=None):
        '''Modify many keys.

        Each key must be a tuple of (job_id, api_key)
        '''
        return super(NeonApiRequest, cls).modify_many(
            [cls._generate_subkey(*k) for k in keys],
            func,
            create_missing=create_missing,
            callback=callback)

    @classmethod
    def delete(cls, job_id, api_key, callback=None):
        return super(NeonApiRequest, cls).delete(
            cls._generate_subkey(job_id, api_key),
            callback=callback)

    @classmethod
    def delete_many(cls, keys, callback=None):
        return super(NeonApiRequest, cls).delete_many(
            [cls._generate_subkey(job_id, api_key) for 
             job_id, api_key in keys],
            callback=callback)
    
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def save_default_thumbnail(self, cdn_metadata=None):
        '''Save the default thumbnail by attaching it to a video. The video
        metadata for this request must be in the database already.

        Inputs:
        cdn_metadata - If known, the metadata to save to the cdn.
                       Otherwise it will be looked up.
        '''
        try:
            thumb_url = self.default_thumbnail
        except AttributeError:
            thumb_url = None

        if not thumb_url:
            # No default thumb to upload
            return

        thumb_type = self.get_default_thumbnail_type()

        # Check to see if there is already a thumbnail that the system
        # knows about (and thus was already uploaded)
        
        video = yield tornado.gen.Task(
            VideoMetadata.get,
            InternalVideoID.generate(self.api_key,
                                     self.video_id))
        if video is None:
            msg = ('VideoMetadata for job %s is missing. '
                   'Cannot add thumbnail' % self.job_id)
            _log.error(msg)
            raise DBStateError(msg)

        known_thumbs = yield tornado.gen.Task(
            ThumbnailMetadata.get_many,
            video.thumbnail_ids)
        min_rank = 1
        for thumb in known_thumbs:
            if thumb.type == thumb_type:
                if thumb_url in thumb.urls:
                    # The exact thumbnail is already there
                    return
            
                if thumb.rank < min_rank:
                    min_rank = thumb.rank
        cur_rank = min_rank - 1

        # Upload the new thumbnail
        meta = ThumbnailMetadata(
            None,
            ttype=thumb_type,
            rank=cur_rank,
            external_id=self.external_thumbnail_id)
        thumb = yield video.download_and_add_thumbnail(meta,
                                               thumb_url,
                                               cdn_metadata,
                                               save_objects=True,
                                               async=True)
        raise tornado.gen.Return(thumb) 
        # Push a thumbnail serving directive to Kinesis so that it can
        # be served quickly.

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def send_callback(self, send_kwargs=None):
        '''Sends the callback to the customer if necessary.

        Inputs:
        send_kwargs - Keyword arguments to utils.http.send_request for when
                      sending the callback
        '''
        if not options.send_callbacks:
            return
        new_callback_state = CallbackState.NOT_SENT
        response = None
        if self.callback_url:
            # Check the callback url format
            parsed = urlparse.urlsplit(self.callback_url)
            if parsed.scheme not in ('http', 'https'):
                _log.error_n('Invalid callback url job %s acct %s: %s'
                             % (self.job_id, self.api_key, self.callback_url))
                statemon.state.increment('invalid_callback_url')
                new_callback_state = CallbackState.ERROR
            else:

                # Build the response
                response = VideoCallbackResponse.create_from_dict(
                    self.response)
                internal_vid = InternalVideoID.generate(self.api_key,
                                                        self.video_id)
                vstatus = yield tornado.gen.Task(VideoStatus.get, internal_vid)
                response.experiment_state = vstatus.experiment_state
                response.winner_thumbnail = vstatus.winner_tid
                response.processing_state = self.state
                response.job_id = self.job_id
                response.video_id = self.video_id
            
                # Send the callback
                self.response = response.to_dict()
                send_kwargs = send_kwargs or {}
                cb_request = tornado.httpclient.HTTPRequest(
                    url=self.callback_url,
                    method='PUT',
                    headers={'content-type' : 'application/json'},
                    body=response.to_json(),
                    request_timeout=20.0,
                    connect_timeout=10.0)
                cb_response = yield utils.http.send_request(
                    cb_request,
                    no_retry_codes=[405],
                    async=True,
                    **send_kwargs)
                if cb_response.error:
                    # Now try a POST for backwards compatibility
                    cb_request.method='POST'
                    cb_response = yield utils.http.send_request(cb_request,
                                                                async=True,
                                                                **send_kwargs)
                    if cb_response.error:
                        statemon.state.define_and_increment(
                            'callback_error.%s' % self.api_key)
                                                            
                        statemon.state.increment('callback_error')
                        _log.warn('Error when sending callback to %s for '
                                  'video %s: %s' %
                                  (self.callback_url, self.video_id,
                                   cb_response.error))
                        new_callback_state = CallbackState.ERROR
                    else:
                       statemon.state.increment('sucessful_callbacks')
                       new_callback_state = CallbackState.SUCESS 
                else:
                    statemon.state.increment('sucessful_callbacks')
                    new_callback_state = CallbackState.SUCESS

            # Modify the database state
            def _mod_obj(x):
                x.callback_state = new_callback_state
                if response:
                    x.response = response.to_dict()
            yield tornado.gen.Task(self.modify, self.job_id, self.api_key,
                                   _mod_obj)

class BrightcoveApiRequest(NeonApiRequest):
    '''
    Brightcove API Request class
    '''
    def __init__(self, job_id, api_key=None, vid=None, title=None, url=None,
                 rtoken=None, wtoken=None, pid=None, http_callback=None,
                 i_id=None, default_thumbnail=None):
        super(BrightcoveApiRequest,self).__init__(
            job_id, api_key, vid, title, url,
            request_type='brightcove',
            http_callback=http_callback,
            default_thumbnail=default_thumbnail)
        self.read_token = rtoken
        self.write_token = wtoken
        self.publisher_id = pid
        self.integration_id = i_id 
        self.autosync = False
     
    def get_default_thumbnail_type(self):
        '''Return the thumbnail type that should be used for a default 
        thumbnail in the request.
        '''
        return ThumbnailType.BRIGHTCOVE

class OoyalaApiRequest(NeonApiRequest):
    '''
    Ooyala API Request class
    '''
    def __init__(self, job_id, api_key=None, i_id=None, vid=None, title=None,
                 url=None, oo_api_key=None, oo_secret_key=None,
                 http_callback=None, default_thumbnail=None):
        super(OoyalaApiRequest, self).__init__(
            job_id, api_key, vid, title, url,
            request_type='ooyala',
            http_callback=http_callback,
            default_thumbnail=default_thumbnail)
        self.oo_api_key = oo_api_key
        self.oo_secret_key = oo_secret_key
        self.integration_id = i_id 
        self.autosync = False

    def get_default_thumbnail_type(self):
        '''Return the thumbnail type that should be used for a default 
        thumbnail in the request.
        '''
        return ThumbnailType.OOYALA

class YoutubeApiRequest(NeonApiRequest):
    '''
    Youtube API Request class
    '''
    def __init__(self, job_id, api_key=None, vid=None, title=None, url=None,
                 access_token=None, refresh_token=None, expiry=None,
                 http_callback=None, default_thumbnail=None):
        super(YoutubeApiRequest,self).__init__(
            job_id, api_key, vid, title, url,
            request_type='youtube',
            http_callback=http_callback,
            default_thumbnail=default_thumbnail)
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.integration_type = "youtube"
        self.previous_thumbnail = None # TODO(Sunil): Remove this
        self.expiry = expiry

    def get_default_thumbnail_type(self):
        '''Return the thumbnail type that should be used for a default 
        thumbnail in the request.
        '''
        return ThumbnailType.YOUTUBE

###############################################################################
## Thumbnail store T_URL => TID => Metadata
############################################################################### 

class ThumbnailID(AbstractHashGenerator):
    '''
    Static class to generate thumbnail id

    _input: String or Image stream. 

    Thumbnail ID is: <internal_video_id>_<md5 MD5 hash of image data>
    '''
    VALID_REGEX = '%s_[0-9A-Za-z]+' % InternalVideoID.VALID_INTERNAL_REGEX

    @staticmethod
    def generate(_input, internal_video_id):
        return '%s_%s' % (internal_video_id, ThumbnailMD5.generate(_input))

    @classmethod
    def is_valid_key(cls, key):
        return len(key.split('_')) == 3

class ThumbnailMD5(AbstractHashGenerator):
    '''Static class to generate the thumbnail md5.

    _input: String or Image stream.
    '''
    salt = 'Thumbn@il'
    
    @staticmethod
    def generate_from_string(_input):
        ''' generate hash from string '''
        _input = ThumbnailMD5.salt + str(_input)
        return AbstractHashGenerator._api_hash_function(_input)

    @staticmethod
    def generate_from_image(imstream):
        ''' generate hash from image '''

        filestream = StringIO()
        imstream.save(filestream,'jpeg')
        filestream.seek(0)
        return ThumbnailMD5.generate_from_string(filestream.buf)

    @staticmethod
    def generate(_input):
        ''' generate hash method ''' 
        if isinstance(_input, basestring):
            return ThumbnailMD5.generate_from_string(_input)
        else:
            return ThumbnailMD5.generate_from_image(_input)


class ThumbnailServingURLs(NamespacedStoredObject):
    '''
    Keeps track of the URLs to serve for each thumbnail id.

    Specifically, maps:

    thumbnail_id -> { (width, height) -> url }

    or, instead of a full url map, there can be a base_url and a list of sizes.
    In that case, the full url would be generated by 
    <base_url>/FNAME_FORMAT % (thumbnail_id, width, height)
    '''    
    FNAME_FORMAT = "neontn%s_w%s_h%s.jpg"
    FNAME_REGEX = ('neontn(%s)_w([0-9]+)_h([0-9]+)\.jpg' % 
                   ThumbnailID.VALID_REGEX)

    def __init__(self, thumbnail_id, size_map=None, base_url=None, sizes=None):
        super(ThumbnailServingURLs, self).__init__(thumbnail_id)
        self.size_map = size_map or {}
        
        self.base_url = base_url
        self.sizes = sizes or set([]) # List of (width, height)

    def __eq__(self, other):
        '''Sets can't do cmp, so we need to overright so that == and != works.
        '''
        if ((other is None) or 
            (type(other) != type(self)) or 
            (self.__dict__.keys() != other.__dict__.keys())):
            return False
        for k, v in self.__dict__.iteritems():
            if v != other.__dict__[k]:
                return False
        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def __len__(self):
        return len(self.size_map) + len(self.sizes)
    
    @classmethod
    def _baseclass_name(cls):
        '''Returns the class name of the base class of the hierarchy.
        '''
        return ThumbnailServingURLs.__name__

    def get_thumbnail_id(self):
        '''Return the thumbnail id for this mapping.'''
        return self.get_id()

    def add_serving_url(self, url, width, height):
        '''Adds a url to serve for a given width and height.

        If there was a previous entry, it is overwritten.
        '''
        if self.base_url is not None:
            urlRe = re.compile(
                '%s/%s' % (re.escape(self.base_url),
                           ThumbnailServingURLs.FNAME_REGEX))
            if urlRe.match(url):
                self.sizes.add((width, height))
                return
            else:
                # TODO(mdesnoyer): once the db is cleaned, make this
                # raise a ValueError
                _log.warn_n('url %s does not conform to base %s' %
                            (url, self.base_url))
        self.size_map[(width, height)] = str(url)

    def get_serving_url(self, width, height):
        '''Get the serving url for a given width and height.

        Raises a KeyError if there isn't one.
        '''
        if (width, height) in self.sizes:
            return (self.base_url + '/' + ThumbnailServingURLs.FNAME_FORMAT %
                    (self.get_thumbnail_id(), width, height))
        return self.size_map[(width, height)]

    def get_serving_url_count(self):
        '''Return the number of serving urls in this object.'''
        return len(self.size_map) + len(self.sizes)

    def is_valid_size(self, width, height):
        '''Returns true if there is a url for this size image.'''
        sz = (width, height)
        return sz in self.sizes or sz in self.size_map

    def __iter__(self):
        '''Iterator of size, url pairs.'''
        return itertools.chain(
            self.size_map.iteritems(),
            ((k, self.get_serving_url(*k)) for k in self.sizes))

    @staticmethod
    def create_filename(tid, width, height):
        '''Creates a filename for a given thumbnail id at a specific size.'''
        return ThumbnailServingURLs.FNAME_FORMAT % (tid, width, height)

    def to_dict(self):
        new_dict = {
            '_type': self.__class__.__name__,
            '_data': copy.copy(self.__dict__)
            }
        new_dict['_data']['size_map'] = self.size_map.items()
        new_dict['_data']['sizes'] = list(self.sizes)
        return new_dict

    @classmethod
    def _create(cls, key, obj_dict):
        obj = super(ThumbnailServingURLs, cls)._create(key, obj_dict)
        if obj:
            # Convert the sizes into tuples and a set
            obj.sizes = set((tuple(x) for x in obj.sizes))
            
            # Load in the url entries into the object
            size_map = obj.size_map
            obj.size_map = {}
            # Find the base url to save that way
            bases = set((os.path.dirname(x[1]) for x in size_map))
            if len(bases) == 1 and obj.base_url is None:
                obj.base_url = bases.pop()
            for k, v in size_map:
                width, height = k
                obj.add_serving_url(v, width, height)
            return obj

        
class ThumbnailURLMapper(NamespacedStoredObject):
    '''
    Schema to map thumbnail url to thumbnail ID. 

    _input - thumbnail url ( key ) , tid - string/image, converted to thumbnail ID
            if imdata given, then generate tid 
    
    THUMBNAIL_URL => (tid)
    
    # NOTE: This has been deprecated and hence not being updated to be a stored
    object
    TODO: Remove this object. It is no longer needed
    '''
    
    def __init__(self, thumbnail_url, tid, imdata=None):
        self.key = thumbnail_url
        if not imdata:
            self.value = tid
        else:
            #TODO: Is this imdata really needed ? 
            raise #self.value = ThumbnailID.generate(imdata) 

    def to_json(self):
        # Actually not json because we are only storing the value
        return str(self.value)

    @classmethod
    def _baseclass_name(cls):
        return ThumbnailURLMapper.__name__

    @classmethod
    def get_id(cls, key, callback=None):
        ''' get thumbnail id '''
        db_connection = DBConnection.get(cls)
        if callback:
            db_connection.conn.get(key, callback)
        else:
            return db_connection.blocking_conn.get(key)

    @classmethod
    def _erase_all_data(cls):
        ''' del all data'''
        db_connection = DBConnection.get(cls)
        db_connection.clear_db()

class ThumbnailMetadata(StoredObject):
    '''
    Class schema for Thumbnail information.

    Keyed by thumbnail id
    '''
    def __init__(self, tid, internal_vid=None, urls=None, created=None,
                 width=None, height=None, ttype=None,
                 model_score=None, model_version=None, enabled=True,
                 chosen=False, rank=None, refid=None, phash=None,
                 serving_frac=None, frameno=None, filtered=None, ctr=None,
                 external_id=None):
        super(ThumbnailMetadata,self).__init__(tid)
        self.video_id = internal_vid #api_key + platform video id
        self.external_id = external_id # External id if appropriate
        self.urls = urls or []  # List of all urls associated with single image
        self.created_time = created or datetime.datetime.now().strftime(
            "%Y-%m-%d %H:%M:%S")# Timestamp when thumbnail was created 
        self.enabled = enabled #boolen, indicates if this thumbnail can be displayed/ tested with 
        self.chosen = chosen #boolean, indicates this thumbnail is chosen by the user as the primary one
        self.width = width
        self.height = height
        self.type = ttype #neon1../ brightcove / youtube
        self.rank = 0 if not rank else rank  #int 
        self.model_score = model_score #string
        self.model_version = model_version #string
        self.frameno = frameno #int Frame Number
        self.filtered = filtered # String describing how it was filtered
        #TODO: remove refid. It's not necessary
        self.refid = refid #If referenceID exists *in case of a brightcove thumbnail
        self.phash = phash # Perceptual hash of the image. None if unknown
        

        # DEPRECATED: Use the ThumbnailStatus table instead
        self.serving_frac = serving_frac 

        # DEPRECATED: Use the ThumbnailStatus table instead
        self.ctr = ctr
        
        # NOTE: If you add more fields here, modify the merge code in
        # video_processor/client, Add unit test to check this

    def _set_keyname(self):
        '''Key the set by the video id'''
        return 'objset:%s' % self.key.rpartition('_')[0]

    @classmethod
    def is_valid_key(cls, key):
        return ThumbnailID.is_valid_key(key)

    def update_phash(self, image):
        '''Update the phash from a PIL image.'''
        self.phash = cv.imhash_index.hash_pil_image(
            image,
            hash_type=options.hash_type,
            hash_size=options.hash_size)

    def get_account_id(self):
        ''' get the internal account id. aka api key '''
        return self.key.split('_')[0]
    
    def get_metadata(self):
        ''' get a dictionary of the thumbnail metadata

        This function is deprecated and is kept only for backwards compatibility
        '''
        return self.to_dict()
    
    def to_dict_for_video_response(self):
        ''' to dict for video response object
            replace key to thumbnail_id 
        '''
        new_dict = copy.copy(self.__dict__)
        new_dict["thumbnail_id"] = new_dict.pop("key")
        return new_dict

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def add_image_data(self, image, video_info=None, cdn_metadata=None):
        '''Incorporates image data to the ThumbnailMetadata object.

        Also uploads the image to the CDNs and S3.

        Inputs:
        image - A PIL image
        cdn_metadata - A list CDNHostingMetadata objects for how to upload the
                       images. If this is None, it is looked up, which is 
                       slow. If a source_crop is requested, the image is also
                       cropped here.
        
        '''        
        image = PILImageUtils.convert_to_rgb(image)
        # Update the image metadata
        self.width = image.size[0]
        self.height = image.size[1]
        self.update_phash(image)

        # Convert the image to JPG
        fmt = 'jpeg'
        filestream = StringIO()
        image.save(filestream, fmt, quality=90) 
        filestream.seek(0)
        imgdata = filestream.read()

        self.key = ThumbnailID.generate(imgdata, self.video_id)

        # Host the primary copy of the image 
        primary_hoster = cmsdb.cdnhosting.CDNHosting.create(
            PrimaryNeonHostingMetadata())
        s3_url_list = yield primary_hoster.upload(image, self.key, async=True)
        
        # TODO (Sunil):  Add redirect for the image

        # Add the primary image to Thumbmetadata
        s3_url = None
        if len(s3_url_list) == 1:
            s3_url = s3_url_list[0][0]
            self.urls.insert(0, s3_url)

        # Host the image on the CDN
        if cdn_metadata is None:
            # Lookup the cdn metadata
            if video_info is None: 
                video_info = yield tornado.gen.Task(VideoMetadata.get,
                                                    self.video_id)

            cdn_key = CDNHostingMetadataList.create_key(
                video_info.get_account_id(), video_info.integration_id)
            cdn_metadata = yield tornado.gen.Task(CDNHostingMetadataList.get,
                                                  cdn_key)
            if cdn_metadata is None:
                # Default to hosting on the Neon CDN if we don't know about it
                cdn_metadata = [NeonCDNHostingMetadata()]
            
        hosters = [cmsdb.cdnhosting.CDNHosting.create(x) for x in cdn_metadata]
        yield [x.upload(image, self.key, s3_url, async=True) for x in hosters]

    @classmethod
    def get_video_id(cls, tid, callback=None):
        '''Given a thumbnail id, retrieves the internal video id 
            asscociated with thumbnail
        '''

        if callback:
            def handle_obj(obj):
                if obj:
                    callback(obj.video_id)
                else:
                    callback(None)
            cls.get(tid, callback=handle_obj)
        else:
            obj = cls.get(tid)
            if obj:
                return obj.video_id
            else:
                return None

    @staticmethod
    def enable_thumbnail(thumbnails, new_tid):
        ''' enable thumb in a list of thumbnails given a new thumb id '''
        new_thumb_obj = None; old_thumb_obj = None
        for thumb in thumbnails:
            #set new tid as chosen
            if thumb.key == new_tid: 
                thumb.chosen = True
                new_thumb_obj = thumb 
            else:
                #set chosen=False for old tid
                if thumb.chosen == True:
                    thumb.chosen = False 
                    old_thumb_obj = thumb 

        #return only the modified thumbnail objs
        return new_thumb_obj, old_thumb_obj 

class ThumbnailStatus(DefaultedStoredObject):
    '''Holds the current status of the thumbnail in the wild.'''

    def __init__(self, thumbnail_id, serving_frac=None, ctr=None,
                 imp=None, conv=None, serving_history=None):
        super(ThumbnailStatus, self).__init__(thumbnail_id)

        # The fraction of traffic this thumbnail will get
        self.serving_frac = serving_frac

        # List of (time, serving_frac) tuples
        self.serving_history = serving_history or []

        # The current click through rate for this thumbnail
        self.ctr = ctr

        # The number of impressions this thumbnail received
        self.imp = imp

        # The number of conversions this thumbnail received
        self.conv = conv

    def set_serving_frac(self, serving_frac):
        '''Sets the serving fraction. Returns true if it is new.'''
        if (self.serving_frac is None or 
            abs(serving_frac - self.serving_frac) > 1e-3):
            self.serving_frac = serving_frac
            self.serving_history.append(
                (datetime.datetime.utcnow().isoformat(),
                 serving_frac))
            return True
        return False
            

    @classmethod
    def _baseclass_name(cls):
        '''Returns the class name of the base class of the hierarchy.
        '''
        return ThumbnailStatus.__name__

class VideoMetadata(StoredObject):
    '''
    Schema for metadata associated with video which gets stored
    when the video is processed

    Contains list of Thumbnail IDs associated with the video
    '''

    '''  Keyed by API_KEY + VID (internal video id) '''
    
    def __init__(self, video_id, tids=None, request_id=None, video_url=None,
                 duration=None, vid_valence=None, model_version=None,
                 i_id=None, frame_size=None, testing_enabled=True,
                 experiment_state=ExperimentState.UNKNOWN,
                 experiment_value_remaining=None,
                 serving_enabled=True, custom_data=None,
                 publish_date=None):
        super(VideoMetadata, self).__init__(video_id) 
        self.thumbnail_ids = tids or []
        self.url = video_url 
        self.duration = duration # in seconds
        self.video_valence = vid_valence 
        self.model_version = model_version
        self.job_id = request_id
        self.integration_id = i_id
        self.frame_size = frame_size #(w,h)
        # Is A/B testing enabled for this video?
        self.testing_enabled = testing_enabled

        # DEPRECATED. Use VideoStatus table instead
        self.experiment_state = \
          experiment_state if testing_enabled else ExperimentState.DISABLED
        self.experiment_value_remaining = experiment_value_remaining

        # Will thumbnails for this video be served by our system?
        self.serving_enabled = serving_enabled 
        
        # Serving URL (ISP redirect URL) 
        # NOTE: This is set by mastermind by calling get_serving_url() method
        # after the request state has been changed to SERVING
        self.serving_url = None

        # A dictionary of extra metadata
        self.custom_data = custom_data or {}

        # The time the video was published in ISO 8601 format
        self.publish_date = publish_date

    def _set_keyname(self):
        '''Key by the account id'''
        return 'objset:%s' % self.get_account_id()

    @classmethod
    def is_valid_key(cls, key):
        return len(key.split('_')) == 2

    def get_id(self):
        ''' get internal video id '''
        return self.key

    def get_account_id(self):
        ''' get the internal account id. aka api key '''
        return self.key.split('_')[0]

    def get_frame_size(self):
        ''' framesize of the video '''
        if self.__dict__.has_key('frame_size'):
            return self.frame_size

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_winner_tid(self):
        '''
        Get the TID that won the A/B test
        '''
        video_status = yield tornado.gen.Task(VideoStatus.get, self.key)
        raise tornado.gen.Return(video_status.winner_tid)

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def add_thumbnail(self, thumb, image, cdn_metadata=None,
                      save_objects=False, video=None):
        '''Add thumbnail to the video.

        Saves the thumbnail object, and the video object if
        save_object is true.

        Inputs:
        @thumb: ThumbnailMetadata object. Should be incomplete
                because image based data will be added along with 
                information about the video. The object will be updated with
                the proper key and other information
        @image: PIL Image
        @cdn_metadata: A list of CDNHostingMetadata objects for how to upload
                       the images. If this is None, it is looked up, which is 
                       slow.
        @save_objects: If true, the database is updated. Otherwise, 
                       just this object is updated along with the thumbnail
                       object.
        '''
        yield thumb.add_image_data(image, self, cdn_metadata, 
                                   async=True)

        # TODO(mdesnoyer): Use a transaction to make sure the changes
        # to the two objects are atomic. For now, put in the thumbnail
        # data and then update the video metadata.
        if save_objects:
            sucess = yield tornado.gen.Task(thumb.save)
            if not sucess:
                raise IOError("Could not save thumbnail")

            updated_video = yield tornado.gen.Task(
                VideoMetadata.modify,
                self.key,
                lambda x: x.thumbnail_ids.append(thumb.key))
            if updated_video is None:
                # It wasn't in the database, so save this object
                self.thumbnail_ids.append(thumb.key)
                sucess = yield tornado.gen.Task(self.save)
                if not sucess:
                    raise IOError("Could not save video data")
            else:
                self.__dict__ = updated_video.__dict__
        else:
            self.thumbnail_ids.append(thumb.key)

        raise tornado.gen.Return(thumb)

    
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def download_image_from_url(self, image_url): 
        try:
            image = yield utils.imageutils.PILImageUtils.download_image(image_url,
                    async=True)
        except IOError, e:
            msg = "IOError while downloading image %s: %s" % (
                image_url, e)
            _log.warn(msg)
            raise ThumbDownloadError(msg)
        except tornado.httpclient.HTTPError as e:
            msg = "HTTP Error while dowloading image %s: %s" % (
                image_url, e)
            _log.warn(msg)
            raise ThumbDownloadError(msg)

        raise tornado.gen.Return(image)

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def download_and_add_thumbnail(self, 
                                   thumb=None, 
                                   image_url=None,
                                   cdn_metadata=None,
                                   image=None, 
                                   external_thumbnail_id=None, 
                                   save_objects=False):
        '''
        Download the image and add it to this video metadata

        Inputs:
        @thumb: ThumbnailMetadata object. Should be incomplete
                because image based data will be added along with 
                information about the video. The object will be updated with
                the proper key and other information
        @image_url: url of the image to download
        @cdn_metadata: A list CDNHostingMetadata objects for how to upload the
                       images. If this is None, it is looked up, which is slow.
        @save_objects: If true, the database is updated. Otherwise, 
                       just this object is updated along with the thumbnail
                       object.
        '''
        if image is None: 
            image = yield self.download_image_from_url(image_url, async=True) 
        if thumb is None: 
            thumb = ThumbnailMetadata(None,
                          ttype=ThumbnailType.DEFAULT,
                          external_id=external_thumbnail_id)
        thumb.urls.append(image_url)
        thumb = yield self.add_thumbnail(thumb, image, cdn_metadata,
                                         save_objects, async=True)
        raise tornado.gen.Return(thumb)

    @classmethod
    def get_video_request(cls, internal_video_id, callback=None):
        ''' get video request data '''
        if not callback:
            vm = cls.get(internal_video_id)
            if vm:
                api_key = vm.key.split('_')[0]
                return NeonApiRequest.get(vm.job_id, api_key)
            else:
                return None
        else:
            raise AttributeError("Callbacks not allowed")

    @classmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_video_requests(cls, i_vids):
        '''
        Get video request objs given video_ids
        '''
        vms = yield tornado.gen.Task(VideoMetadata.get_many, i_vids)
        retval = [None for x in vms]
        request_keys = []
        request_idx = []
        cur_idx = 0
        for vm in vms:
            rkey = None
            if vm:
                api_key = vm.key.split('_')[0]
                rkey = (vm.job_id, api_key)
                request_keys.append(rkey)
                request_idx.append(cur_idx)
            cur_idx += 1
          
        requests = yield tornado.gen.Task(NeonApiRequest.get_many, request_keys)  
        for api_request, idx in zip(requests, request_idx):
            retval[idx] = api_request
        raise tornado.gen.Return(retval)

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_serving_url(self, staging=False, save=True):
        '''
        Get the serving URL of the video. If self.serving_url is not
        set, fetch the neon publisher id (TAI) and save the video object 
        with the serving_url set
        
        NOTE: any call to this function will return a valid serving url. 
        multiple calls to this function may or may not return the same URL 

        @save : If true, the url is saved to the database
        '''
        subdomain_index = random.randrange(1, 4)
        platform_vid = InternalVideoID.to_external(self.get_id())
        serving_format = "http://i%s.neon-images.com/v1/client/%s/neonvid_%s.jpg"

        if self.serving_url and not staging:
            # Return the saved serving_url
            raise tornado.gen.Return(self.serving_url)

        nu = yield tornado.gen.Task(
                NeonUserAccount.get, self.get_account_id())
        pub_id = nu.staging_tracker_account_id if staging else \
          nu.tracker_account_id
        serving_url = serving_format % (subdomain_index, pub_id,
                                                platform_vid)

        if not staging:

            def _update_serving_url(vobj):
                vobj.serving_url = self.serving_url
            if save:
                # Keep information about the serving url around
                self.serving_url = serving_url
                yield tornado.gen.Task(VideoMetadata.modify, self.key,
                                       _update_serving_url)
        raise tornado.gen.Return(serving_url)
        
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def image_available_in_isp(self):
        try:
            neon_user_account = yield tornado.gen.Task(NeonUserAccount.get,
                                                       self.get_account_id())
            if neon_user_account is None:
                msg = ('Cannot find the neon user account %s for video %s. '
                       'This should never happen' % 
                       (self.get_account_id(), self.key))
                _log.error(msg)
                raise DBStateError(msg)
                
            request = tornado.httpclient.HTTPRequest(
                'http://%s/v1/video?%s' % (
                    options.isp_host,
                    urllib.urlencode({
                        'video_id' : InternalVideoID.to_external(self.key),
                        'publisher_id' : neon_user_account.tracker_account_id
                        })),
                follow_redirects=True)
            res = yield utils.http.send_request(request, async=True)

            if res.code != 200:
                if res.code != 204:
                    _log.error('Unexpected response looking up video %s on '
                               'isp: %s' % (self.key, res))
                else:
                    _log.debug('Image not available in ISP yet.')
                raise tornado.gen.Return(False)
                
            raise tornado.gen.Return(True)
        except tornado.httpclient.HTTPError as e: 
            _log.error('Unexpected response looking up video %s on '
                       'isp: %s' % (self.key, e))

        raise tornado.gen.Return(False)
    

class VideoStatus(DefaultedStoredObject):
    '''Stores the status of the video in the wild for often changing entries.

    '''
    def __init__(self, video_id, experiment_state=ExperimentState.UNKNOWN,
                 winner_tid=None,
                 experiment_value_remaining=None,
                 state_history=None):
        super(VideoStatus, self).__init__(video_id)

        # State of the experiment
        self.experiment_state = experiment_state

        # Thumbnail id of the winner thumbnail
        self.winner_tid = winner_tid

        # For the multi-armed bandit strategy, the value remaining
        # from the monte carlo analysis.
        self.experiment_value_remaining = experiment_value_remaining

        # [(time, new_state)]
        self.state_history = state_history or []

    def set_experiment_state(self, value):
        if value != self.experiment_state:
            self.experiment_state = value
            self.state_history.append(
                (datetime.datetime.utcnow().isoformat(),
                 value))

    @classmethod
    def _baseclass_name(cls):
        '''Returns the class name of the base class of the hierarchy.
        '''
        return VideoStatus.__name__

    

class AbstractJsonResponse(object):
    
    def to_dict(self):
        return self.__dict__

    def to_json(self):
        return json.dumps(self, default=lambda o: o.__dict__)

    @classmethod
    def create_from_dict(cls, d):
        '''Create the object from a dictionary.'''
        retval = cls()
        if d is not None:
            for k, v in d.iteritems():
                retval.__dict__[k] = v
        retval.timestamp = str(time.time())

        return retval

class VideoResponse(AbstractJsonResponse):
    ''' VideoResponse object that contains list of thumbs for a video 
        # NOTE: this obj is only used to format in to a json response 
    '''
    def __init__(self, vid, job_id, status, i_type, i_id, title, duration,
            pub_date, cur_tid, thumbs, abtest=True, winner_thumbnail=None,
            serving_url=None):
        self.video_id = vid # External video id
        self.job_id = job_id 
        self.status = status
        self.integration_type = i_type
        self.integration_id = i_id
        self.title = title
        self.duration = duration
        self.publish_date = pub_date
        self.current_thumbnail = cur_tid
        #list of ThumbnailMetdata dicts 
        self.thumbnails = thumbs if thumbs else [] 
        self.abtest = abtest
        self.winner_thumbnail = winner_thumbnail
        self.serving_url = serving_url

class VideoCallbackResponse(AbstractJsonResponse):
    def __init__(self, jid=None, vid=None, fnos=None, thumbs=None,
                 s_url=None, err=None,
                 processing_state=RequestState.UNKNOWN,
                 experiment_state=ExperimentState.UNKNOWN,
                 winner_thumbnail=None):
        self.job_id = jid
        self.video_id = vid
        self.framenos = fnos if fnos is not None else []
        self.thumbnails = thumbs if thumbs is not None else []
        self.serving_url = s_url
        self.error = err
        self.timestamp = str(time.time())
        self.processing_state = processing_state
        self.experiment_state = experiment_state
        self.winner_thumbnail = winner_thumbnail
    
if __name__ == '__main__':
    # If you call this module you will get a command line that talks
    # to the server. nifty eh?
    utils.neon.InitNeon()
    code.interact(local=locals())
