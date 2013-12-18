#!/usr/bin/env python

'''
Video processing client unit test

1. Neon api request
2. Brightcoev api request 
3. Brightcove api request with autosync

Inject failures
- error downloading video file
- error with video file
- error with few thumbnails
- error with client callback

'''

import os.path
import sys
base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..',
                                         '..'))
if sys.path[0] <> base_path:
        sys.path.insert(0,base_path)

import json
import logging
import model
import mock
import os
import subprocess
import random
import re
import request_template
import unittest
import urllib
import utils
import test_utils
import test_utils.mock_boto_s3 as boto_mock

from boto.s3.connection import S3Connection
from mock import patch
from mock import MagicMock
from PIL import Image
from supportServices import neondata
from StringIO import StringIO
from api import client
from utils.options import define, options
from tornado.httpclient import HTTPResponse, HTTPRequest, HTTPError

_log = logging.getLogger(__name__)

class TestVideoClient(unittest.TestCase):
    '''
    NOTE: In this test the database calls have been mocked out
    
    The test is mostly monolithic to save time on video processing,
    asserts have messages to indicate the failure points

    '''
    def setUp(self):
        #setup properties,model
        #TODO: options
        self.model_file = "../../model_data/20130924.model"
        self.model_version = "test" 
        self.model = model.load_model(self.model_file)
        self.test_video_file = "test.mp4" #~8sec video
   
        self.dl = None
        self.pv = None

        #mock s3
        self.patcher = patch('api.client.S3Connection')
        mock_conn = self.patcher.start()
        self.s3conn = boto_mock.MockConnection()
        mock_conn.return_value = self.s3conn

    #ProcessVideo setup
    def processvideo_setup(self):
        #httpdownload
        jparams = request_template.neon_api_request %("j","v","ak","neon","ak","j")
        params = json.loads(jparams)
        self.dl = client.HttpDownload(jparams, None, self.model, self.model_version)
        self.pv = client.ProcessVideo(params, jparams, 
                self.model, self.model_version, False,123)
        self.dl.pv = self.pv
        nthumbs = params['api_param']
        self.pv.process_all(self.test_video_file,nthumbs)

    def tearDown(self):
        self.patcher.stop()

    def _create_random_image(self):
        h = 360
        w = 480
        pixels = [(0,0,0) for _w in range(h*w)]
        r = random.randrange(0,255)
        g = random.randrange(0,255)
        b = random.randrange(0,255)
        pixels[0] = (r,g,b)
        im = Image.new("RGB",(h,w))
        im.putdata(pixels)
        imgstream = StringIO()
        im.save(imgstream, "jpeg", quality=100)
        imgstream.seek(0)
        data = imgstream.read()
        return imgstream

    def _dequeue_job(self,request_type):
        #Mock/ Job template
        pass
#if request_type == "neon"

    @patch('api.client.S3Connection')
    def test_process_all(self,mock_conntype):
        
        #s3mocks to mock host_thumbnails_to_s3
        conn = boto_mock.MockConnection()
        mock_conntype.return_value = conn
        conn.create_bucket('host-thumbnails')
        conn.create_bucket('neon-beta-test')

        self.processvideo_setup()
        
        #verify metadata has been populated
        for key,value in self.pv.video_metadata.iteritems():
            self.assertNotEqual(value,None)
       
        #verify that following maps get populated
        self.assertGreater(len(self.pv.data_map),0,"Model did not return values")
        self.assertGreater(len(self.pv.attr_map),0,"Model did not return values")
        self.assertGreater(len(self.pv.timecodes),0,"Model did not return values")
        
        #HttpDownload
        #Mock Database call NeonApiRequest.get
        self.nplatform_patcher = patch('api.client.NeonApiRequest')
        self.mock_nplatform_patcher = self.nplatform_patcher.start()
        self.mock_nplatform_patcher.get.side_effect = [
                neondata.NeonApiRequest("d","d",None,None,None,None,None)]

        #send client response & verify
        self.dl.send_client_response()
        s3_keys = [x for x in conn.buckets['host-thumbnails'].get_all_keys()]
        self.assertEqual(len(s3_keys),1,"send client resposne and host images s3")

        #save data to s3
        self.pv.save_data_to_s3()
        s3_keys = [x for x in conn.buckets['neon-beta-test'].get_all_keys()]
        self.assertEqual(len(s3_keys),3,"Save data to s3")

        # TEST Brightcove request flow and finalize_brightcove_request() 
        # Replace the request parameters of the dl & pv objects to save time on
        # video processing and reuse the setup
        vid  = "vid123"
        i_id = "i123"
        bp = neondata.BrightcovePlatform("testaccountneonapi",i_id)
        api_key = bp.neon_api_key
        bp.save()

        jparams = request_template.brightcove_api_request %("j",vid,api_key,
                            "brightcove",api_key,"j",i_id)
        params = json.loads(jparams)
        self.pv.request_map = params
        self.pv.request = jparams
        self.dl.job_params = params
        
        #brightcove platform patcher
        self.bplatform_patcher = patch('api.client.BrightcoveApiRequest')
        self.mock_bplatform_patcher = self.bplatform_patcher.start()
        breq = neondata.BrightcoveApiRequest("d","d",None,None,None,None,None,None)
        breq.previous_thumbnail = "http://prevthumb"
        self.mock_bplatform_patcher.get.side_effect = [breq]
       
        #mock tornado http
        request = HTTPRequest('http://google.com')
        response = HTTPResponse(request, 200, buffer=self._create_random_image())
        clientp = patch('api.client.tornado.httpclient.HTTPClient')
        http_patcher = clientp.start()
        http_patcher().fetch.side_effect = [response,response]
        
        self.dl.send_client_response()
        bcove_thumb = False
        for key in conn.buckets['host-thumbnails'].get_all_keys():
            if "brightcove" in key.name:
                bcove_thumb = True  
        
        self.assertTrue(bcove_thumb,"finalize brightcove request")        
        
        #verify thumbnail metadata and video metadata 
        vm = neondata.VideoMetadata.get(api_key+"_"+vid)
        self.assertNotEqual(vm,None,"assert videometadata")
        
        #TODO: Brightcove request with autosync
    
        #cleanup
        http_patcher.stop()
        self.mock_nplatform_patcher.stop()
        self.mock_bplatform_patcher.stop()
    
    #TODO: test request finalizers independently
    
    #TODO: test intermittent DB/ processing failure cases

    #TODO: test streaming callback and async callback

if __name__ == '__main__':
    unittest.main()
