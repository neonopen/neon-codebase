'''
Brightcove API Interface class
'''

import os
import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

import datetime
import json
from poster.encode import multipart_encode
import poster.encode
from PIL import Image
import re
from StringIO import StringIO
import cmsdb.neondata 
import time
import tornado.gen
import tornado.httpclient
import tornado.httputil
import tornado.ioloop
import tornado.escape
import urllib
import utils.http
import utils.logs
import utils.neon
from utils.http import RequestPool
from utils.imageutils import PILImageUtils

import logging
_log = logging.getLogger(__name__)

from utils.options import define, options
#define("local", default=1, help="create neon requests locally", type=int)
define('max_write_connections', default=1, type=int, 
       help='Maximum number of write connections to Brightcove')
define('max_read_connections', default=20, type=int, 
       help='Maximum number of read connections to Brightcove')
define('max_retries', default=5, type=int,
       help='Maximum number of retries when sending a Brightcove error')

class BrightcoveApiError(IOError): pass
class BrightcoveApiClientError(BrightcoveApiError): pass
class BrightcoveApiServerError(BrightcoveApiError): pass

class BrightcoveApi(object): 

    ''' Brighcove API Interface class
    All video ids used in the class refer to the Brightcove platform VIDEO ID
    '''

    write_connection = RequestPool(options.max_write_connections,
                                   options.max_retries)
    read_connection = RequestPool(options.max_read_connections,
                                  options.max_retries)
    
    def __init__(self, neon_api_key, publisher_id=0, read_token=None,
                 write_token=None, autosync=False, publish_date=None,
                 neon_video_server=None, account_created=None):
        self.publisher_id = publisher_id
        self.neon_api_key = neon_api_key
        self.read_token = read_token
        self.write_token = write_token 
        self.read_url = "http://api.brightcove.com/services/library"
        self.write_url = "http://api.brightcove.com/services/post"
        self.autosync = autosync
        self.last_publish_date = publish_date if publish_date else time.time()
        self.neon_uri = "http://localhost:8081/api/v1/submitvideo/"  
        if neon_video_server is not None:
            self.neon_uri = "http://%s:8081/api/v1/submitvideo/" % neon_video_server

        self.THUMB_SIZE = 120, 90
        self.STILL_SIZE = 480, 360
        self.account_created = account_created

    def format_get(self, url, data=None):
        if data is not None:
            if isinstance(data, dict):
                data = urllib.urlencode(data)
            if '?' in url:
                url += '&amp;%s' % data
            else:
                url += '?%s' % data
        return url

    ###### Brightcove media api update method ##########
    
    def find_video_by_id(self, video_id, find_vid_callback=None):
        ''' Brightcove api request to get info about a videoid '''

        url = ('http://api.brightcove.com/services/library?command=find_video_by_id'
                    '&token=%s&media_delivery=http&output=json&' 
                    'video_id=%s' %(self.read_token, video_id)) 

        req = tornado.httpclient.HTTPRequest(url=url, method="GET", 
                request_timeout=60.0, connect_timeout=10.0)

        if not find_vid_callback:
            return BrightcoveApi.read_connection.send_request(req)
        else:
            BrightcoveApi.read_connection.send_request(req, find_vid_callback)

    @tornado.gen.coroutine
    def add_image(self, video_id, tid, image=None, remote_url=None,
                  atype='thumbnail', reference_id=None):
        '''Add Image brightcove api helper method
        
        #NOTE: When uploading an image with a reference ID that already
        #exists, the image is not
        updated. Although other metadata like displayName is updated.

        Inputs:
        video_id - Brightcove video id
        tid - Internal Neon thumbnail id for the image
        image - The image to upload
        remote_url - The remote url to set for this image
        atype - Type of image being uploaded. Either "thumbnail" or "videostill"
        reference_id - Reference id for the image to send to brightcove

        returns:
        dictionary of the JSON of the brightcove response
        '''
        #help.brightcove.com/developer/docs/mediaapi/add_image.cfm
        
        if reference_id:
            reference_id = "v2_%s" %reference_id #V2 ref ID

        im = image            
        image_fname = 'neontn%s.jpg' % (tid) 

        outer = {}
        params = {}
        params["token"] = self.write_token 
        params["video_id"] = video_id
        params["filename"] = image_fname
        params["resize"] = False
        image = {} 
        if reference_id is not None:
            image["referenceId"] = reference_id
        
        if atype == 'thumbnail':    
            image["type"] = "THUMBNAIL"
            image["displayName"] = str(self.publisher_id) + \
                    'neon-thumbnail-for-video-%s'%video_id
        else:
            image["type"] = "VIDEO_STILL"
            image["displayName"] = str(self.publisher_id) + \
                    'neon-video-still-for-video-%s'%video_id 
        
        if remote_url:
            image["remoteUrl"] = remote_url

        params["image"] = image
        outer["params"] = params
        outer["method"] = "add_image"

        body = tornado.escape.json_encode(outer)

        if remote_url:
            post_param = []
            args = poster.encode.MultipartParam("JSONRPC", value=body)
            post_param.append(args)
            datagen, headers = multipart_encode(post_param)
            body = "".join([data for data in datagen])

        else:
            #save image
            filestream = StringIO()
            im.save(filestream, 'jpeg')
            filestream.seek(0)
            image_data = filestream.getvalue()
            post_param = []
            fileparam = poster.encode.MultipartParam(
                "filePath",
                value=image_data,
                filetype='image/jpeg',
                filename=image_fname)
            args = poster.encode.MultipartParam("JSONRPC", value=body)
            post_param.append(args)
            post_param.append(fileparam)
            datagen, headers = multipart_encode(post_param)
            body = "".join([data for data in datagen])
        
        #send request
        client_url = "http://api.brightcove.com/services/post"
        req = tornado.httpclient.HTTPRequest(url=client_url,
                                             method="POST",
                                             headers=headers, 
                                             body=body,
                                             request_timeout=60.0,
                                             connect_timeout=10.0)

        response = yield tornado.gen.Task(
            BrightcoveApi.write_connection.send_request,
            req)
        if response.error:
            if response.error.code >= 500:
                raise BrightcoveApiServerError(
                    'Internal Brightcove error when uploading %s for tid %s %s'
                    % (atype, tid, response.error))
            elif response.error.code >= 400:
                raise BrightcoveApiClientError(
                    'Client error when uploading %s for tid %s %s'
                    % (atype, tid, response.error))
            raise BrightcoveApiClientError(
                'Unexpected error when uploading %s for tid %s %s'
                % (atype, tid, response.error))

        try:
            json_response = tornado.escape.json_decode(response.body)
        except Exception:
            raise BrightcoveApiServerError(
                'Invalid JSON received from Brightcove: %s' %
                response.body)

        raise tornado.gen.Return(json_response['result'])

    @tornado.gen.coroutine
    def update_thumbnail_and_videostill(self,
                                        video_id,
                                        tid,
                                        image=None,
                                        remote_url=None,
                                        thumb_size=None,
                                        still_size=None): 
    
        ''' add thumbnail and videostill in to brightcove account.  

        Inputs:
        video_id - brightcove video id
        tid - Thumbnail id to update reference id with
        image - PIL image to set the image with. Either this or remote_url
                must be set.
        remote_url - A remote url to push into brightcove that points to the
                     image
        thumb_size - (width, height) of the thumbnail
        still_size - (width, height) of the video still image

        Returns:
        (brightcove_thumb_id, brightcove_still_id)
        '''
        thumb_size = thumb_size or self.THUMB_SIZE
        still_size = still_size or self.STILL_SIZE
        if image is not None:
            # Upload an image and set it as the thumbnail
            thumb = PILImageUtils.resize(image,
                                         im_w=thumb_size[0],
                                         im_h=thumb_size[1])
            still = PILImageUtils.resize(image,
                                         im_w=still_size[0],
                                         im_h=still_size[1])

            responses = yield [self.add_image(video_id,
                                              tid,
                                              image=thumb,
                                              atype='thumbnail',
                                              reference_id=tid),
                               self.add_image(video_id,
                                              tid,
                                              image=still,
                                              atype='videostill',
                                              reference_id='still-%s'%tid)]
            raise tornado.gen.Return([x['id'] for x in responses])
        
        elif remote_url is not None:
            # Set the thumbnail as a remote url. If it is a neon
            # serving url, then add the requested size to the url
            thumb_url, is_thumb_neon_url = self._build_remote_url(
                remote_url, thumb_size)
            thumb_reference_id = tid
            if is_thumb_neon_url:
                thumb_reference_id = 'thumbservingurl-%s' % video_id
                
            still_url, is_still_neon_url = self._build_remote_url(
                remote_url, still_size)
            still_reference_id = tid
            if is_still_neon_url:
                still_reference_id = 'stillservingurl-%s' % video_id

            responses = yield [self.add_image(video_id,
                                              tid,
                                              remote_url=thumb_url,
                                              atype='thumbnail',
                                              reference_id=thumb_reference_id),
                               self.add_image(video_id,
                                              tid,
                                              remote_url=still_url,
                                              atype='videostill',
                                              reference_id=still_reference_id)]
            
            raise tornado.gen.Return([x['id'] for x in responses])

        else:
            raise TypeError('Either image or remote_url must be set')

    def _build_remote_url(self, url_base, size):
        '''Create a remote url. 

        If the base is a neon serving url, tack on the size params.

        returns (remote_url, is_neon_serving)
        '''
        remote_url = url_base
        neon_url_re = re.compile('/neonvid_[0-9a-zA-Z_\.]+$')
        is_neon_serving = neon_url_re.search(url_base) is not None
        if is_neon_serving:
            arams = zip(('width', 'height'), size)
            param_str = '&'.join(['%s=%i' % x for x in params if x[1]])
            if params_str:
                remote_url = '%s?%s' % (url_base, params_str)

        return remote_url, is_neon_serving

    ##########################################################################
    # Feed Processors
    ##########################################################################

    def get_video_url_to_download(self, b_json_item, frame_width=None):
        '''
        Return a video url to download from a brightcove json item 
        
        if frame_width is specified, get the closest one  
        '''

        video_urls = {}
        try:
            d_url  = b_json_item['FLVURL']
        except KeyError, e:
            _log.error("missing flvurl")
            return

        #If we get a broken response from brightcove api
        if not b_json_item.has_key('renditions'):
            return d_url

        renditions = b_json_item['renditions']
        for rend in renditions:
            f_width = rend["frameWidth"]
            url = rend["url"]
            video_urls[f_width] = url 
       
        # no renditions
        if len(video_urls.keys()) < 1:
            return d_url
        
        if frame_width:
            if video_urls.has_key(frame_width):
                return video_urls[frame_width] 
            closest_f_width = min(video_urls.keys(),
                                key=lambda x:abs(x-frame_width))
            return video_urls[closest_f_width]
        else:
            #return the max width rendition
            return video_urls[max(video_urls.keys())]

    def get_publisher_feed(self, command='find_all_videos', output='json',
                           page_no=0, page_size=100, callback=None):
    
        '''Get videos after the signup date, Iterate until you hit 
           video the publish date.
        
        Optimize with the latest video processed which is stored in the account

        NOTE: using customFields or specifying video_fields creates API delays
        '''

        data = {}
        data['command'] = command
        data['token'] = self.read_token
        data['media_delivery'] = 'http'
        data['output'] = output
        data['page_number'] = page_no 
        data['page_size'] = page_size
        data['sort_by'] = 'publish_date'
        data['get_item_count'] = "true"
        data['cache_buster'] = time.time() 

        url = self.format_get(self.read_url, data)
        req = tornado.httpclient.HTTPRequest(url=url,
                                             method="GET",
                                             request_timeout=60.0,
                                             connect_timeout=10.0)
        return BrightcoveApi.read_connection.send_request(req, callback)

    def process_publisher_feed(self, items, i_id):
        ''' process publisher feed for neon tags and generate brightcove
        thumbnail/still requests '''
        
        vids_to_process = [] 
        bc = cmsdb.neondata.BrightcovePlatform.get(
            self.neon_api_key, i_id)
        videos_processed = bc.get_videos() 
        if videos_processed is None:
            videos_processed = {} 
        
        #parse and get video ids to process
        '''
        - Get videos after a particular date
        - Check if they have already been queued up, else queue it 
        '''
        for item in items:
            to_process = False
            vid   = str(item['id'])
            title = item['name']
            #Check if neon has processed the videos already 
            if vid not in videos_processed:
                thumb  = item['thumbnailURL'] 
                still  = item['videoStillURL']
                try:
                    d_url  = item['FLVURL']
                except KeyError, e:
                    _log.error("missing flvurl for video %s" % vid)
                    continue
                length = item['length']

                d_url = self.get_video_url_to_download(item, 
                                bc.rendition_frame_width)

                if still is None:
                    still = thumb

                if thumb is None or still is None or length <0:
                    _log.info("key=process_publisher_feed" 
                                " msg=%s is a live feed" % vid)
                    continue

                if d_url is None:
                    _log.info("key=process_publisher_feed"
                                " msg=flv url missing for %s" % vid)
                    continue

                resp = self.format_neon_api_request(vid,
                                                    d_url,
                                                    prev_thumbnail=still,
                                                    request_type='topn',
                                                    i_id=i_id,
                                                    title=title)
                _log.info("creating request for video [topn] %s" % vid)
                if resp is not None and not resp.error:
                    #Update the videos in customer inbox
                    r = tornado.escape.json_decode(resp.body)
                    
                    def _update_account(bc):
                        bc.videos[vid] = r['job_id']
                        #publishedDate may be null, if video is unscheduled
                        bc.last_process_date = int(item['publishedDate']) /1000 if item['publishedDate'] else None
                    bp = cmsdb.neondata.BrightcovePlatform.modify(
                            self.neon_api_key, i_id, _update_account)
                else:
                    _log.error("failed to create request for vid %s" % vid)

            else:
                #Sync the changes in brightcove account to NeonDB
                #TODO: Sync not just the latest 100 videos
                job_id = bc.videos[vid]
                def _update_request(vid_request):
                    pub_date = int(item['publishedDate']) if item['publishedDate'] else None
                    vid_request.publish_date = pub_date 
                    vid_request.video_title = title
                request = cmsdb.neondata.NeonApiRequest.modify(
                    job_id, self.neon_api_key, _update_request)

    def sync_neondb_with_brightcovedb(self, items, i_id):
        ''' sync neondb with brightcove metadata '''        
        bp = cmsdb.neondata.BrightcovePlatform.get(
            self.neon_api_key, i_id)
        videos_processed = bp.get_videos() 
        if videos_processed is None:
            videos_processed = [] 
        
        for item in items:
            vid = str(item['id'])
            title = item['name']
            if vid in videos_processed:
                job_id = bp.videos[vid]
                def _update_request(vid_request):
                    pub_date = int(item['publishedDate']) if item['publishedDate'] else None
                    vid_request.publish_date = pub_date 
                    vid_request.video_title = title
                request = cmsdb.neondata.NeonApiRequest.modify(
                    job_id, self.neon_api_key, _update_request)

    def format_neon_api_request(self, id, video_download_url, 
                                prev_thumbnail=None, request_type='topn',
                                i_id=None, title=None, callback=None):
        ''' Format and submit reuqest to neon thumbnail api '''

        request_body = {}
        #brightcove tokens
        request_body["write_token"] = self.write_token
        request_body["read_token"] = self.read_token
        request_body["api_key"] = self.neon_api_key 
        request_body["video_id"] = str(id)
        request_body["video_title"] = str(id) if title is None else title 
        request_body["video_url"] = video_download_url
        request_body["callback_url"] = None #no callback required 
        request_body["autosync"] = self.autosync
        request_body["topn"] = 1
        request_body["integration_id"] = i_id 

        if request_type == 'topn':
            client_url = self.neon_uri + "brightcove"
            request_body["brightcove"] =1
            request_body["publisher_id"] = self.publisher_id
            if prev_thumbnail is not None:
                _log.debug("key=format_neon_api_request "
                        " msg=brightcove prev thumbnail not set")
            request_body['default_thumbnail'] = prev_thumbnail
        else:
            return
        
        body = tornado.escape.json_encode(request_body)
        h = tornado.httputil.HTTPHeaders({"content-type": "application/json"})
        req = tornado.httpclient.HTTPRequest(url = client_url,
                                             method = "POST",
                                             headers = h,
                                             body = body,
                                             request_timeout = 30.0,
                                             connect_timeout = 10.0)

        response = utils.http.send_request(req, ntries=1, callback=callback)
        if response and response.error:
            _log.error(('key=format_neon_api_request '
                        'msg=Error sending Neon API request: %s')
                        % response.error)

        return response

    def create_neon_api_requests(self, i_id, request_type='default'):
        ''' Create Neon Brightcove API Requests '''
        
        #Get publisher feed
        items_to_process = []  
        items_processed = [] #videos that Neon has processed
        done = False
        page_no = 0

        while not done: 
            count = 0
            response = self.get_publisher_feed(command='find_all_videos',
                                               page_no = page_no)
            if response.error:
                break
            json = tornado.escape.json_decode(response.body)
            page_no += 1
            try:
                items = json['items']
                total = json['total_count']
                psize = json['page_size']
                pno   = json['page_number']

            except Exception, e:
                _log.exception('key=create_neon_api_requests msg=%s' % e)
                return
            
            for item in items:
                pdate = int(item['publishedDate']) / 1000
                check_date = self.account_created if \
                        self.account_created is not None else self.last_publish_date
                if pdate > check_date:
                    items_to_process.append(item)
                    count += 1
                else:
                    items_processed.append(item)

            #if we have seen all items or if we have seen all the new
            #videos since last pub date
            if count < total or psize * (pno +1) > total:
                done = True

        #Sync video metadata of processed videos
        self.sync_neondb_with_brightcovedb(items_processed, i_id)

        if len(items_to_process) < 1 :
            return

        self.process_publisher_feed(items_to_process, i_id)
        return


    ## TODO: potentially replace find_all_videos by find_modified_videos ? 

    def create_requests_unscheduled_videos(self, i_id, page_no=0, page_size=25):
   
        '''
        ## Find videos scheduled in the future and process them
        ## Use this method as an additional call to check for videos 
        ## that are scheduled in the future
        # http://docs.brightcove.com/en/video-cloud/media/reference.html
        '''

        data = {}
        data['command'] = "find_modified_videos" 
        data['token'] = self.read_token
        data['media_delivery'] = 'http'
        data['output'] = 'json' 
        data['page_number'] = page_no 
        data['page_size'] = page_size
        #data['sort_by'] = 'modified_date'
        #data['sort_order'] = 'DESC'
        data['get_item_count'] = "true"
        data['video_fields'] =\
            "id,name,length,endDate,startDate,creationDate,publishedDate,lastModifiedDate,thumbnailURL,videoStillURL,FLVURL,renditions"
        data["from_date"] = 21492000
        data["filter"] = "UNSCHEDULED,INACTIVE"
        data['cache_buster'] = time.time() 

        url = self.format_get(self.read_url, data)
        req = tornado.httpclient.HTTPRequest(url=url,
                                             method = "GET",
                                             request_timeout = 60.0
                                             )
        response = BrightcoveApi.read_connection.send_request(req)
        if response.error:
            _log.error("key=create_requests_unscheduled_videos" 
                        " msg=Error getting unscheduled videos from "
                        "Brightcove: %s" % response.error)
            raise response.error
        items = tornado.escape.json_decode(response.body)

        #Logic to determine videos that may be not scheduled to run yet
        #publishedDate is null, and creationDate is recent (last 24 hrs) 
        #publishedDate can be null for inactive videos too

        #NOTE: There may be videos which are marked as not to use
        # by renaming the title  
        
        #check if requests for these videos have been created
        items_to_process = []

        for item in items['items']:
            if item['publishedDate'] is None or len(item['publishedDate']) ==0:
                items_to_process.append(item)
                _log.debug("key=create_requests_unscheduled_videos" 
                        " msg=creating request for vid %s" %item['id'])
        self.process_publisher_feed(items_to_process,i_id)

    ############## NEON API INTERFACE ########### 

    def create_video_request(self, video_id, i_id, create_callback):
        ''' Create neon api request for the particular video '''

        def get_vid_info(response):
            ''' vid info callback '''
            if not response.error and "error" not in response.body:
                data = tornado.escape.json_decode(response.body)
                try:
                    v_url = data["FLVURL"]
                except KeyError, e:
                    create_callback(response)
                    return

                still = data['videoStillURL']
                vid = str(data["id"])
                title = data["name"]
                self.format_neon_api_request(vid,
                                             v_url,
                                             still,
                                             request_type='topn',
                                             i_id=i_id,
                                             title=title,
                                             callback = create_callback)
            else:
                create_callback(response)

        self.find_video_by_id(video_id, get_vid_info)

    #### Verify Read token and create Requests during signup #####

    @tornado.gen.coroutine
    def verify_token_and_create_requests(self, i_id, n):
        '''
        Initial call when the brightcove account gets created
        verify the read token and create neon api requests
        #Sync version
        '''
        result = self.get_publisher_feed(command='find_all_videos',
                                         page_size = n) #get n videos

        if result and not result.error:
            bc = cmsdb.neondata.BrightcovePlatform.get(
                self.neon_api_key, i_id)
            if not bc:
                _log.error("key=verify_brightcove_tokens" 
                            " msg=account not found %s"%i_id)
                return
            vitems = tornado.escape.json_decode(result.body)
            items = vitems['items']
            keys = []
            #create request for each video 
            result = [] 
            for item in items:
                vid = str(item['id'])                              
                title = item['name']
                video_download_url = self.get_video_url_to_download(item)
                
                #NOTE: If a video doesn't have a thumbnail 
                #Perhaps a signle renedition video or a live feed
                if not item.has_key('videoStillURL'):
                    continue

                prev_thumbnail = item['videoStillURL'] #item['thumbnailURL']
                response = self.format_neon_api_request(vid,
                                                        video_download_url,
                                                        prev_thumbnail,
                                                        'topn',
                                                        i_id,
                                                        title)
                if not response.error:
                    vid = str(item['id'])
                    jid = tornado.escape.json_decode(response.body)
                    job_id = jid["job_id"]
                    item['job_id'] = job_id 
                    bc.videos[vid] = job_id 
                    result.append(item)
            #Update the videos in customer inbox
            res = bc.save()
            if not res:
                _log.error("key=verify_token_and_create_requests" 
                        " msg=customer inbox not updated %s" %i_id)
                raise tornado.gen.Return(result)

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def get_current_thumbnail_url(self, video_id):
        '''Method to retrieve the current thumbnail url on Brightcove
        
        Used by AB Test to keep track of any uploaded image to
        brightcove, its URL

        Inputs:
        video_id - The brightcove video id to get the urls for

        Returns thumb_url, still_url

        If there is an error, (None, None) is returned
        
        '''
        thumb_url = None
        still_url = None

        url = ('http://api.brightcove.com/services/library?' 
                'command=find_video_by_id&token=%s&media_delivery=http'
                '&output=json&video_id=%s'
                '&video_fields=videoStillURL%%2CthumbnailURL' %
                (self.read_token, video_id))

        req = tornado.httpclient.HTTPRequest(url=url,
                                             method="GET", 
                                             request_timeout=60.0,
                                             connect_timeout=10.0)
        response = yield tornado.gen.Task(
            BrightcoveApi.read_connection.send_request, req)

        if response.error:
            _log.error('key=get_current_thumbnail_url '
                       'msg=Error getting thumbnail for video id %s'%video_id)
            raise tornado.gen.Return((None, None))

        try:
            result = tornado.escape.json_decode(response.body)
            thumb_url = result['thumbnailURL'].split('?')[0]
            still_url = result['videoStillURL'].split('?')[0]
        except ValueError as e:
            _log.error('key=get_current_thumbnail_url '
                       'msg=Invalid JSON response from %s' % url)
            raise tornado.gen.Return((None, None))
        except KeyError:
            _log.error('key=get_current_thumbnail_url '
                       'msg=No valid url set for video id %s' % video_id)
            raise tornado.gen.Return((None, None))

        raise tornado.gen.Return((thumb_url, still_url))

    def create_request_from_playlist(self, pid, i_id):
        ''' create thumbnail api request given a video id 
            NOTE: currently we only need a sync version of this method

        '''

        url = 'http://api.brightcove.com/services/library?command=find_playlist_by_id' \
                '&token=%s&media_delivery=http&output=json&playlist_id=%s' %\
                (self.read_token, pid)
        req = tornado.httpclient.HTTPRequest(url=url,
                                             method="GET",
                                             request_timeout=60.0,
                                             connect_timeout=10.0)
        response = utils.http.send_request(req)
        if response.error:
            _log.error('key=create_request_from_playlist msg=Unable to get %s'
                       % url)
            return False
       
        json = tornado.escape.json_decode(response.body)
        try:
            items_to_process = json['videos']

        except ValueError, e:
            _log.exception('json error: %s' % e)
            return

        except Exception, e:
            _log.exception('unexpected error: %s' % e)
            return
            
        # Process the publisher feed
        self.process_publisher_feed(items_to_process, i_id)
        return

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def find_videos_by_ids(self, video_ids, video_fields=None,
                           media_delivery='http'):
        '''Finds many video information from the brightcove request.
        Inputs:
        video_ids - list of brightcove video ids to get info for
        video_fields - list of video fields to populate
        media_delivery - should urls be http, http_ios or default

        Outputs:
        A dictionary of video->{fields requested}
        '''
        results = {}

        MAX_VIDS_PER_REQUEST = 50
        
        for i in range(0, len(video_ids), MAX_VIDS_PER_REQUEST):
            url_params = {
                'command' : 'find_videos_by_ids',
                'token' : self.read_token,
                'video_ids' : ','.join(video_ids[i:(i+MAX_VIDS_PER_REQUEST)]),
                'media_delivery' : media_delivery,
                'output' : 'json'
                }
            if video_fields is not None:
                video_fields.append('id')
                url_params['video_fields'] = ','.join(set(video_fields))

            request = tornado.httpclient.HTTPRequest(
                '%s?%s' % (self.read_url, urllib.urlencode(url_params)),
                request_timeout = 60.0)
            
            response = yield tornado.gen.Task(
                BrightcoveApi.read_connection.send_request, request)

            if response.error:
                _log.error('Error calling find_videos_by_ids: %s' %
                           response.error)
                try:
                    json_data = json.load(response.buffer)
                    if json_data['code'] >= 200:
                        raise BrightcoveApiClientError(response.error)
                except ValueError:
                    # It's not JSON data so there was some other error
                    pass    
                except KeyError:
                    # It may be valid json but doesn't have a code
                    pass
                raise BrightcoveApiServerError(response.error)

            json_data = json.load(response.buffer)
            for item in json_data['items']:
                if item is not None:
                    results[item['id']] = item

        raise tornado.gen.Return(results)

