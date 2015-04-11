'''
Utilities to deal with redis in tests

Author: Mark Desnoyer (desnoyer@neon-lab.com)
Copyright 2013 Neon Labs
'''

import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

import logging
from . import net
import random
import re
import signal
import subprocess
from cmsdb import neondata
import tempfile
import utils.ps
from utils.options import define, options

_log = logging.getLogger(__name__)

class RedisServer:
    '''A redis serving running in its own process.

    To use, create the object and then start() and stop() it. e.g.

    def setUp(self):
      self.redis = RedisServer(port)
      self.redis.start()

    def tearDown(self):
      self.redis.stop()

    def test_something(self):
      connect_to_redis(self.redis.port)
    '''
    
    def __init__(self, port=None):
        self.port = port
        if self.port is None:
            self.port = net.find_free_port()

    def start(self, clear_singleton=True):
        ''' Start on a random port and set cmsdb.neondata.dbPort '''

        # Clear the singleton instance
        # This is required so that we can use a new connection(port) 
        if clear_singleton:
            neondata.DBConnection.clear_singleton_instance()
            neondata.PubSubConnection.clear_singleton_instance()

        self.config_file = tempfile.NamedTemporaryFile()
        self.config_file.write('port %i\n' % self.port)
        self.config_file.write('notify-keyspace-events Kgsz$')
        self.config_file.flush()

        _log.info('Redis server started on port %i' % self.port)

        self.proc = subprocess.Popen([
            '/usr/bin/env', 'redis-server',
            self.config_file.name],
            stdout=subprocess.PIPE)

        upRe = re.compile('The server is now ready to accept connections on '
                          'port')
        video_db_log = []
        while self.proc.poll() is None:
            line = self.proc.stdout.readline()
            video_db_log.append(line)
            if upRe.search(line):
                break

        if self.proc.poll() is not None:
            raise Exception('Error starting video db. Log:\n%s' %
                            '\n'.join(video_db_log))

        # Set the port for the most common place we use redis. If it's
        # not being used in the test, it won't hurt anything.
        self.old_port = options.get('cmsdb.neondata.dbPort')
        options._set('cmsdb.neondata.dbPort', self.port)
        

    def stop(self, clear_singleton=True):
        ''' stop redis instance '''
        # Clear the singleton instance. This is required so that the
        # next test can use a new connection(port)
        if clear_singleton:
            neondata.PubSubConnection.clear_singleton_instance()
        neondata.DBConnection.clear_singleton_instance()

        self.config_file.close()
        options._set('cmsdb.neondata.dbPort', self.old_port)
        still_running = utils.ps.send_signal_and_wait(signal.SIGTERM,
                                                      [self.proc.pid],
                                                      timeout=8)
        if still_running:
            utils.ps.send_signal_and_wait(signal.SIGKILL,
                                          [self.proc.pid],
                                          timeout=10)
        
        self.proc.wait()
        _log.info('Redis server on port %i stopped' % self.port)
