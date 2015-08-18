#!/usr/bin/env python

import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

import datetime
import json
import hashlib
import PIL.Image as Image
import logging
import os
import random
import signal
import time

import tornado.httpserver
import tornado.ioloop
import tornado.web
import tornado.escape
import tornado.gen
import tornado.httpclient

import traceback

import utils.neon
import utils.logs
import utils.http

from StringIO import StringIO
from cmsdb import neondata
from utils.inputsanitizer import InputSanitizer
from utils import statemon
import utils.sync
from utils.options import define, options
from voluptuous import Schema, Required, All, Length, Range, MultipleInvalid, Invalid, Coerce

import logging
_log = logging.getLogger(__name__)
import uuid

define("port", default=8084, help="run on the given port", type=int)

class ResponseCode(object): 
    HTTP_OK = 200
    HTTP_ACCEPTED = 202 
    HTTP_BAD_REQUEST = 400 
    HTTP_INTERNAL_SERVER_ERROR = 500
    HTTP_NOT_IMPLEMENTED = 501

class APIV2Sender(object): 
    def success(self, data, code=ResponseCode.HTTP_OK):
        self.set_status(code) 
        self.write(data) 
        self.finish()

    def error(self, message, extra_data=None, code=ResponseCode.HTTP_BAD_REQUEST): 
        error_json = {} 
        error_json['message'] = message 
        self.set_status(code)
        if extra_data: 
            error_json['fields'] = extra_data 
        self.write(error_json)
        self.finish() 
 

class APIV2Handler(tornado.web.RequestHandler, APIV2Sender):
    def initialize(self): 
        self.set_header("Content-Type", "application/json")
        self.api_key = self.request.headers.get('X-Neon-API-Key')

    def parse_args(self):
        args = {} 
        # if we have query_arguments only use them 
        if self.request.query_arguments is not None: 
            for key, value in self.request.query_arguments.iteritems():
                #TODO hack(and it's gonna break on a double value passed in)
                # let's use Coerce inside voluptuous instead of using isdigit
                args[key] = int(value[0]) if value[0].isdigit() else value[0]
        # otherwise let's use what we find in the body
        elif self.request.body_arguments is not None: 
            for key, value in self.request.body_arguments.iteritems():
                args[key] = int(value[0]) if value[0].isdigit() else value[0]

        return args

    @tornado.gen.coroutine 
    def get(self, *args):
        self.error('get not implemented', code=ResponseCode.HTTP_NOT_IMPLEMENTED)

    __get = get
 
    @tornado.gen.coroutine 
    def post(self, *args):
        self.error('post not implemented', code=ResponseCode.HTTP_NOT_IMPLEMENTED)

    __post = post

    @tornado.gen.coroutine 
    def put(self, *args):
        self.error('put not implemented', code=ResponseCode.HTTP_NOT_IMPLEMENTED)

    __put = put

    @tornado.gen.coroutine 
    def delete(self, *args):
        self.error('delete not implemented', code=ResponseCode.HTTP_NOT_IMPLEMENTED)

    __delete = delete

'''****************************************************************
NewAccountHandler : class responsible for creating a new account
   HTTP Verbs     : post 
****************************************************************'''
class NewAccountHandler(APIV2Handler):
    @tornado.gen.coroutine 
    def post(self):
        schema = Schema({ 
          Required('customer_name') : All(str, Length(min=1, max=1024)),
          'default_width': All(int, Range(min=1, max=8192)), 
          'default_height': All(int, Range(min=1, max=8192)),
          'default_thumbnail_id': All(str, Length(min=1, max=2048)) 
        })
        try:
            args = self.parse_args()
            schema(args) 
            user = neondata.NeonUserAccount(customer_name=args['customer_name'])
            try:
                user.default_size = list(user.default_size) 
                user.default_size[0] = args.get('default_width', neondata.DefaultSizes.WIDTH)
                user.default_size[1] = args.get('default_height', neondata.DefaultSizes.HEIGHT)
                user.default_size = tuple(user.default_size)
                user.default_thumbnail_id = args.get('default_thumbnail_id', None)
            except KeyError as e: 
                pass 
            output = yield tornado.gen.Task(neondata.NeonUserAccount.save, user)
            user = yield tornado.gen.Task(neondata.NeonUserAccount.get, user.neon_api_key)
            self.success(user.to_json())
        except MultipleInvalid as e:
            self.error('%s %s' % (e.path[0], e.msg)) 
        
