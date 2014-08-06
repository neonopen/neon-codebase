#!/usr/bin/env python
''''
Server that logs data from the tracker and sends it to a local flume agent

Tornado server listens for http requests and sends them to flume. If
flume can't handle the load, the events are logged to disk and then
replayed when flume comes back.

'''

import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

import avro.io
import avro.schema
from clickTracker.flume import ThriftSourceProtocol
from clickTracker.flume.ttypes import *
from clickTracker import TTornado
import hashlib
import httpagentparser
import json
import os
import Queue
import re
import shortuuid
from cStringIO import StringIO
import threading
from thrift import Thrift
from thrift.transport import TTransport
from thrift.protocol import TCompactProtocol
import time
import tornado.gen
import tornado.ioloop
import tornado.web
import tornado.httpserver
import tornado.escape
import utils.http
from utils.inputsanitizer import InputSanitizer
import utils.neon
import utils.ps
import utils.sync

import boto.exception
from utils.s3 import S3Connection

#logging
import logging
_log = logging.getLogger(__name__)

from utils.options import define, options
define("port", default=9080, help="run on the given port", type=int)
define("flume_port", default=6367, type=int,
       help='Port to talk to the flume agent running locally')
define("backup_disk", default="/mnt/neon/backlog", type=str,
        help="Location to store backup lines which failed to send to the flume agent")
define("backup_max_events_per_file", default=100000, type=int,
       help='Maximum events to allow backups on per file')
define("flume_flush_interval", default=100, type=int,
       help='Flush flume events after how many events?')
define("message_schema",
       default=os.path.abspath(
           os.path.join(os.path.dirname(__file__), '..', 'schema',
                        'compiled', 'TrackerEvent.avsc')),
        help='Path to the output avro message schema (avsc) file')
define("schema_bucket", default="neon-avro-schema",
       help='S3 Bucket that contains schemas')
define("isp_host", default="127.0.0.1",
       help="Host where the image serving platform is.")
define("isp_port", default=8089,
       help="Host where the image serving platform resides")

from utils import statemon
statemon.define('qsize', int)
statemon.define('flume_errors', int)
statemon.define('messages_handled', int)
statemon.define('invalid_messages', int)
statemon.define('internal_server_error', int)
statemon.define('unknown_basename', int)
statemon.define('isp_connection_error', int)
statemon.define('not_interesting_message', int)

class NotInterestingData(Exception): pass

# TODO(mdesnoyer): Remove version 1 code once it is phased out

#############################################
#### DATA FORMAT ###
#############################################

class TrackerData(object):
    '''
    Schema for click tracker data
    '''
    def __init__(self, action, _id, ttype, cts, sts, page, cip, imgs, tai,
                 cvid=None, xy=None):
        #TODO: handle unicode data too 
        
        self.a = action # load/ click
        self.id = _id    # page load id
        self.ttype = ttype #tracker type
        self.ts = cts #client timestamp
        self.sts = sts #server timestamp
        self.cip = cip #client IP
        self.page = page # Page where the video is shown
        self.tai = tai # Tracker account id
        if isinstance(imgs, list):        
            self.imgs = imgs #image list
            self.cvid = cvid #current video in the player
        else:
            self.img = imgs  #clicked image
            if xy:
                self.xy = xy 

    def to_flume_event(self, writer=None, schema_hash=None):
        '''Coverts the data to a flume event.'''
        return ThriftFlumeEvent(headers = {
                'timestamp' : str(self.sts),
                'tai' : self.tai,
                'track_vers' : '1',
                'event': self.a,
                'schema': schema_hash
                }, body = json.dumps(self.__dict__))

