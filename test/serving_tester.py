#!/usr/bin/env python
''' This script fires up the serving system and runs end to end tests.

***WARNING*** This test does not run completely locally. It interfaces
   with some Amazon services and will thus incure some fees. Also, it
   cannot be run from multiple locations at the same time.

Copyright: 2013 Neon Labs
Author: Mark Desnoyer (desnoyer@neon-lab.com)
'''
import os.path
import sys
base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] <> base_path:
    sys.path.insert(0,base_path)

import atexit
from boto.s3.connection import S3Connection
from boto.s3.bucketlistresultset import BucketListResultSet
import clickTracker.trackserver
import copy
from datetime import datetime
import json
import logging
import mastermind.server
import MySQLdb as sqldb
import multiprocessing
import os
import Queue
import random
import re
import signal
import SimpleHTTPServer
import SocketServer
import stats.db
import stats.stats_processor
import subprocess
import supportServices.services
from supportServices import neondata
import tempfile
import time
import tornado.httpserver
import tornado.ioloop
import tornado.web
import unittest
import urllib
import urllib2
import utils.neon
import utils.ps
from utils import statemon

from utils.options import define, options

define('stats_db', help='Name of the stats database to talk to',
       default='serving_tester')
define('stats_db_user', help='User for the stats db connection',
       default='neon')
define('stats_db_pass', help='Password for the stats db connection',
       default='neon')
define('bc_directive_port', default=7212, type=int,
       help='Port where the brightcove directives will be output')
define('fakes3root', default='/tmp/neon_s3_root', type=str,
       help='Directory that acts as the root for fakes3')

_log = logging.getLogger(__name__)

_erase_local_log_dir = multiprocessing.Event()
_activity_watcher = utils.ps.ActivityWatcher()