'''*****************************************************************
AccountHandler : class responsible for updating and getting accounts 
   HTTP Verbs  : get, put 
*****************************************************************'''
class AccountHandler(APIV2Handler):
    '''**********************
    AccountHandler.get
    **********************'''    
    @tornado.gen.coroutine
    def get(self, account_id):
        schema = Schema({ 
          Required('account_id') : All(str, Length(min=1, max=256)),
        }) 
        try:
            args = {} 
            args['account_id'] = str(account_id)  
            schema(args) 
            user_account = yield tornado.gen.Task(neondata.NeonUserAccount.get, args['account_id'])
            # we don't want to send back everything, build up object of what we want to send back 
            rv_account = {} 
            rv_account['tracker_account_id'] = user_account.tracker_account_id
            rv_account['account_id'] = user_account.account_id 
            rv_account['staging_tracker_account_id'] = user_account.staging_tracker_account_id 
            rv_account['default_thumbnail_id'] = user_account.default_thumbnail_id 
            rv_account['integrations'] = user_account.integrations
            rv_account['default_size'] = user_account.default_size
            rv_account['created'] = user_account.created 
            rv_account['updated'] = user_account.updated
            output = json.dumps(rv_account)
            self.success(output) 
        except MultipleInvalid as e:  
            self.error('%s %s' % (e.path[0], e.msg))
        except Exception as e:  
            self.error('could not retrieve the account', {'account_id': account_id})  
 
    '''**********************
    AccountHandler.put
    **********************'''    
    @tornado.gen.coroutine
    def put(self, account_id):
        schema = Schema({ 
          Required('account_id') : All(str, Length(min=1, max=256)),
          'default_width': All(int, Range(min=1, max=8192)), 
          'default_height': All(int, Range(min=1, max=8192)),
          'default_thumbnail_id': All(str, Length(min=1, max=2048)) 
        })
        try:
            args = self.parse_args()
            args['account_id'] = str(account_id)
            schema(args)
            acct = yield tornado.gen.Task(neondata.NeonUserAccount.get, args['account_id'])
            def _update_account(a):
                try: 
                    a.default_size = list(a.default_size) 
                    a.default_size[0] = args.get('default_width', acct.default_size[0])
                    a.default_size[1] = args.get('default_height', acct.default_size[1])
                    a.default_size = tuple(a.default_size)
                    a.default_thumbnail_id = args.get('default_thumbnail_id', a.default_thumbnail_id) 
                except KeyError as e: 
                    pass 
            result = yield tornado.gen.Task(neondata.NeonUserAccount.modify, acct.key, _update_account)
            output = result.to_json()
            self.success(output)
        except MultipleInvalid as e: 
            self.error('%s %s' % (e.path[0], e.msg))
        except Exception as e:  
            self.error('could not update the account', {'account_id': account_id})  