class BaseTrackerDataV2(object):
    '''
    Object that mirrors the the Avro TrackerEvent schema and is used to 
    write the Avro data
    '''
    # A map from schema entries to the http headers where the value is found
    header_map = {
        'uagent' : 'User-Agent',
        'country': 'Geoip_country_code3',
        'city' : 'Geoip_city',
        'region' : "Geoip_region",
        'zip' : "Geoip_postal_code",
        'lat' : "Geoip_latitude",
        'lon' : "Geoip_longitude"
        }

    tracker_type_map = {
        'brightcove' : 'BRIGHTCOVE',
        'ooyala' : 'OOYALA',
        'bcgallery' : 'BCGALLERY',
        'ign' : 'IGN'
        }
        
    
    def __init__(self, request):
        self.pageId = request.get_argument('pageid') # page_id
        self.trackerAccountId = request.get_argument('tai') # tracker_account_id
        # tracker_type (brightcove, ooyala, bcgallery, ign as of April 2014)
        try:
            self.trackerType = \
              BaseTrackerDataV2.tracker_type_map[request.get_argument('ttype').lower()]
        except KeyError:
            raise tornado.web.HTTPError(
                400, "Invalid ttype %s" % request.get_argument('ttype'))
        
        self.pageURL = request.get_argument('page') # page_url
        self.refURL = request.get_argument('ref', None) # referral_url

        self.serverTime = long(time.time() * 1000) # Server time stamp in ms
        self.clientTime = long(request.get_argument('cts')) # client_time in ms
        self.clientIP = request.request.remote_ip # client_ip
        # Neon's user id
        self.neonUserId = request.get_cookie('neonglobaluserid', default="") 

        self.userAgent = self.get_header_safe(request, 'User-Agent')
        if self.userAgent:
            self.agentInfo = BaseTrackerDataV2.extract_agent_info(
                self.userAgent)

        self.ipGeoData = {
            'country': self.get_header_safe(request, 'Geoip_country_code3'),
            'city': self.get_header_safe(request, 'Geoip_city'),
            'region': self.get_header_safe(request, 'Geoip_region'),
            'zip': self.get_header_safe(request, 'Geoip_postal_code'),
            'lat': self.get_header_safe(request, 'Geoip_latitude', float),
            'lon': self.get_header_safe(request, 'Geoip_longitude', float)
            }

        self.eventData = {}

    @tornado.gen.coroutine
    def fill_thumbnail_ids(self, request):
        '''Fills the thumbnail ids for the event.

        Must be implemented by subclasses if necessary, but defaults
        to filling a single thumbnail id with the 'tid' or 'bn'
        argument that is optional (if it is not there, the thumbnail
        id is unknown.
        '''
        self.eventData['thumbnailId'] = None
        try:
            # Try getting the thumbnail id when it is explicit in the arguments
            self.eventData['thumbnailId'] = \
              InputSanitizer.sanitize_null(request.get_argument('tid'))
        except tornado.web.MissingArgumentError:
            # Now try getting it from the image basename
            try:
                bn = request.get_argument('bn')
            except tornado.web.MissingArgumentError:
                # It's optional, so stop
                return
            tids = yield self._lookup_thumbnail_ids_from_isp([bn])
            if tids[0] is None:
                raise NotInterestingData()
            self.eventData['thumbnailId'] = InputSanitizer.sanitize_null(
                tids[0])

    @tornado.gen.coroutine
    def _lookup_thumbnail_ids_from_isp(self, basenames):
        '''Uses the image serving platform to find the thumbnails ids.

        Inputs:
        basenames - List of image basenames

        Returns:
        list of thumbnail ids, or None if it is unknown
        '''
        vidRe = re.compile('neonvid_([0-9a-zA-Z]+_[0-9a-zA-Z]+)')
        tidRe = re.compile('neontn_([0-9a-zA-Z]+_[0-9a-zA-Z]+_[0-9a-zA-Z]+)')

        # Parse the basenames
        vids = []
        tids = []
        for bn in basenames:
            tidSearch = tidRe.search(bn)
            if tidSearch:
                tids.append(tidSearch.group(1))
                vids.append(None)
            else:
                vidSearch = vidRe.search(bn)
                tids.append(None)
                if vidSearch:
                    vids.append(vidSearch.group(1))
                else:
                    # TODO(mdesnoyer): Log the unknown basenames but
                    # limit the number of times each unknown one is
                    # logged.                    
                    vids.append(None)
                    statemon.state.increment('unknown_basename')

        # Send a request to the image serving platform for all the video ids
        to_req =  [x for x in vids if x is not None]
        if len(to_req) > 0:
            headers = ({"Cookie" : 'neonglobaluserid=%s' % self.neonUserId} 
                       if self.neonUserId else None)
            request = tornado.httpclient.HTTPRequest(
                'http://%s:%s/getthumbnailid/%s?params=%s' % (
                    options.isp_host,
                    options.isp_port,
                    self.trackerAccountId,
                    ','.join(to_req)),
                headers=headers)
            response = yield tornado.gen.Task(utils.http.send_request, request)
            if response.error:
                    
                statemon.state.increment('isp_connection_error')
                _log.error('Error getting tids from the image serving '
                           'platform.')
                raise tornado.web.HTTPError(500, str(response.error))
            
            tid_response = response.body.split(',')
            if len(tid_response) != len(to_req):
                _log.error('Response from the Image Serving Platform is '
                           'invalid. Request was %s. Response was %s' % 
                           (request.url, response.body))
                raise tornado.web.HTTPError(500)
            for i in range(len(to_req)):
                if tid_response[i] == 'null':
                    statemon.state.increment('unknown_basename')
                    _log.error('No thumbnail id known for video id %s' %
                               to_req[i])
                else:
                    tids[i] = tid_response[i]

        raise tornado.gen.Return(tids)

    def get_header_safe(self, request, header_name, typ=unicode):
        '''Returns the header value, or None if it's not there.'''
        try:
            strval = unicode(request.request.headers[header_name], 'utf-8')
            if strval == '':
                return None
            return typ(strval)
        except KeyError:
            return None
        except ValueError as e:
            raise tornado.web.HTTPError(
                400, "Invalid header info %s" % e)
            

    @staticmethod
    def extract_agent_info(uagent):
        retval = {}
        try:
            raw_data = httpagentparser.detect(uagent)
            if 'browser' not in raw_data:
                return None
            retval['browser'] = raw_data['browser']
            if 'dist' in raw_data:
                retval['os'] = raw_data['dist']
            elif 'flavor' in raw_data:
                retval['os'] = raw_data['flavor']
            elif 'platform' in raw_data:
                retval['os'] = raw_data['platform']
            else:
                retval['os'] = raw_data['os']
        except Exception, e:
            _log.exception("httpagentparser failed %s" % e)
            return None
        return retval

    def to_flume_event(self, writer, schema_url):
        '''Coverts the data to a flume event.'''
        encoded_str = StringIO()
        encoder = avro.io.BinaryEncoder(encoded_str)
        writer.write(self.__dict__, encoder)
        return ThriftFlumeEvent(headers = {
                'timestamp' : str(self.serverTime),
                'tai' : self.trackerAccountId,
                'track_vers' : '2.2',
                'event' : self.eventType,
                'flume.avro.schema.url' : schema_url
                }, body=encoded_str.getvalue())

    @staticmethod
    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def generate(request_handler):
        '''A Factory generator to make the event.

        Inputs:
        request_handler - The http request handler
        '''
        event_map = {
            'iv' : ImagesVisible,
            'il' : ImagesLoaded,
            'ic' : ImageClicked,
            'vp' : VideoPlay,
            'vc' : VideoClick,
            'ap' : AdPlay,
            'vvp' : VideoViewPercentage}

        action = request_handler.get_argument('a')
        try:
            event = event_map[action](request_handler)
            yield event.fill_thumbnail_ids(request_handler)
            raise tornado.gen.Return(event)
        except KeyError as e:
            _log.error('Invalid event: %s' % action)
            raise tornado.web.HTTPError(400)
    