class BrightcoveFeedIterator(object):
    '''An iterator that walks through entries from a Brightcove feed.

    Automatically deals with paging.

    If you want to do this iteration so that any calls are
    asynchronous, then you have to manually create a loop like:

    try:
      while True:
        item = yield iter.next(async=True)
    except StopIteration:
      pass      
    
    '''
    def __init__(self, command, token, request_pool, page_size=100,
                 output='json', max_items=None, **kwargs):
        '''Create an iterator

        Inputs:
        command - Command to execute
        token - The brightcove token to use
        request_pool - The request pool to use to send the request
        page_size - The size of each page when it is requested
        output - Output type as per the Brightcove API
        max_items - The maximum number of entries to return
        kwargs - Any other arguments to pass as url arguments to the command
        '''
        self.args = kwargs
        self.args['command'] = command
        self.args['token'] = token
        self.args['page_size'] = page_size
        self.args['output'] = output
        self.args['page_number'] = 0
        self.max_items = max_items
        self.page_data = []
        self.request_pool = request_pool
        self.items_returned = 0

    def __iter__(self):
        self.args['page_number'] = 0
        self.items_returned = 0
        return self

    @utils.sync.optional_sync
    @tornado.gen.coroutine
    def next(self):
        if self.items_returned >= self.max_items:
            raise StopIteration()
        
        if len(self.page_data) == 0:
            # Get more entries
            request = tornado.httpclient.HTTPRequest(
                ('http://api.brightcove.com/services/library?%s' %
                 urllib.urlencode(self.args)),
                method='GET',
                request_timeout=60.0)
            response = yield tornado.gen.Task(self.request_pool,
                                              request)
            if response.error:
                if response.error.code > 500:
                    raise BrightcoveApiServerError(
                        'Error getting entries from Brightcove %s: %s' % 
                        (request.url, response.error))
                else:
                    raise BrightcoveApiClientError(
                        'Client error getting entries from Brightcove %s: %s' %
                        (request.url, response.error))
            self.args['page_number'] += 1
                
            json_data = json.load(response.body)
            self.page_data = json_data['items']
            self.page_data.reverse()

        if len(self.page_data) == 0:
            # We've gotten all the data
            raise StopIteration()

        self.items_returned += 1
        raise tornado.gen.Return(self.page_data.pop())