'''*********************************************************************
IntegrationHelper : class responsible for helping the integration 
                    handlers, create the integration, update the 
                    integration, and add strategies to accounts based on 
                    the integration type

Method list: createIntegration, updateIntegration, addExperimentStrategy  
*********************************************************************'''
class IntegrationHelper():
    @staticmethod 
    @tornado.gen.coroutine
    def createIntegration(acct, args, integration_type):
        def _createOoyalaIntegration(p):
            try:
                p.account_id = acct.neon_api_key
                p.partner_code = args['publisher_id'] 
                p.ooyala_api_key = args.get('ooyala_api_key', None)
                p.api_secret = args.get('ooyala_api_secret', None)
                p.auto_update = args.get('autosync', False)
            except KeyError as e: 
                pass 
             
        def _createBrightcoveIntegration(p):
            try:
                current_time = time.time()
                p.account_id = acct.neon_api_key
                p.publisher_id = args['publisher_id'] 
                p.read_token = args.get('read_token', None)
                p.write_token = args.get('write_token', None)
                p.callback_url = args.get('callback_url', None)
            except KeyError as e: 
                pass
 
        integration_id = uuid.uuid1().hex
        if integration_type == neondata.IntegrationType.OOYALA: 
            platform = yield tornado.gen.Task(neondata.OoyalaIntegration.modify, 
                                              acct.neon_api_key, integration_id, 
                                              _createOoyalaIntegration, 
                                              create_missing=True) 
        elif integration_type == neondata.IntegrationType.BRIGHTCOVE:
            platform = yield tornado.gen.Task(neondata.BrightcoveIntegration.modify, 
                                              acct.neon_api_key, integration_id, 
                                              _createBrightcoveIntegration, 
                                              create_missing=True) 
        
        result = yield tornado.gen.Task(acct.modify, 
                                        acct.neon_api_key, 
                                        lambda p: p.add_platform(platform))
        
        # ensure the platform made it to the database by executing a get
        if integration_type == neondata.IntegrationType.OOYALA: 
            platform = yield tornado.gen.Task(neondata.OoyalaIntegration.get,
                                              acct.neon_api_key,
                                              integration_id)
        elif integration_type == neondata.IntegrationType.BRIGHTCOVE:
            platform = yield tornado.gen.Task(neondata.BrightcoveIntegration.get,
                                              acct.neon_api_key,
                                              integration_id)


        if platform: 
            raise tornado.gen.Return(platform)
        else: 
            raise SaveError('unable to save the integration')

    @staticmethod 
    @tornado.gen.coroutine
    def addStrategy(acct, integration_type): 
         if integration_type == neondata.IntegrationType.OOYALA: 
             strategy = neondata.ExperimentStrategy(acct.neon_api_key) 
         elif integration_type == neondata.IntegrationType.BRIGHTCOVE: 
             strategy = neondata.ExperimentStrategy(acct.neon_api_key)
         result = yield tornado.gen.Task(strategy.save)  
         if result: 
             raise tornado.gen.Return(result)
         else: 
             raise SaveError('unable to save strategy to account')
    
    @staticmethod 
    @tornado.gen.coroutine
    def getIntegration(apiv2, account_id, integration_type): 
        schema = Schema({
          Required('account_id') : All(str, Length(min=1, max=256)),
          Required('integration_id') : All(str, Length(min=1, max=256))
        })
        args = apiv2.parse_args()
        args['account_id'] = str(account_id)
        schema(args)
        integration_id = args['integration_id'] 
        if integration_type == neondata.IntegrationType.OOYALA: 
            platform = yield tornado.gen.Task(neondata.OoyalaIntegration.get, 
                                              args['account_id'], 
                                              integration_id)
        elif integration_type == neondata.IntegrationType.BRIGHTCOVE: 
            platform = yield tornado.gen.Task(neondata.BrightcoveIntegration.get, 
                                              args['account_id'], 
                                              args['integration_id'])
        if platform: 
            raise tornado.gen.Return(platform) 
        else: 
            raise GetError('%s %s' % ('unable to find the integration for id:',integration_id))

    @staticmethod 
    @tornado.gen.coroutine
    def addVideoResponses(account): 
        return ""