class ImagesVisible(BaseTrackerDataV2):
    '''An event specifying that the image became visible.'''
    def __init__(self, request):
        super(ImagesVisible, self).__init__(request)
        self.eventData['isImagesVisible'] = True
        self.eventType = 'IMAGES_VISIBLE'
        self.eventData['thumbnailIds'] = []

    @tornado.gen.coroutine
    def fill_thumbnail_ids(self, request):
        try:
            tids = request.get_argument('tids').split(',')
        except tornado.web.MissingArgumentError:
            tids = yield self._lookup_thumbnail_ids_from_isp(
                request.get_argument('bns').split(','))
        self.eventData['thumbnailIds'] = [x for x in tids if x is not None]
        if len(self.eventData['thumbnailIds']) == 0:
            raise NotInterestingData()

class ImagesLoaded(BaseTrackerDataV2):
    '''An event specifying that the image were loaded.'''
    def __init__(self, request):
        super(ImagesLoaded, self).__init__(request)
        self.eventData['isImagesLoaded'] = True
        self.eventType = 'IMAGES_LOADED'
        self.eventData['images'] = [] 

    @tornado.gen.coroutine
    def fill_thumbnail_ids(self, request):
        tids = []
        vids = []
        widths = []
        heights = []
        images = []
        has_tids = False
        try:
            arg_list = request.get_argument('tids')
            has_tids = True
        except tornado.web.MissingArgumentError:
            arg_list = request.get_argument('bns')
        if len(arg_list) > 0:
            for tup in arg_list.split(','):
                elems = tup.split(' ') # '+' delimiter converts to ' '
                if has_tids:
                    tids.append(elems[0])
                else:
                    vids.append(elems[0])
                widths.append(int(elems[1]))
                heights.append(int(elems[2]))

            if not has_tids:
                tids = yield self._lookup_thumbnail_ids_from_isp(vids)

            for w, h, tid in zip(widths, heights, tids):
                if tid is not None:
                    images.append({'thumbnailId' : tid,
                                   'width' : w,
                                   'height' : h})
        self.eventData['images'] = images
        if len(self.eventData['images']) == 0:
            raise NotInterestingData()
            