class TestServingSystem(unittest.TestCase):
    __test__ = False

    @classmethod
    def setUpClass(cls):
        _log.info('Starting the directive capture server on port %i' 
                  % options.bc_directive_port)
        cls._directive_cap = DirectiveCaptureProc(options.bc_directive_port)
        cls.directive_q = cls._directive_cap.q
        cls._directive_cap.start()
        cls._directive_cap.wait_until_running()

    @classmethod
    def tearDownClass(cls):
        cls._directive_cap.terminate()
        cls._directive_cap.join(30)
        if cls._directive_cap.is_alive():
            try:
                os.kill(cls._directive_cap.pid, signal.SIGKILL)
            except OSError:
                pass

    def setUp(self):
        ClearStatsDb()

        conn = sqldb.connect(user=options.stats_db_user,
                             passwd=options.stats_db_pass,
                             host='localhost',
                             db=options.stats_db)
        self.statscursor = conn.cursor()

        # Clear the log storage area
        log_path = options.get('clickTracker.trackserver.output')
        s3pathRe = re.compile('s3://([0-9a-zA-Z_\-]+)')
        s3match = s3pathRe.match(log_path)
        if s3match:
            s3conn = S3Connection()
            bucket = s3conn.get_bucket(s3match.groups()[0])
            for key in BucketListResultSet(bucket):
                key.delete()
            _erase_local_log_dir.set()
        else:
            for cur_file in os.listdir(log_path):
                os.remove(os.path.join(log_path, cur_file))

        # Empty the directives queue
        while not self.__class__.directive_q.empty():
            self.__class__.directive_q.get_nowait()
        self.directives_captured = []
        
        # Clear the video database
        #neondata._erase_all_data()


    def tearDown(self):
        pass
    
    def simulateEvents(self, data):
        '''
        Simulate a set of loads and clicks with randomized ordering

        data - [(url, n_loads, n_clicks)]
        '''
        random.seed(5951674)
        def format_get_request(vals):
            base_url = "http://localhost:%s/track?" % (
                options.get('clickTracker.trackserver.port'))
            base_url += urllib.urlencode(vals)
            return base_url

        # First generate the events
        base_event = {
            'ttype': 'flashonlyplayer',
            'id': 0,
            'page': "http://neontest",
            'cvid': 0
            }
        events = []
        for url, n_loads, n_clicks in data:
            for i in range(n_loads):
                if i < n_clicks:
                    event = copy.copy(base_event)
                    event['a'] = 'click'
                    event['img'] = url
                    events.append(event)
                event = copy.copy(base_event)
                event['a'] = 'load'
                event['imgs'] = [url, 'garbage.jpg']
                events.append(event)


        # Shuffle the events
        random.shuffle(events)

        # Now blast them off
        for event in events:
            event['ts'] = time.time()
            req = format_get_request(event)
            response = urllib2.urlopen(req)
            if response.getcode() !=200 :
                _log.debug("Tracker request not submitted")

    def waitToFinish(self):
        '''Waits until the processing is finished.'''
        # Waits until the trackserver is done
        while (_activity_watcher.is_active() or
               statemon.state.get('clickTracker.trackserver.buffer_size')>0 or
               statemon.state.get('clickTracker.trackserver.qsize') > 0):
            _activity_watcher.wait_for_idle()

        # Give the stats processor enough time to kick off
        time.sleep(options.get('stats.stats_processor.run_period') + 0.1)

        # Wait for activity to stop again
        _activity_watcher.wait_for_idle()

    def waitForMastermind(self):
        '''Waits until mastermind is finished processing.'''
        sleep_time = max(
            options.get('mastermind.server.stats_db_polling_delay'),
            options.get('mastermind.server.video_db_polling_delay')) + 0.1
        time.sleep(sleep_time)

        _activity_watcher.wait_for_idle()

    def assertDirectiveCaptured(self, directive, timeout=None):
        '''Verifies that a given directive is received.

        Inputs:
        directive - (video_id, [(thumb_id, frac)])
        timeout - (optional) How long to wait for the directive
        '''
        
        # Check the directives we already know about
        for saw_directive in self.directives_captured:
            if directivesEqual(directive, saw_directive):
                return

        if timeout is None:
            self.fail('Directive %s not found. Saw %s' % 
                      (directive, self.directives_captured)) 

        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                new_directive = self.__class__.directive_q.get(
                    True, deadline-time.time())
            except Queue.Empty:
                break
            
            self.directives_captured.append(new_directive)
            if directivesEqual(directive, new_directive):
                return

        self.fail('Directive %s not found. Saw %s' % 
                  (directive, self.directives_captured))            

    def getStats(self, thumb_id):
        '''Retrieves the statistics about a given thumb.

        Inputs:
        thumb_id - Thumbnail id

        Outputs:
        (loads, clicks)
        '''
        response = stats.db.execute(
            self.statscursor,
            '''SELECT sum(loads), sum(clicks) from hourly_events
            where thumbnail_id = %s''', (thumb_id,))
        response = self.statscursor.fetchall()
        return (response[0][0], response[0][1])
        

    #Add helper functions to add stuff to the video database
    def add_account_to_videodb(self, account_id='acct0',
                               integration_id='testintegration1',
                               n_vids=1, n_thumbs=3):
        '''Creates a basic account in the video db.

        This account has account id a_id, integration id i_id, a
        number of video ids <a_id>_vid<i> for i in 0->n-1, and thumbs
        <a_id>_vid<i>_thumb<j> for j in 0->m-1. Thumb m-1 is brighcove, while
        the rest are neon. 

        '''

        video_ids = ["vid%i" % i for i in range(n_vids)]

        # create neon user account
        nu = neondata.NeonUserAccount(account_id)
        api_key = nu.neon_api_key
        nu.save()

        # create brightcove platform account
        bp = neondata.BrightcovePlatform(account_id, integration_id,
                                         abtest=True) 
        bp.save()

        # Create Request objects  <-- not required? 
        #TODO: ImageMD5Mapper & TID generator 
        # Add fake video data in to DB
        for vid in video_ids:
            i_vid = '%s_%s' % (account_id, vid)
            bp.add_video(i_vid,"dummy_request_id")
            tids = []; thumbnail_url_mappers=[];thumbnail_id_mappers=[]  
            # fake thumbnails for videos
            for t in range(n_thumbs):
                #Note: assume last image is bcove
                ttype = "neon" if t < (n_thumbs -1) else "brightcove"
                tid = '%s_%s_thumb%i' % (account_id, vid, t) 
                url = 'http://%s.jpg' % tid 
                urls = [] ; urls.append(url)
                tdata = neondata.ThumbnailMetaData(
                    tid, urls, time.time(), 480, 360,
                    ttype, 0, 0, True, False, rank=t)
                tids.append(tid)
                
                # ID Mappers (ThumbIDMapper,ImageMD5Mapper,URLMapper)
                url_mapper = neondata.ThumbnailURLMapper(url, tid)
                id_mapper = neondata.ThumbnailIDMapper(
                    tid, i_vid, tdata.to_dict())
                thumbnail_url_mappers.append(url_mapper)
                thumbnail_id_mappers.append(id_mapper)

            vmdata = neondata.VideoMetadata(i_vid,tids,
                    "job_id","http://testvideo.mp4", 10, 0, 0, integration_id)
            retid = neondata.ThumbnailIDMapper.save_all(thumbnail_id_mappers)
            returl = neondata.ThumbnailURLMapper.save_all(
                thumbnail_url_mappers)
            if not vmdata.save() or retid or returl:
                _log.debug("Didnt save data to the DB, DB error")
            
        # Update Brightcove account with videos
        bp.save()


    #TODO: Write the actual tests
    def test_initial_directives_received(self):
        self.add_account_to_videodb('init_account0', 'init_int0', 1, 3)
        self.assertDirectiveCaptured(('init_account0_vid0',
                                      [('init_account0_vid0_thumb0', 0.85),
                                       ('init_account0_vid0_thumb1', 0.00),
                                       ('init_account0_vid0_thumb2', 0.15)]),
            timeout=5)

    def test_video_got_bad_stats(self):
        self.add_account_to_videodb('bad_stats0', 'bad_stats_int0', 1, 3)
        
        # Simulate loads and clicks to the point that the thumbnail
        # should turn off.
        self.simulateEvents([
            ('http://bad_stats0_vid0_thumb0.jpg', 8500, 100),
            ('http://bad_stats0_vid0_thumb1.jpg', 20, 1),
            ('http://bad_stats0_vid0_thumb2.jpg', 1500, 500)])

        self.waitToFinish()

        # The neon thumb (0) should be turned off now
        self.assertDirectiveCaptured(('bad_stats0_vid0',
                                      [('bad_stats0_vid0_thumb0', 0.00),
                                       ('bad_stats0_vid0_thumb1', 0.00),
                                       ('bad_stats0_vid0_thumb2', 1.00)]),
            timeout=5)

        # Check that the database got the stats correctly.
        self.assertEqual(self.getStats('bad_stats0_vid0_thumb0'),
                         (8500, 100))
        self.assertEqual(self.getStats('bad_stats0_vid0_thumb1'),
                         (20, 1))
        self.assertEqual(self.getStats('bad_stats0_vid0_thumb2'),
                         (1500, 500))