'''*********************************************************************
OoyalaIntegrationHandler : class responsible for creating/updating/
                           getting an ooyala integration
   HTTP Verbs            : get, post, put
*********************************************************************'''
class OoyalaIntegrationHandler(APIV2Handler):
 
    '''**********************
    OoyalaIntegration.post
    **********************'''    
    @tornado.gen.coroutine
    def post(self, account_id): 
        schema = Schema({
          Required('account_id') : All(str, Length(min=1, max=256)),
          Required('publisher_id') : All(Coerce(str), Length(min=1, max=256)),
          'ooyala_api_key': All(str, Length(min=1, max=1024)), 
          'ooyala_api_secret': All(str, Length(min=1, max=1024)), 
          'autosync': All(int, Range(min=0, max=1))
        })
        try: 
            args = self.parse_args()
            args['account_id'] = str(account_id)
            schema(args)
            acct = yield tornado.gen.Task(neondata.NeonUserAccount.get, args['account_id'])
            platform = yield tornado.gen.Task(IntegrationHelper.createIntegration, acct, args, neondata.IntegrationType.OOYALA)
            yield tornado.gen.Task(IntegrationHelper.addStrategy, acct, neondata.IntegrationType.OOYALA)
            self.success(platform.to_json()) 
        except SaveError as e: 
            self.error(e.msg, {'account_id': account_id, 'publisher_id': args['publisher_id']}, e.code)  
        except AttributeError as e:  
            self.error(e.msg, {'account_id': account_id, 'publisher_id': args['publisher_id']})  
        except MultipleInvalid as e: 
            self.error('%s %s' % (e.path[0], e.msg))

    '''**********************
    OoyalaIntegration.get
    **********************'''    
    @tornado.gen.coroutine
    def get(self, account_id):
        try: 
            platform = yield tornado.gen.Task(IntegrationHelper.getIntegration, 
                                              self,
                                              account_id,  
                                              neondata.IntegrationType.OOYALA) 
            self.success(platform.to_json())
        except GetError as e:
            self.error('error getting the integration')  
        except AttributeError as e:  
            self.error('error getting the integration', {'account_id': account_id})
        except MultipleInvalid as e: 
            self.error('%s %s' % (e.path[0], e.msg))
 
    '''**********************
    OoyalaIntegration.put
    **********************'''    
    @tornado.gen.coroutine
    def put(self, account_id):
        try: 
            schema = Schema({
              Required('account_id') : All(str, Length(min=1, max=256)),
              Required('integration_id') : All(str, Length(min=1, max=256)),
              'ooyala_api_key': All(str, Length(min=1, max=1024)), 
              'ooyala_api_secret': All(str, Length(min=1, max=1024)), 
              'publisher_id': All(str, Length(min=1, max=1024))
            })
            args = self.parse_args()
            args['account_id'] = str(account_id)
            schema(args)

            def _update_platform(p):
                try:
                    p.ooyala_api_key = args['ooyala_api_key'] 
                    p.api_secret = args['ooyala_api_secret'] 
                    p.partner_code = args['publisher_id'] 
                except KeyError as e: 
                    pass
 
            result = yield tornado.gen.Task(neondata.OoyalaIntegration.modify, 
                                         args['account_id'], 
                                         args['integration_id'], 
                                         _update_platform)

            ooyala_integration = yield tornado.gen.Task(neondata.OoyalaIntegration.get, 
                                                     args['account_id'], 
                                                     args['integration_id']) 
            self.success(ooyala_integration.to_json())
             
        except AttributeError as e:  
            self.error('error updating the integration', {'integration_id': integration_id})

        except MultipleInvalid as e:
            self.error('%s %s' % (e.path[0], e.msg))