class ImageClicked(BaseTrackerDataV2):
    '''An event specifying that the image was clicked.'''
    def __init__(self, request):
        super(ImageClicked, self).__init__(request)
        self.eventData['isImageClick'] = True
        self.eventType = 'IMAGE_CLICK'
        self.eventData['thumbnailId'] = None
        self.eventData['pageCoords'] = {
            'x' : float(request.get_argument('x', 0)),
            'y' : float(request.get_argument('y', 0))
            }
        self.eventData['windowCoords'] = {
            'x' : float(request.get_argument('wx', 0)),
            'y' : float(request.get_argument('wy', 0))
            }
        self.eventData['imageCoords'] = {
            'x' : float(request.get_argument('cx', 0)),
            'y' : float(request.get_argument('cy', 0))
            }

    @tornado.gen.coroutine
    def fill_thumbnail_ids(self, request):
        '''The thumbnail id is required, so we can't use the default.'''
        try:
            self.eventData['thumbnailId'] = request.get_argument('tid')
        except tornado.web.MissingArgumentError:
            tids = yield self._lookup_thumbnail_ids_from_isp(
                [request.get_argument('bn')])
            if tids[0] is None:
                raise NotInterestingData()
            self.eventData['thumbnailId'] = tids[0]

class VideoClick(BaseTrackerDataV2):
    '''An event specifying that the image was clicked within the player'''
    def __init__(self, request):
        super(VideoClick, self).__init__(request)
        self.eventData['isVideoClick'] = True
        self.eventData['thumbnailId'] = None
        self.eventType = 'VIDEO_CLICK'
        self.eventData['videoId'] = request.get_argument('vid') # Video id
        self.eventData['playerId'] = request.get_argument('playerid', None) # Player id