def directivesEqual(a, b):
    '''Returns true if two directives are equivalent.
    
    Directives are of the form:
    (video_id, [(thumb_id, frac)])
    '''
    if a[0] <> b[0] or len(a[1]) <> len(b[1]):
        return False

    for thumb_id, frac in a[1]:
        if ((thumb_id, frac) not in b[1] and
            [thumb_id, frac] not in b[1]):
            return False

    return True

class DirectiveCaptureProc(multiprocessing.Process):
    '''A mini little http server that captures mastermind directives.

    The directives are shoved into a multiprocess Queue after being parsed.
    '''
    def __init__(self, port):
        super(DirectiveCaptureProc, self).__init__()
        self.port = port
        self.q = multiprocessing.Queue()
        self.is_running = multiprocessing.Event()

    def wait_until_running(self):
        '''Blocks until the data is loaded.'''
        self.is_running.wait()

    def run(self):
        application = tornado.web.Application([
            (r'/directive', DirectiveCaptureHandler, dict(q=self.q))])
        server = tornado.httpserver.HTTPServer(application)
        utils.ps.register_tornado_shutdown(server)
        server.listen(self.port)
        self.is_running.set()
        tornado.ioloop.IOLoop.instance().start()

class DirectiveCaptureHandler(tornado.web.RequestHandler):
    def initialize(self, q):
        self.q = q

    def post(self):
        data = json.loads(self.request.body)
        self.q.put(data['d'])

def ClearStatsDb():
    # Clear the stats database
    conn = sqldb.connect(user=options.stats_db_user,
                         passwd=options.stats_db_pass,
                         host='localhost',
                         db=options.stats_db)
    statscursor = conn.cursor()
    stats.db.execute(statscursor,
                     '''DELETE from hourly_events''')
    stats.db.execute(statscursor,
                     '''DELETE from last_update''')
    stats.db.execute(
        statscursor,
        'REPLACE INTO last_update (tablename, logtime) VALUES (%s, %s)',
        ('hourly_events', datetime.utcfromtimestamp(0)))
    conn.commit()