'''*********************************************************************
BrightcoveIntegrationHandler : class responsible for creating/updating/
                               getting a brightcove integration
   HTTP Verbs                : get, post, put
*********************************************************************'''
class BrightcoveIntegrationHandler(APIV2Handler):
 
    '''*********************
    BrightcoveIntegration.post 
    *********************'''   
    @tornado.gen.coroutine
    def post(self, account_id):
        schema = Schema({
          Required('account_id') : All(str, Length(min=1, max=256)),
          Required('publisher_id') : All(Coerce(str), Length(min=1, max=256)),
          'read_token': All(str, Length(min=1, max=1024)), 
          'write_token': All(str, Length(min=1, max=1024))
        })
        try: 
            args = self.parse_args()
            args['account_id'] = str(account_id)
            schema(args)
            acct = yield tornado.gen.Task(neondata.NeonUserAccount.get, args['account_id'])
            platform = yield tornado.gen.Task(IntegrationHelper.createIntegration, 
                                              acct, 
                                              args, 
                                              neondata.IntegrationType.BRIGHTCOVE)
            yield tornado.gen.Task(IntegrationHelper.addStrategy, 
                                   acct, 
                                   neondata.IntegrationType.BRIGHTCOVE)
            self.success(platform.to_json())
        except SaveError as e:
            self.error(e.msg, {'account_id' : account_id, 'publisher_id' : publisher_id}, e.code)  
        except MultipleInvalid as e: 
            self.error('%s %s' % (e.path[0], e.msg))
        except Exception as e:  
            self.error(e.msg, {'account_id' : account_id, 'publisher_id' : publisher_id})  

    '''*********************
    BrightcoveIntegration.get 
    *********************'''    
    @tornado.gen.coroutine
    def get(self, account_id):  
        try: 
            platform = yield tornado.gen.Task(IntegrationHelper.getIntegration, 
                                              self,
                                              account_id,  
                                              neondata.IntegrationType.BRIGHTCOVE) 
            self.success(platform.to_json())
        except GetError as e: 
            self.error(e.msg) 
        except MultipleInvalid as e:
            self.error('%s %s' % (e.path[0], e.msg))
        except Exception as e: 
            self.error(e.msg, {'account_id' : account_id}) 
 
    '''*********************
    BrightcoveIntegration.put 
    *********************'''    
    @tornado.gen.coroutine
    def put(self, account_id):
        try:   
            schema = Schema({
              Required('account_id') : All(str, Length(min=1, max=256)),
              Required('integration_id') : All(str, Length(min=1, max=256)),
              'read_token': All(str, Length(min=1, max=1024)), 
              'write_token': All(str, Length(min=1, max=1024)), 
              'publisher_id': All(str, Length(min=1, max=1024))
            })
            args = self.parse_args()
            args['account_id'] = account_id = str(account_id)
            integration_id = args['integration_id'] 
            schema(args)

            def _update_platform(p):
                try:
                    p.read_token = args['read_token'] 
                    p.write_token = args['write_token'] 
                    p.publisher_id = args['publisher_id'] 
                except KeyError as e: 
                    pass
 
            result = yield tornado.gen.Task(neondata.BrightcoveIntegration.modify, 
                                         account_id, 
                                         integration_id, 
                                         _update_platform)

            platform = yield tornado.gen.Task(neondata.BrightcoveIntegration.get, 
                                              account_id, 
                                              integration_id) 
            self.success(platform.to_json())
        except MultipleInvalid as e: 
            self.error('%s %s' % (e.path[0], e.msg))
        except Exception as e:  
            self.error('unable to update integration', {'integration_id' : integration_id}) 