class VideoPlay(BaseTrackerDataV2):
    '''An event specifying that the image were loaded.'''
    def __init__(self, request):
        super(VideoPlay, self).__init__(request)
        self.eventData['isVideoPlay'] = True
        
        self.eventType = 'VIDEO_PLAY'
        # Thumbnail id
        self.eventData['thumbnailId'] = None
        self.eventData['videoId'] = request.get_argument('vid') # Video id
        self.eventData['playerId'] = request.get_argument('playerid', None) # Player id
         # If an adplay preceeded video play 
        self.eventData['didAdPlay'] = InputSanitizer.to_bool(
            request.get_argument('adplay', False))
        # (time when player initiates request to play video - 
        #             Last time an image or the player was clicked) 
        self.eventData['autoplayDelta'] = InputSanitizer.sanitize_int(
            request.get_argument('adelta')) # autoplay delta in milliseconds
        self.eventData['playCount'] = InputSanitizer.sanitize_int(
            request.get_argument('pcount')) #the current count of the video playing on the page 
        self.infocus = utils.inputsanitizer.InputSanitizer.to_bool(
                request.get_argument('infocus', True)) # Was the player in foucs when video started playing (optional) 

class AdPlay(BaseTrackerDataV2):
    '''An event specifying that the image were loaded.'''
    def __init__(self, request):
        super(AdPlay, self).__init__(request)
        self.eventData['isAdPlay'] = True
        
        self.eventType = 'AD_PLAY'
        # Thumbnail id
        self.eventData['thumbnailId'] = None
        #VID can be null, if VideoClick event doesn't fire before adPlay
        # Video id
        self.eventData['videoId'] = InputSanitizer.sanitize_null(request.get_argument('vid')) 
        self.eventData['playerId'] = request.get_argument('playerid', None) # Player id
        # (time when player initiates request to play video - Last time an image or the player was clicked) 
        self.eventData['autoplayDelta'] = InputSanitizer.sanitize_int(
            request.get_argument('adelta')) # autoplay delta in millisecond
         #the current count of the video playing on the page
        self.eventData['playCount'] = InputSanitizer.sanitize_int(request.get_argument('pcount'))

class VideoViewPercentage(BaseTrackerDataV2):
    '''An event specifying that a percentage of the video was viewed.'''
    def __init__(self, request):
        super(VideoViewPercentage, self).__init__(request)
        self.eventData['isVideoViewPercentage'] = True

        self.eventType = 'VIDEO_VIEW_PERCENTAGE'

        # External video id
        self.eventData['videoId'] = request.get_argument('vid')

        #the current count of the video playing on the page
        self.eventData['playCount'] = InputSanitizer.sanitize_int(request.get_argument('pcount'))

        # Percentage of the video that has been seen (1-100)
        try:
            self.eventData['percent'] = round(
                float(request.get_argument('prcnt')))
        except ValueError:
            raise tornado.web.MissingArgumentError('prcnt')

    @tornado.gen.coroutine
    def fill_thumbnail_ids(self, request):
        '''There is no thumbnail id for this event, so just return.'''
        return

#############################################
#### WEB INTERFACE #####
#############################################

class TrackerDataHandler(tornado.web.RequestHandler):
    '''Common class to handle http requests to the tracker.'''

    @tornado.gen.coroutine
    def parse_tracker_data(self, version):
        '''Parses the tracker data from a GET request.

        returns:
        TrackerData object
        '''
        if version == 1:
            raise tornado.gen.Return(self._parse_v1_tracker_data())
        elif version == 2:
            event = yield BaseTrackerDataV2.generate(self, async=True)
            raise tornado.gen.Return(event)
        else:
            _log.fatal('Invalid api version %s' % version)
            raise ValueError('Bad version %s' % version)
        

    def _parse_v1_tracker_data(self):
        ttype = self.get_argument('ttype')
        action = self.get_argument('a')
        _id = self.get_argument('id')
        cts = self.get_argument('ts')
        sts = int(time.time())
        page = self.get_argument('page') #url decode
        tai = self.get_argument('tai') #tracker account id 
        cvid = None

        #On load the current video loaded in the player is logged
        if action == 'load':
            imgs = self.get_argument('imgs')
            imgs = [e.strip('"\' ') for e in imgs.strip('[]').split(',')]
            if ttype != 'imagetracker':
                cvid = self.get_argument('cvid')
        else:
            imgs = self.get_argument('img')

        xy = self.get_argument('xy', None) #click on image
        cip = self.request.remote_ip
        return TrackerData(action, _id, ttype, cts, sts, page, cip, imgs, tai,
                           cvid, xy)

