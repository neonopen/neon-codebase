'''
Abstract definitions for an OVP integration.

Authors: Mark Desnoyer (desnoyer@neon-lab.com)
Copyright 2015 Neon Labs
'''
import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

from cmsdb import neondata
from integrations.exceptions import IntegrationError
import json
import logging
import tornado.gen
import utils.http
from utils.options import define, options
from utils import statemon
import utils.sync

statemon.define('job_submission_error', int)
statemon.define('new_job_submitted', int)
statemon.define('unexpected_submission_error', int)
define('max_submit_retries', default=3, 
       help='Maximum times we will retry a video submit before passing on it.')

define('cmsapi_host', default='services.neon-lab.com',
       help='Host where the cmsapi is')
define('cmsapi_port', default=80, type=int, help='Port where the cmsapi is')

_log = logging.getLogger(__name__)

class OVPError(IntegrationError): pass
class OVPRefIDError(OVPError): pass
class OVPCustomRefIDError(OVPError): pass
class CMSAPIError(IntegrationError): pass

class OVPIntegration(object):
    def __init__(self, account_id, platform):
        # Neon Account ID 
        self.account_id = account_id 
        # An AbstractPlatform or AbstractIntegration Object 
        self.platform = platform
        # hacky to make generic work for dual keyed platform objects 
        self.is_platform_dual_keyed = False
    
    @tornado.gen.coroutine 
    def submit_many_videos(self, videos): 
        '''Submits many videos utilizing child class functions 

        Parameters: 
        videos - json object of many videos

        Returns: 
        dictionary of video_info => { video_id -> job_ids }
        '''
        added_jobs = 0
        last_processed_date = None
        self.set_video_iter(videos)
  
        while True:   
            try:  
                video = self.get_next_video_item() 
                if isinstance(video, StopIteration):
                    break
                video_id = self.get_video_id(video) 
                video_url = self.get_video_url(video) 
                callback_url = self.get_video_callback_url(video) 
                video_title = self.get_video_title(video)
                thumbnail_info = self.get_video_thumbnail_info(video)
                if thumbnail_info['thumb_ref']:  
                    thumb_id = thumbnail_info['thumb_ref'] 
                if thumbnail_info['thumb_url']: 
                    default_thumbnail = thumbnail_info['thumb_url'] 
                custom_data = self.get_video_custom_data(video) 
                duration = self.get_video_duration(video) 
                publish_date = last_processed_date = self.get_video_publish_date(video) 

                existing_video = yield tornado.gen.Task(neondata.VideoMetadata.get, 
                                                        neondata.InternalVideoID.generate(self.account_id, video_id))
                if not existing_video:
                    response = yield self.submit_video(video_id=video_id, 
                                                       video_url=video_url, 
                                                       callback_url=callback_url,
                                                       external_thumbnail_id=thumb_id, 
                                                       custom_data=custom_data, 
                                                       duration=duration, 
                                                       publish_date=publish_date, 
                                                       video_title=unicode(video_title), 
                                                       default_thumbnail=default_thumbnail)
                    if response['job_id']:
                        added_jobs += 1
            except KeyError as e:
                # let's continue here, we do not have enough to submit 
                pass 
            except OVPCustomRefIDError: 
                pass 
            except OVPRefIDError: 
                pass 
            except Exception as e:
                # we got an unknown error from somewhere, it could be video,
                #  server, or api related -- we will retry it on the next goaround 
                #  if we have not reached the max retries for this video, otherwise 
                #  we pass and move on
                def _increase_retries(x):
                    x.video_submit_retries += 1

                if self.platform.video_submit_retries < options.max_submit_retries:
                    # update last_process_date, so we start on this video next time
                    yield self.update_last_processed_date(last_processed_date, 
                                                          reset_retries=False)
                    if self.is_platform_dual_keyed:  
                        self.platform = yield tornado.gen.Task(
                            self.platform.modify,
                            self.platform.neon_api_key,
                            self.platform.integration_id,
                            _increase_retries)
                    else: 
                        self.platform = yield tornado.gen.Task(
                            self.platform.modify,
                            self.platform.integration_id,
                            _increase_retries)
                    _log.info('Added %d jobs for integration before failure.' % added_jobs)
                    return  
                else:
                    _log.error('Unknown error, reached max retries on '
                               'video submit for account %s: item %s: %s' % 
                               (self.account_id, item, e))
                    statemon.state.define_and_increment(
                        'submit_video_bc_error.%s' % self.account_id)
                    pass 
        
        yield self.update_last_processed_date(last_processed_date) 
        _log.info('Added %d jobs for integration' % added_jobs)
 
        raise tornado.gen.Return(self.platform)
    
    @tornado.gen.coroutine 
    def update_last_processed_date(self, 
                                   last_mod_date, 
                                   reset_retries=True):
        if last_mod_date is not None:
            def _set_mod_date_and_retries(x):
                new_date = last_mod_date 
                if new_date > x.last_process_date: 
                    x.last_process_date = new_date
                if reset_retries: 
                    x.video_submit_retries = 0
            if self.is_platform_dual_keyed: 
                self.platform = yield tornado.gen.Task(
                    self.platform.modify,
                    self.platform.neon_api_key,
                    self.platform.integration_id,
                    _set_mod_date_and_retries)
            else: 
                self.platform = yield tornado.gen.Task(
                    self.platform.modify,
                    self.platform.integration_id,
                    _set_mod_date_and_retries)
            
            _log.debug(
                'updated last process date for account %s integration %s'
                 % (self.account_id, self.platform.integration_id))

    @tornado.gen.coroutine
    def submit_video(self, video_id, video_url,
                     callback_url=None,
                     video_title=None,
                     default_thumbnail=None,
                     external_thumbnail_id=None,
                     custom_data=None,
                     duration=None,
                     publish_date=None):
        '''Submits a single video for processing to the CMSAPI.

        Parameters:
        Same as the video post parameters from the CMSAPI

        Returns:
        json returned by the CMSAPI
        '''
        body = {
            'video_id' : video_id,
            'video_url': video_url,
            'video_title': video_title,
            'default_thumbnail': default_thumbnail,
            'external_thumbnail_id': external_thumbnail_id,
            'callback_url': callback_url,
            'custom_data': custom_data,
            'duration': duration,
            'publish_date': publish_date
            }
        headers = {"X-Neon-API-Key" : self.platform.neon_api_key,
                   "Content-Type" : "application/json"}
        url = ('http://%s:%s/api/v1/accounts/%s/neon_integrations/%s/'
               'create_thumbnail_api_request') % (
                   options.cmsapi_host,
                   options.cmsapi_port,
                   self.account_id,
                   self.platform.integration_id)
        request = tornado.httpclient.HTTPRequest(
            url=url,
            method='POST',
            headers=headers,
            body=json.dumps(body),
            request_timeout=300.0,
            connect_timeout=30.0)

        response = yield tornado.gen.Task(utils.http.send_request, request,
                                          base_delay=4.0, ntries=2)

        if response.code == 409:
            _log.warn('Video %s for account %s already exists' % 
                      (video_id, self.platform.neon_api_key))
            raise tornado.gen.Return(json.loads(response.body))
        elif response.error is not None:
            statemon.state.increment('job_submission_error')
            _log.error('Error submitting video %s: %s' % (video_id,
                                                          response.error))
            raise CMSAPIError('Error submitting video: %s' % response.error)

        _log.info('New video was submitted for account %s video id %s'
                  % (self.platform.neon_api_key, video_id))
        statemon.state.increment('new_job_submitted')
        raise tornado.gen.Return(json.loads(response.body))

    @tornado.gen.coroutine
    def lookup_and_submit_videos(self, ovp_video_ids):
        '''Lookup information about videos and submit them as jobs.

        Returns a dictionary of video_id->job_id/Exception
        '''
        raise NotImplementedError()

    @tornado.gen.coroutine
    def process_publisher_stream(self):
        '''Look at the stream of newest videos in the account and submit
        them if necessary.
        '''
        raise NotImplementedError()

    @tornado.gen.coroutine
    def set_thumbnail(self, internal_video_id, thumb_metadata):
        '''Sets the thumbnail that is returned from the OVP's apis.

        TODO: Scope this function better once it is used. For now,
        it's a placeholder.

        Inputs:
        internal_video_id - The internal video id of the video to modify
        thumb_metadata - The thumbnail metadata of the thumbnail to show
        '''
        raise NotImplementedError()

    def get_video_id(self, video):
        '''Find the video_id in the video object
          
           If using submit_many_videos: 
             Child classes must implement this even if it 
             is just to return None 
        '''
        raise NotImplementedError()

    def get_video_url(self, video):
        '''Find the video_url in the video object

           If using submit_many_videos: 
             Child classes must implement this even if it 
             is just to return None 
        '''
        raise NotImplementedError()

    def get_video_callback_url(self, video):
        '''Find the video_callback_url in the video object

           If using submit_many_videos: 
             Child classes must implement this even if it 
             is just to return None 
        '''
        raise NotImplementedError()

    def get_video_title(self, video):
        '''Find the video_title in the video object

           If using submit_many_videos: 
             Child classes must implement this even if it 
             is just to return None 
        '''
        raise NotImplementedError()

    def get_video_custom_data(self, video):
        '''Find custom_data in the video object

           If using submit_many_videos: 
             Child classes must implement this even if it 
             is just to return None 
        '''
        raise NotImplementedError()

    def get_video_duration(self, video):
        '''Find duration in the video object

           If using submit_many_videos: 
             Child classes must implement this even if it 
             is just to return None 
        '''
        raise NotImplementedError()

    def get_video_publish_date(self, video):
        '''Find duration in the video object

           If using submit_many_videos: 
             Child classes must implement this even if it 
             is just to return None 
        '''
        raise NotImplementedError()

    def get_video_thumbnail_info(self, video):
        '''Find the default_thumbnail in the video object

           If using submit_many_videos: 
             Child classes must implement this even if it 
             is just to return None 

           thumbnail_info expects thumb_ref (external_id) and 
             thumb_url (external_url) in a python object eg 
           thumb_info['thumb_ref'] = '1233124' 
           thumb_info['thumb_url'] = 'http://meisaurl.com' 
        '''
        raise NotImplementedError()