'''*********************************************************************
ThumbnailHandler : class responsible for creating/updating/getting a
                   thumbnail 
HTTP Verbs       : get, post, put
*********************************************************************'''
class ThumbnailHandler(APIV2Handler):
    @tornado.gen.coroutine
    def post(self, account_id):
        schema = Schema({
          Required('account_id') : All(str, Length(min=1, max=256)),
          Required('video_id') : All(str, Length(min=1, max=256)),
          Required('thumbnail_location') : All(str, Length(min=1, max=2048))
        })
        try:
            args = self.parse_args()
            args['account_id'] = account_id_api_key = str(account_id)
            schema(args)
            video_id = args['video_id'] 
            internal_video_id = neondata.InternalVideoID.generate(account_id_api_key,video_id)

            video = yield tornado.gen.Task(neondata.VideoMetadata.get, internal_video_id)

            current_thumbnails = yield tornado.gen.Task(neondata.ThumbnailMetadata.get_many,
                                                        video.thumbnail_ids)
            cdn_key = neondata.CDNHostingMetadataList.create_key(account_id_api_key,
                                                                 video.integration_id)
            cdn_metadata = yield tornado.gen.Task(neondata.CDNHostingMetadataList.get,
                                                  cdn_key)
            # ranks can be negative 
            min_rank = 1
            for thumb in current_thumbnails:
                if (thumb.type == neondata.ThumbnailType.CUSTOMUPLOAD and
                    thumb.rank < min_rank):
                    min_rank = thumb.rank
            cur_rank = min_rank - 1
 
            new_thumbnail = neondata.ThumbnailMetadata(None,
                                                       internal_vid=internal_video_id, 
                                                       ttype=neondata.ThumbnailType.CUSTOMUPLOAD, 
                                                       rank=cur_rank)
            # upload image to cdn 
            yield video.download_and_add_thumbnail(new_thumbnail,
                                                   args['thumbnail_location'],
                                                   cdn_metadata,
                                                   async=True)
            #save the thumbnail
            new_thumbnail.save() 

            # save the video 
            new_video = yield tornado.gen.Task(neondata.VideoMetadata.modify, 
                                               internal_video_id, 
                                               lambda x: x.thumbnail_ids.append(new_thumbnail.key))

            if new_video: 
                self.success('{ "message": "thumbnail accepted for processing" }', code=ResponseCode.HTTP_ACCEPTED)  
            else: 
                self.error('unable to save thumbnail to video', {'thumbnail_location' : args['thumbnail_location']})  
              
        except MultipleInvalid as e: 
            self.error('%s %s' % (e.path[0], e.msg))
        except Exception as e:  
            self.error('unable to add thumbnail', {'thumbnail_location' : args['thumbnail_location']}) 

    @tornado.gen.coroutine
    def put(self, account_id): 
        schema = Schema({
          Required('account_id') : All(str, Length(min=1, max=256)),
          Required('thumbnail_id') : All(str, Length(min=1, max=512)),
          'enabled': All(int, Range(min=0, max=1))
        })
        try:
            args = self.parse_args()
            args['account_id'] = account_id_api_key = str(account_id)
            schema(args)
            thumbnail_id = args['thumbnail_id'] 
            
            thumbnail = yield tornado.gen.Task(neondata.ThumbnailMetadata.get, 
                                               thumbnail_id)
            def _update_thumbnail(t):
                try:
                    t.enabled = bool(args.get('enabled', thumbnail.enabled))
                except KeyError as e: 
                    pass

            yield tornado.gen.Task(neondata.ThumbnailMetadata.modify, 
                                   thumbnail_id, 
                                   _update_thumbnail)

            thumbnail = yield tornado.gen.Task(neondata.ThumbnailMetadata.get, 
                                               thumbnail_id)
 
            self.success(json.dumps(thumbnail.__dict__))

        except MultipleInvalid as e: 
            self.error('%s %s' % (e.path[0], e.msg))
        except Exception as e:
            self.error('unable to update thumbnail', {'account_id': account_id, 'thumbnail_id': thumbnail_id})  
 
    @tornado.gen.coroutine
    def get(self, account_id): 
        schema = Schema({
          Required('account_id') : All(str, Length(min=1, max=256)),
          Required('thumbnail_id') : All(str, Length(min=1, max=512))
        })
        try:
            args = self.parse_args()
            args['account_id'] = account_id_api_key = str(account_id)
            schema(args)
            thumbnail_id = args['thumbnail_id'] 
            thumbnail = yield tornado.gen.Task(neondata.ThumbnailMetadata.get, 
                                               thumbnail_id) 
            self.success(json.dumps(thumbnail.__dict__))

        except MultipleInvalid as e: 
            self.error('%s %s' % (e.path[0], e.msg))

        except Exception as e:
            self.error('unable to get thumbnail', {'account_id': account_id, 'thumbnail_id': thumbnail_id})  