class FlumeBuffer:
    '''Class that handles buffering messages to flume.'''
    def __init__(self, port, backup_q):
        self.port = port
        self.backup_q = backup_q
        self.client = None
        self.buffer = []
        self.flush_interval = options.flume_flush_interval

    @tornado.gen.coroutine
    def _open(self):
        '''Opens a connection to flume.'''

        try:
            transport = TTornado.TTornadoStreamTransport('localhost',
                                                         self.port)
            pfactory = TCompactProtocol.TCompactProtocolFactory()
            self.client = ThriftSourceProtocol.Client(transport, pfactory)
            yield tornado.gen.Task(transport.open)
            self.is_open = True
        except TTransport.TTransportException as e:
            _log.error('Error opening connection to Flume: %s' % e)
            raise
        
    @tornado.gen.coroutine
    def send(self, event):
        '''Send an events to flume.

        event - A ThriftFlumeEvent object
        '''
        self.buffer.append(event)

        if len(self.buffer) >= self.flush_interval:
            yield self._send_buffer()

    @tornado.gen.coroutine
    def _send_buffer(self):
        '''Sends all the events in the buffer to flume.'''
        # First copy the buffer and put a new empty one in so that
        # another call can add to it without losing messages.
        local_buf = self.buffer
        self.buffer = []

        try:
            yield self._open()
            
            status = yield tornado.gen.Task(self.client.appendBatch, local_buf)
            if status != Status.OK:
                raise Thrift.TException('Flume returned error: %s' % status)
        except Thrift.TException as e:
            _log.error('Error writing to Flume: %s' % e)
            statemon.state.increment('flume_errors')
            for event in local_buf:
                self.backup_q.put(event)
            
        except IOError as e:
            _log.error('Error writing to Flume stream: %s' % e)
            statemon.state.increment('flume_errors')
            for event in local_buf:
                self.backup_q.put(event)
        

class LogLines(TrackerDataHandler):
    '''Handler for real tracking data that should be logged.'''

    def initialize(self, watcher, version, avro_writer, schema_url,
                   flume_buffer):
        '''Initialize the logger.'''
        self.watcher = watcher
        self.version = version
        self.avro_writer = avro_writer
        self.schema_url = schema_url
        self.flume_buffer = flume_buffer
    
    @tornado.web.asynchronous
    @tornado.gen.coroutine
    def get(self, *args, **kwargs):
        '''Handle a tracking request.'''
        with self.watcher.activate():
            #statemon.state.increment('messages_handled')
            
            try:
                tracker_data = yield self.parse_tracker_data(self.version)
            except tornado.web.MissingArgumentError as e:
                _log.error('Invalid request: %s' % self.request.uri)
                statemon.state.increment('invalid_messages')
                raise
            except tornado.web.HTTPError as e:
                _log.error('Error processing request %s: %s' % (
                    self.request.uri, e))
                statemon.state.increment('internal_server_error')
                raise
            except NotInterestingData as e:
                # The data wasn't interesting to us even though it was
                # valid, so just record that and don't send the data
                # on.
                statemon.state.increment('not_interesting_message')
                self.set_status(200)
                self.finish()
                return
            except Exception, err:
                _log.exception("key=get_track request=%s msg=%s",
                               self.request.uri, err)
                statemon.state.increment('internal_server_error')
                self.set_status(500)
                self.finish()
                return

            data = tracker_data.to_flume_event(self.avro_writer,
                                               self.schema_url)
            try:
                yield self.flume_buffer.send(data)
                self.set_status(200)
                
            except Exception, err:
                _log.exception("key=loglines msg=Q error %s", err)
                self.set_status(500)
            self.finish()

    def memory_check(self):
        '''Method to check memory on the node'''
        return True