def LaunchStatsDb():
    '''Launches the stats db, which is a mysql interface.

    Makes sure that the database is up.
    '''
    if options.get('stats.stats_processor.stats_host') <> 'localhost':
        raise Exception('Stats db has to be local so we do not squash '
                        'important data')
    
    _log.info('Connecting to stats db')
    try:
        conn = sqldb.connect(user=options.stats_db_user,
                             passwd=options.stats_db_pass,
                             host='localhost',
                             db=options.stats_db)
    except sqldb.Error as e:
        _log.error(('Error connection to stats db. Make sure that you '
                    'have a mysql server running locally and that it has '
                    'a database name: %s with user: %s and pass: %s. ') 
                    % (options.stats_db, options.stats_db_user,
                       options.stats_db_pass))
        raise

    cursor = conn.cursor()    
    stats.db.create_tables(cursor)
    ClearStatsDb()
    
    _log.info('Connection to stats db is good')

def LaunchVideoDb():
    '''Launches the video db.'''
    if options.get('supportServices.neondata.dbPort') == 6379:
        raise Exception('Not allowed to talk to the default Redis server '
                        'so that we do not accidentially erase it. '
                        'Please change the port number.')
    
    _log.info('Launching video db')
    proc = subprocess.Popen([
        '/usr/bin/env', 'redis-server',
        os.path.join(os.path.dirname(__file__), 'test_video_db.conf')],
        stdout=subprocess.PIPE)

    # Wait until the db is up correctly
    upRe = re.compile('The server is now ready to accept connections on port')
    video_db_log = []
    while proc.poll() is None:
        line = proc.stdout.readline()
        video_db_log.append(line)
        if upRe.search(line):
            break

    if proc.poll() is not None:
        raise Exception('Error starting video db. Log:\n%s' %
                        '\n'.join(video_db_log))

    _log.info('Video db is up')

def LaunchSupportServices():
    proc = multiprocessing.Process(target=supportServices.services.main)
    proc.start()
    _log.warn('Launching Support Services with pid %i' % proc.pid)

def LaunchMastermind():
    proc = multiprocessing.Process(target=mastermind.server.main,
                                   args=(_activity_watcher,))
    proc.start()
    _log.warn('Launching Mastermind with pid %i' % proc.pid)

def LaunchClickLogServer():
    proc = multiprocessing.Process(
        target=clickTracker.trackserver.main,
        args=(_activity_watcher,))
    proc.start()
    _log.warn('Launching click log server with pid %i' % proc.pid)

def LaunchFakeS3():
    '''Launch a fakes3 instance if the settings call for it.'''
    s3host = options.get('utils.s3.s3host')
    s3port = options.get('utils.s3.s3port')

    if s3host == 'localhost':
        _log.info('Launching fakes3')
        proc = subprocess.Popen([
            '/usr/bin/env', 'fakes3',
            '--root', options.fakes3root,
            '--port', str(s3port)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT)

        upRe = re.compile('port=')
        fakes3_log = []
        while proc.poll() is None:
            line = proc.stdout.readline()
            fakes3_log.append(line)
            if upRe.search(line):
                break

        if proc.poll() is not None:
            raise Exception('Error starting fake s3. Log:\n%s' %
                            '\n'.join(fakes3_log))

        _log.warn('FakeS3 is up with pid %i' % proc.pid)

def LaunchStatsProcessor():
    proc = multiprocessing.Process(
        target=stats.stats_processor.main,
        args=(_erase_local_log_dir, _activity_watcher))
    proc.start()
    _log.warn('Launching stats processor with pid %i' % proc.pid)

def main():
    signal.signal(signal.SIGTERM, lambda sig, y: sys.exit(-sig))
    atexit.register(utils.ps.shutdown_children)

    # Turn off the annoying logs
    logging.getLogger('tornado.access').propagate = False
    logging.getLogger('tornado.application').propagate = False
    logging.getLogger('mrjob.local').propagate = False
    logging.getLogger('mrjob.config').propagate = False
    logging.getLogger('mrjob.conf').propagate = False
    logging.getLogger('mrjob.runner').propagate = False
    logging.getLogger('mrjob.sim').propagate = False

    LaunchStatsDb()
    LaunchVideoDb()
    LaunchSupportServices()
    LaunchMastermind()
    LaunchClickLogServer()
    LaunchFakeS3()
    LaunchStatsProcessor()

    _activity_watcher.wait_for_idle()

    suite = unittest.TestLoader().loadTestsFromTestCase(TestServingSystem)
    result = unittest.TextTestRunner().run(suite)

    if result.wasSuccessful():
        sys.exit(0)
    else:
        sys.exit(1)
    

if __name__ == "__main__":
    utils.neon.InitNeonTest()
    main()