'''*********************************************************************
VideoHelper      : helper class responsible for creating new video jobs 
                   for any and all integration types
*********************************************************************'''
class VideoHelper():
    @staticmethod 
    @tornado.gen.coroutine
    def addVideo(request, account_id):
        schema = Schema({
          Required('account_id') : All(str, Length(min=1, max=256)),
          Required('external_video_ref') : All(str, Length(min=1, max=512)),
          'integration_id' : All(str, Length(min=1, max=256)),
          'video_url': All(str, Length(min=1, max=512)), 
          'callback_url': All(str, Length(min=1, max=512)), 
          'video_title': All(str, Length(min=1, max=256)),
          'default_thumbnail_url': All(str, Length(min=1, max=128)),
          'thumbnail_ref': All(str, Length(min=1, max=512))
        })
        args = parse_args(request)
        args['account_id'] = str(account_id)
        schema(args)
        account_id_api_key = args['account_id'] 
        integration_id = args.get('integration_id', None) 
 
        user_account = yield tornado.gen.Task(neondata.NeonUserAccount.get, account_id_api_key)

        def _createNeonApiRequest(r): 
            job_id = uuid.uuid1().hex
            r.job_id = job_id
            r.api_key = account_id_api_key 
            r.video_id = args['external_video_ref'] 
            if integration_id: 
                r.integration_id = integration_id
                r.integration_type = user_account.integrations[integration_id]
            r.video_url = args.get('video_url', None) 
            r.callback_url = args.get('callback_url', None)
            r.video_title = args.get('video_title', None) 
            r.default_thumbnail = args.get('default_thumbnail_url', None) 
            r.external_thumbnail_ref = args.get('thumbnail_ref', None)  
            
        api_request = yield tornado.gen.Task(neondata.NeonApiRequest.modify, 
                                             job_id, 
                                             _createNeonApiRequest, 
                                             create_missing=True) 
        result = api_request      
        # result = add the job
        # this should be done with a call to video_server.add_job, or via http
        # add the video, save the default thumbnail
        
        if result: 
            raise tornado.gen.Return(result) 
        else: 
            raise SaveError('unable to add to video, unable to process')

    @staticmethod 
    @tornado.gen.coroutine
    def getThumbnailsFromIds(tids):
        if tids: 
            thumbnails = yield tornado.gen.Task(neondata.VideoMetadata.get_many, 
                                                tids)
            thumbnails = [obj.__dict__ for obj in thumbnails] 

        raise tornado.gen.Return(thumbnails)
     
'''*********************************************************************
VideoHandler     : class responsible for creating/updating/getting a
                   video
HTTP Verbs       : get, post, put
*********************************************************************'''
class VideoHandler(APIV2Handler):
    '''**********************
    Video.post 
    **********************'''    
    @tornado.gen.coroutine
    def post(self, account_id):
        try:
            video = yield tornado.gen.Task(VideoHelper.addVideo, 
                                           self.request, 
                                           account_id) 
            self.success(video.to_json())
        except MultipleInvalid as e: 
            self.error('%s %s' % (e.path[0], e.msg))
        except SaveError as e: 
            self.error(e.msg, code=e.code) 
        except Exception as e:
            self.error('unable to create video request', {'account_id': account_id})  
    
    '''**********************
    Video.get 
    **********************'''    
    @tornado.gen.coroutine
    def get(self, account_id):  
        try: 
            schema = Schema({
              Required('account_id') : All(str, Length(min=1, max=256)),
              Required('video_id') : All(str, Length(min=1, max=4096)),
              'fields': All(str, Length(min=1, max=4096))
            })
            args = self.parse_args()
            args['account_id'] = account_id_api_key = str(account_id)
            schema(args)
            video_id = args['video_id']
            fields = args.get('fields', None) 
            
            vid_dict = {} 
            output_list = []
            internal_video_ids = [] 
            video_ids = video_id.split(',')
            for v_id in video_ids: 
                internal_video_id = neondata.InternalVideoID.generate(account_id_api_key,v_id)
                internal_video_ids.append(internal_video_id)
 
            videos = yield tornado.gen.Task(neondata.VideoMetadata.get_many, 
                                            internal_video_ids) 
            if videos:  
               new_videos = [] 
               if fields:
                   field_set = set(fields.split(','))
                   for obj in videos:
                       obj = obj.__dict__
                       new_video = {} 
                       for field in field_set: 
                           if field == 'thumbnails':
                               new_video['thumbnails'] = yield VideoHelper.getThumbnailsFromIds(obj['thumbnail_ids'])
                           elif field in obj: 
                               new_video[field] = obj[field] 
                       if new_video: 
                          new_videos.append(new_video)
               else: 
                   new_videos = [obj.__dict__ for obj in videos] 

               vid_dict['videos'] = new_videos
               vid_dict['video_count'] = len(new_videos)

            self.success(json.dumps(vid_dict))

        except MultipleInvalid as e:
            self.error('%s %s' % (e.path[0], e.msg))
        except Exception as e:
            self.error('unable to get videos', {'account_id': account_id, 'video_id': video_id})  
 
    '''**********************
    Video.put 
    **********************'''    
    @tornado.gen.coroutine
    def put(self, account_id):
        try: 
            schema = Schema({
              Required('account_id') : All(str, Length(min=1, max=256)),
              Required('video_id') : All(str, Length(min=1, max=256)),
              'testing_enabled': All(int, Range(min=0, max=1))
            })
            args = self.parse_args()
            args['account_id'] = account_id_api_key = str(account_id)
            schema(args)

            abtest = bool(args['testing_enabled'])
            internal_video_id = neondata.InternalVideoID.generate(account_id_api_key,args['video_id']) 
            def _update_video(v): 
                v.testing_enabled = abtest
            result = yield tornado.gen.Task(neondata.VideoMetadata.modify, 
                                            internal_video_id, 
                                            _update_video)
            video = yield tornado.gen.Task(neondata.VideoMetadata.get, 
                                            internal_video_id)

            self.success(json.dumps(video.__dict__))

        except MultipleInvalid as e:
            self.error('%s %s' % (e.path[0], e.msg)) 
        except Exception as e:
            self.error('unable to update video', {'account_id': account_id})  