class TestTracker(TrackerDataHandler):
    '''Handler for test requests.'''

    def initialize(self, version):
        '''Initialize the logger.'''
        self.version = version
    
    @tornado.web.asynchronous
    @tornado.gen.coroutine
    def get(self, *args, **kwargs):
        '''Handle a test tracking request.'''
        try:
            tracker_data = yield self.parse_tracker_data(self.version)
        except tornado.web.HTTPError as e:
            raise
        except NotInterestingData as e:
            pass
        except Exception as err:
            _log.exception("key=test_track msg=%s", err) 
            self.set_status(500)
            self.finish()
            return
        
        self.set_status(200)
        self.finish()

###########################################
# File Backup Handler thread 
###########################################
class FileBackupHandler(threading.Thread):
    '''Thread that uploads data to S3.'''
    
    def __init__(self, dataQ, watcher=utils.ps.ActivityWatcher()):
        super(FileBackupHandler, self).__init__()
        self.dataQ = dataQ
        self.daemon = True
        self.watcher = watcher
        self.backup_stream = None
        self.protocol_writer = None
        self.events_in_file = 0

        statemon.state.qsize = self.dataQ.qsize()

        # Make sure the backup directory exists
        if not os.path.exists(options.backup_disk):
            os.makedirs(options.backup_disk)

    def __del__(self):
        if self.backup_stream is not None:
            self.backup_stream.close()

    def _generate_log_filename(self):
        '''Create a new log filename.'''
        return '%s_%s_clicklog.log' % (
            time.strftime('%S%M%H%d%m%Y', time.gmtime()),
            shortuuid.uuid())

    def _open_new_backup_file(self):
        '''Opens a new backup file and puts it on self.backup_stream.'''
        if not os.path.exists(options.backup_disk):
            os.makedirs(options.backup_disk)
            
        backup_file = \
          open(os.path.join(options.backup_disk,
                            self._generate_log_filename()),
                            'wb')
        self.backup_stream = TTransport.TFileObjectTransport(
            backup_file)
        self.protocol_writer = TCompactProtocol.TCompactProtocol(
            self.backup_stream)

    def _prepare_backup_stream(self):
        '''Prepares the backup stream for writing to.

        This could mean flushing it to disk or closing this file and
        opening a new one.
        '''
        if self.backup_stream is None:
            self._open_new_backup_file()

        # See if the file should be flushed
        if self.events_in_file % options.flume_flush_interval == 0:
            self.backup_stream.flush()

        # Check to see if the file should be rolled over
        if self.events_in_file >= options.backup_max_events_per_file:
            self.backup_stream.close()
            self._open_new_backup_file()
            self.events_in_file = 0

    def run(self):
        '''Main runner for the handler.'''
        while True:
            try:
                try:
                    event = self.dataQ.get(True, 30)
                except Queue.Empty:
                    if self.backup_stream is not None:
                        self.backup_stream.flush()
                    continue

                with self.watcher.activate():
                    statemon.state.qsize = self.dataQ.qsize()
                    self._prepare_backup_stream()

                    event.write(self.protocol_writer)
                    self.events_in_file += 1
            except Exception as err:
                _log.exception("key=file_backup_handler msg=%s", err)

            self.dataQ.task_done()

class HealthCheckHandler(TrackerDataHandler):
    '''Handler for health check ''' 
    
    @tornado.web.asynchronous
    def get(self, *args, **kwargs):
        '''Handle a test tracking request.'''

        self.write("<html> Server OK </html>")
        self.finish()

###########################################
# Create Tornado server application
###########################################

class Server(threading.Thread):
    '''The server, which can be run as it's own thread.

    Or just call run() directly to have it startup and block.
    '''
    def __init__(self, watcher=utils.ps.ActivityWatcher()):
        '''Create the server. 

        Inputs:
        
        watcher - Optional synchronization object that can be used to
        know when the server is active.
        
        '''
        super(Server, self).__init__()
        self.backup_queue = Queue.Queue()
        self.backup_handler = FileBackupHandler(self.backup_queue, watcher)
        self.io_loop = tornado.ioloop.IOLoop()
        self._is_running = threading.Event()
        self._watcher = watcher

        # Figure out the message schema
        with open(options.message_schema) as f:
            schema_str = f.read()
        schema = avro.schema.parse(schema_str)
        schema_hash = hashlib.md5(schema_str).hexdigest()
        schema_url = ('http://%s.s3.amazonaws.com/%s.avsc' % 
                      (options.schema_bucket, schema_hash))
        avro_writer = avro.io.DatumWriter(schema)
        flume_buffer = FlumeBuffer(options.flume_port, self.backup_queue)

        # Make sure that the schema exists at a URL that can be reached
        response = utils.http.send_request(
            tornado.httpclient.HTTPRequest(schema_url), 2)
        if response.error:
            _log.fatal('Could not find schema at %s. '
                       'Did you run schema/compile_schema.py?' % 
                       schema_url)
            raise response.error

        self.application = tornado.web.Application([
            (r"/", LogLines, dict(watcher=self._watcher,
                                  version=1,
                                  avro_writer=avro_writer,
                                  schema_url=schema_url,
                                  flume_buffer=flume_buffer)),
            (r"/v2", LogLines, dict(watcher=self._watcher,
                                    version=2,
                                    avro_writer=avro_writer,
                                    schema_url=schema_url,
                                    flume_buffer=flume_buffer)),
            (r"/track", LogLines, dict(watcher=self._watcher,
                                       version=1,
                                       avro_writer=avro_writer,
                                       schema_url=schema_url,
                                       flume_buffer=flume_buffer)),
            (r"/v2/track", LogLines, dict(watcher=self._watcher,
                                          version=2,
                                          avro_writer=avro_writer,
                                          schema_url=schema_url,
                                          flume_buffer=flume_buffer
                                          )),
            (r"/test", TestTracker, dict(version=1)),
            (r"/v2/test", TestTracker, dict(version=2)),
            (r"/healthcheck", HealthCheckHandler),
            ])

    def run(self):
        statemon.state.flume_errors = 0
        statemon.state.messages_handled = 0
        statemon.state.invalid_messages = 0
        
        with self._watcher.activate():
            self.backup_handler.start()
            self.io_loop.make_current()
            
            server = tornado.httpserver.HTTPServer(self.application,
                                                   io_loop=self.io_loop,
                                                   xheaders=True)
            utils.ps.register_tornado_shutdown(server)
            server.listen(options.port)
        

            self._is_running.set()
        self.io_loop.start()
        server.stop()

    @tornado.gen.engine
    def wait_until_running(self):
        '''Blocks until the server/io_loop is running.'''
        self._is_running.wait()
        yield tornado.gen.Task(self.io_loop.add_callback)

    def wait_for_processing(self):
        '''Blocks until the current requests are all processed.'''
        self.event_queue.join()

    def stop(self):
        '''Stops the server'''
        self.io_loop.stop()

def main(watcher=utils.ps.ActivityWatcher()):
    '''Main function that runs the server.'''
    with watcher.activate():
        server = Server(watcher)
    server.run()
    

# ============= MAIN ======================== #
if __name__ == "__main__":
    utils.neon.InitNeon()
    #Turn off Access logs for tornado
    logging.getLogger('tornado.access').propagate = False
    main()