'''*********************************************************************
OptimizelyIntegrationHandler : class responsible for creating/updating/
                               getting an optimizely integration 
HTTP Verbs                   : get, post, put
Notes                        : not yet implemented, likely phase 2
*********************************************************************'''
class OptimizelyIntegrationHandler(tornado.web.RequestHandler):
    def __init__(self): 
        super(OptimizelyIntegrationHandler, self).__init__() 

'''*********************************************************************
LiveStreamHandler : class responsible for creating a new live stream job 
   HTTP Verbs     : post
        Notes     : outside of scope of phase 1, future implementation
*********************************************************************'''
class LiveStreamHandler(tornado.web.RequestHandler): 
    def __init__(self): 
        super(OptimizelyIntegrationHandler, self).__init__() 

'''*********************************************************************
Controller Defined Exceptions 
*********************************************************************'''
class Error(Exception): 
    pass 

class SaveError(Error): 
    def __init__(self, msg, code=ResponseCode.HTTP_INTERNAL_SERVER_ERROR): 
        self.msg = msg
        self.code = code 

class GetError(Error): 
    def __init__(self, msg): 
        self.msg = msg

'''*********************************************************************
Endpoints 
*********************************************************************'''
application = tornado.web.Application([
    (r'/api/v2/accounts/?$', NewAccountHandler),
    (r'/api/v2/accounts/([a-zA-Z0-9]+)/?$', AccountHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/integrations/ooyala/?$', OoyalaIntegrationHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/integrations/brightcove/?$', BrightcoveIntegrationHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/integrations/optimizely/?$', OptimizelyIntegrationHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/thumbnails/?$', ThumbnailHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/videos/?$', VideoHandler),
    (r'/api/v2/([a-zA-Z0-9]+)/?$', AccountHandler),
    (r'/api/v2/(\d+)/jobs/live_stream', LiveStreamHandler)
], gzip=True)

def main():
    global server
    print "API V2 is running" 
    signal.signal(signal.SIGTERM, lambda sig, y: sys.exit(-sig))
    server = tornado.httpserver.HTTPServer(application)
    server.listen(options.port)
    tornado.ioloop.IOLoop.current().start()

if __name__ == "__main__":
    utils.neon.InitNeon()
    main()
