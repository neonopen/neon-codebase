#!/usr/bin/env python
import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..',
                                             '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

import api.brightcove_api
from cStringIO import StringIO
from cmsdb import neondata
from cmsdb.neondata import ThumbnailMetadata, ThumbnailType, VideoMetadata
from cvutils.imageutils import PILImageUtils
import datetime
import integrations
import integrations.brightcove
import json
import logging
from mock import patch, MagicMock
import multiprocessing
import test_utils.neontest
import test_utils.postgresql
import tornado.gen
import tornado.httpclient
import tornado.testing
import unittest
import utils.neon
from utils.options import define, options


class TestUpdateExistingThumb(test_utils.neontest.AsyncTestCase):
    def setUp(self):
        self.submit_mocker = patch('integrations.ovp.cmsapiv2.client')
        self.submit_mock = self._future_wrap_mock(
            self.submit_mocker.start().Client().send_request)
        self.submit_mock.side_effect = \
          lambda x, **kwargs: tornado.httpclient.HTTPResponse(
              x, 201, buffer=StringIO('{"job_id": "job1"}'))
        # Mock out the image download
        self.im_download_mocker = patch(
            'cvutils.imageutils.PILImageUtils.download_image')
        self.random_image = PILImageUtils.create_random_image(480, 640)
        self.im_download_mock = self._future_wrap_mock(
            self.im_download_mocker.start())
        self.im_download_mock.side_effect = [self.random_image]

        # Mock out the image upload
        self.cdn_mocker = patch('cmsdb.cdnhosting.CDNHosting')
        self.cdn_mock = self._future_wrap_mock(
            self.cdn_mocker.start().create().upload)
        self.cdn_mock.return_value = [('some_cdn_url.jpg', 640, 480)]

        # Mock out the brightcove api and build the platform
        mock_bc_api = MagicMock()
        self.platform = neondata.BrightcoveIntegration('acct1')
        self.platform.get_api = lambda: mock_bc_api
        self.integration = integrations.brightcove.BrightcoveIntegration(
            'acct1', self.platform)
        
        # Add a video to the database
        vid = VideoMetadata('acct1_v1',
                            ['acct1_v1_n1', 'acct1_v1_bc1'],
                            i_id=self.platform.integration_id, 
                            request_id='job1')
        vid.save()
        #self.platform.add_video('v1', 'job1')
        ThumbnailMetadata('acct1_v1_n1', 'acct1_v1',
                          ttype=ThumbnailType.NEON, rank=1).save()

        neondata.NeonApiRequest('job1', 'acct1', 'v1', 'Original title').save()

        super(test_utils.neontest.AsyncTestCase, self).setUp()

    def tearDown(self):
        self.im_download_mocker.stop()
        self.cdn_mocker.stop()
        self.submit_mocker.stop()
        self.postgresql.clear_all_tables()
        super(test_utils.neontest.AsyncTestCase, self).tearDown()

    @classmethod
    def setUpClass(cls):
        dump_file = '%s/cmsdb/migrations/cmsdb.sql' % (__base_path__)
        cls.postgresql = test_utils.postgresql.Postgresql(dump_file=dump_file)

    @classmethod
    def tearDownClass(cls): 
        cls.postgresql.stop()

    @tornado.testing.gen_test
    def test_no_video_in_db(self):
        with self.assertLogExists(logging.WARNING, 'No VideoMetadata for'):
            yield self.integration.submit_one_video_object(
                { 'id' : 'v2',
                  'length' : 100,
                  'FLVURL' : 'http://video.mp4',
                  'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                  'videoStill' : {
                      'id' : 'still_id',
                      'referenceId' : None,
                      'remoteUrl' : None
                  },
                  'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
                  'thumbnail' : {
                      'id' : 123456,
                      'referenceId' : None,
                      'remoteUrl' : None
                  }
                }
                )

    @tornado.testing.gen_test
    def test_no_thumb_data(self):
        yield self.integration.submit_one_video_object(
                { 'id' : 'v1',
                  'length' : 100,
                  'FLVURL' : 'http://video.mp4',
                  'thumbnailURL' : None,
                  'thumbnail' : {
                      'id' : 123456,
                      'referenceId' : None,
                      'remoteUrl' : None
                  }
                }
                )

        # Make sure no image was uploaded
        self.assertEquals(self.im_download_mock.call_count, 0)
        self.assertEquals(self.cdn_mock.call_count, 0)

    @tornado.testing.gen_test
    def test_match_urls(self):
        ThumbnailMetadata('acct1_v1_bc1', 'acct1_v1',
                          ['http://bc.com/vid_still.jpg'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=1).save()

        yield self.integration.submit_one_video_object(
            { 'id' : 'v1',
              'length' : 100,
              'FLVURL' : 'http://video.mp4',
              'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
              'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : None,
                  'remoteUrl' : None
              },
              'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
              'thumbnail' : {
                  'id' : 123456,
                  'referenceId' : None,
                  'remoteUrl' : None
                  }
                  }
            )

        # Check that the external id was set
        self.assertEquals(ThumbnailMetadata.get('acct1_v1_bc1').external_id,
                          'still_id')

        # Make sure no image was uploaded
        self.assertEquals(self.im_download_mock.call_count, 0)
        self.assertEquals(self.cdn_mock.call_count, 0)

    @tornado.testing.gen_test
    def test_match_moved_bc_urls(self):
        ThumbnailMetadata('acct1_v1_bc1', 'acct1_v1',
                          ['http://bcsecure01-a.akamaihd.net/4/vid_still.jpg'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=1).save()

        yield self.integration.submit_one_video_object({
            'id' : 'v1',
            'length' : 100,
            'FLVURL' : 'http://video.mp4',
            'videoStillURL' : 'http://brightcove.com/3/vid_still.jpg?x=5',
            'videoStill' : {
                'id' : 'still_id',
                'referenceId' : None,
                'remoteUrl' : None
            },
            'thumbnailURL' : 'http://brightcove.com/3/thumb_still.jpg?x=8',
            'thumbnail' : {
                'id' : 123456,
                'referenceId' : None,
                'remoteUrl' : None
                }
                })
        

        # Check that the external id was set
        self.assertEquals(ThumbnailMetadata.get('acct1_v1_bc1').external_id,
                          'still_id')

        # Make sure no image was uploaded
        self.assertEquals(self.im_download_mock.call_count, 0)
        self.assertEquals(self.cdn_mock.call_count, 0)

    @tornado.testing.gen_test
    def test_match_remote_urls(self):
        ThumbnailMetadata('acct1_v1_bc1', 'acct1_v1',
                          ['http://some_remote_still?c=90'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=1).save()
        yield self.integration.submit_one_video_object({
            'id' : 'v1',
            'length' : 100,
            'FLVURL' : 'http://video.mp4',
            'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
            'videoStill' : {
                'id' : 'still_id',
                'referenceId' : None,
                'remoteUrl' : 'http://some_remote_still'
                },
            'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
            'thumbnail' : {
                'id' : 'thumb_id',
                'referenceId' : None,
                'remoteUrl' : None
                }
            }
        )

        # Check that the external id was set
        self.assertEquals(ThumbnailMetadata.get('acct1_v1_bc1').external_id,
                          'still_id')

        # Make sure no image was uploaded
        self.assertEquals(self.im_download_mock.call_count, 0)
        self.assertEquals(self.cdn_mock.call_count, 0)

    @tornado.testing.gen_test
    def test_match_reference_id(self):
        ThumbnailMetadata('acct1_v1_bc1', 'acct1_v1',
                          ['http://bc.com/some_moved_location'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=1,
                          refid='my_thumb_ref').save()
        yield self.integration.submit_one_video_object({
            'id' : 'v1',
            'length' : 100,
            'FLVURL' : 'http://video.mp4',
            'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
            'videoStill' : {
                'id' : 'still_id',
                'referenceId' : 'my_still_ref',
                'remoteUrl' : None
            },
            'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
            'thumbnail' : {
                'id' : 'thumb_id',
                'referenceId' : 'my_thumb_ref',
                'remoteUrl' : None
            }
            }
        )

        # Check that the external id was set
        self.assertEquals(ThumbnailMetadata.get('acct1_v1_bc1').external_id,
                          'still_id')

        # Make sure no image was uploaded
        self.assertEquals(self.im_download_mock.call_count, 0)
        self.assertEquals(self.cdn_mock.call_count, 0)

    @tornado.testing.gen_test
    def test_match_external_id(self):
        ThumbnailMetadata('acct1_v1_bc1', 'acct1_v1',
                          ['http://bc.com/some_moved_location'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=1,
                          refid='my_thumb_ref',
                          external_id=123456).save()
        
        yield self.integration.submit_one_video_object({
            'id' : 'v1',
            'length' : 100,
            'FLVURL' : 'http://video.mp4',
            'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
            'videoStill' : {
                'id' : 'still_id',
                'referenceId' : None,
                'remoteUrl' : None
                },
            'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
            'thumbnail' : {
                'id' : 123456,
                'referenceId' : None,
                'remoteUrl' : None
                }
                }
            )

        # Make sure no image was uploaded
        self.assertEquals(self.im_download_mock.call_count, 0)
        self.assertEquals(self.cdn_mock.call_count, 0)

    @tornado.testing.gen_test
    def test_match_external_id_string(self):
        ThumbnailMetadata('acct1_v1_bc1', 'acct1_v1',
                          ['http://bc.com/some_moved_location'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=1,
                          refid='my_thumb_ref',
                          external_id='123456').save()
        
        yield self.integration.submit_one_video_object({
            'id' : 'v1',
            'length' : 100,
            'FLVURL' : 'http://video.mp4',
            'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
            'videoStill' : {
                'id' : 'still_id',
                'referenceId' : None,
                'remoteUrl' : None
                },
            'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
            'thumbnail' : {
                'id' : 123456,
                'referenceId' : None,
                'remoteUrl' : None
                }
                }
            )

        # Make sure no image was uploaded
        self.assertEquals(self.im_download_mock.call_count, 0)
        self.assertEquals(self.cdn_mock.call_count, 0)

    
    @tornado.testing.gen_test
    def test_update_title_and_published_date(self):
        ThumbnailMetadata('acct1_v1_bc1', 'acct1_v1',
                          ['http://bc.com/some_moved_location'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=1,
                          refid='my_thumb_ref',
                          external_id='123456').save()
        
        yield self.integration.submit_one_video_object({
            'id' : 'v1',
            'length' : 100,
            'FLVURL' : 'http://video.mp4',
            'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
            'publishedDate' : "1439768747000",
            'name' : 'A new title',
            'videoStill' : {
                'id' : 'still_id',
                'referenceId' : None,
                'remoteUrl' : None
                },
            'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
            'thumbnail' : {
                'id' : 123456,
                'referenceId' : None,
                'remoteUrl' : None
                }
                }
            )

        # Make sure no image was uploaded
        self.assertEquals(self.im_download_mock.call_count, 0)
        self.assertEquals(self.cdn_mock.call_count, 0)

        # Check the video object
        video = neondata.VideoMetadata.get('acct1_v1')
        self.assertEquals(video.publish_date, '2015-08-16T23:45:47')

        # Check the request object
        req = neondata.NeonApiRequest.get(video.job_id, 'acct1')
        self.assertEquals(req.publish_date, '2015-08-16T23:45:47')
        self.assertEquals(req.video_title, 'A new title')

    @tornado.testing.gen_test
    def test_new_thumb_found(self):
        ThumbnailMetadata('acct1_v1_bc1', 'acct1_v1',
                          ['http://bc.com/some_old_thumb.jpg'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=1,
                          refid='my_thumb_ref',
                          external_id='old_thumb_id').save()
        ThumbnailMetadata('acct1_v1_bc2', 'acct1_v1',
                          ['http://bc.com/some_newer_thumb.jpg'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=0,
                          external_id='old_thumb_id2').save()
        VideoMetadata('acct1_v1',
                      ['acct1_v1_n1', 'acct1_v1_bc1', 'acct1_v1_bc2'],
                      i_id='i1').save()
        yield self.integration.submit_one_video_object({
            'id' : 'v1',
            'length': 100,
            'FLVURL' : 'http://video.mp4',
            'videoStillURL' : 'http://bc.com/new_still.jpg?x=8',
            'videoStill' : {
                'id' : 1234568,
                'referenceId' : None,
                'remoteUrl' : None
            },
            'thumbnailURL' : 'http://bc.com/new_thumb.jpg?x=8',
            'thumbnail' : {
                'id' : 'thumb_id',
                'referenceId' : None,
                'remoteUrl' : None
            }
            })


        # Make sure a new image was added to the database
        video_meta = VideoMetadata.get('acct1_v1')
        self.assertEquals(len(video_meta.thumbnail_ids), 4)
        thumbs = ThumbnailMetadata.get_many(video_meta.thumbnail_ids)
        for thumb in thumbs:
            if thumb.key not in ['acct1_v1_bc1', 'acct1_v1_n1',
                                 'acct1_v1_bc2']:
                self.assertEquals(thumb.rank, -1)
                self.assertEquals(thumb.type, ThumbnailType.DEFAULT)
                self.assertEquals(thumb.urls, [
                    'some_cdn_url.jpg',
                    'http://bc.com/new_still.jpg?x=8'])
                self.assertEquals(thumb.external_id, '1234568')

        # Make sure the new image was uploaded
        self.im_download_mock.assert_called_with(
            'http://bc.com/new_still.jpg?x=8')
        self.assertGreater(self.cdn_mock.call_count, 0)

    @tornado.testing.gen_test
    def test_new_thumbnail_but_same_photo(self):
        ThumbnailMetadata('acct1_v1_bc1', 'acct1_v1',
                          ['http://bc.com/some_old_thumb.jpg'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=1,
                          refid=None,
                          external_id='4562077467001').save()
        ThumbnailMetadata('acct1_v1_bc2', 'acct1_v1',
                          ['http://bc.com/some_newer_thumb.jpg'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=0,
                          external_id='4562077467001').save()
        VideoMetadata('acct1_v1',
                      ['acct1_v1_n1', 'acct1_v1_bc1', 'acct1_v1_bc2'],
                      i_id='i1').save()

        yield self.integration.submit_one_video_object(
            { 'id' : 'v1',
              'length' : 100,
              'FLVURL' : 'http://video.mp4',
              'videoStillURL' : 'http://r.ddmcdn.com/s_f/o_1/DSC/uploads/2015/10/150813.032.01.197_20151016_103245.jpg',
              'videoStill' : {
                  'id' : '4562077467002',
                  'referenceId' : None,
                  'remoteUrl' : 'http://r.ddmcdn.com/s_f/o_1/DSC/uploads/2015/10/150813.032.01.197_20151016_103245.jpg'
              },
              'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
              'thumbnail' : {
                  'id' : '4562076241002',
                  'referenceId' : None,
                  'remoteUrl' : 'http://r.ddmcdn.com/s_f/o_1/DSC/uploads/2015/10/150813.032.01.197_20151016_103245.jpg'
              }
            }
            )

        # Make sure the url is loaded, image uploaded.
        self.im_download_mock.assert_called_with(
            'http://r.ddmcdn.com/s_f/o_1/DSC/uploads/2015/10/150813.032.01.197_20151016_103245.jpg')
        self.assertGreater(self.cdn_mock.call_count, 0)

        # Reset the future wrap
        self.im_download_mock.result_mock()
        self.im_download_mock.side_effect = [self.random_image]
        # Make sure a new image was added to the database
        # The first one is added, but not the second one
        video_meta = VideoMetadata.get('acct1_v1')
        self.assertEquals(len(video_meta.thumbnail_ids), 4)
        thumbs = ThumbnailMetadata.get_many(video_meta.thumbnail_ids)
        for thumb in thumbs:
            if thumb.key not in ['acct1_v1_bc1', 'acct1_v1_n1',
                                 'acct1_v1_bc2']:
                self.assertEquals(thumb.rank, -1)
                self.assertEquals(thumb.type, ThumbnailType.DEFAULT)
                self.assertEquals(thumb.urls, [
                    'some_cdn_url.jpg',
                    'http://r.ddmcdn.com/s_f/o_1/DSC/uploads/2015/10/150813.032.01.197_20151016_103245.jpg'])
                self.assertEquals(thumb.external_id, '4562077467002')

        yield self.integration.submit_one_video_object(
            { 'id' : 'v1',
              'length' : 100,
              'FLVURL' : 'http://video.mp4',
              'videoStillURL' : 'http://second.jpg',
              'videoStill' : {
                  'id' : '4562077467003',
                  'referenceId' : None,
                  'remoteUrl' : None
              },
              'thumbnailURL' : None,
              'thumbnail' : {
                  'id' : '4562076241003',
                  'referenceId' : None,
                  'remoteUrl' : None
              }
            }
            )

        self.im_download_mock.assert_called_with('http://second.jpg')
        self.assertGreater(self.cdn_mock.call_count, 0)

        # Make sure a new image was added to the database
        # The first one is added, but not the second one
        video_meta = VideoMetadata.get('acct1_v1')
        self.assertEquals(len(video_meta.thumbnail_ids), 4)
        thumbs = ThumbnailMetadata.get_many(video_meta.thumbnail_ids)
        for thumb in thumbs:
            if thumb.key not in ['acct1_v1_bc1', 'acct1_v1_n1',
                                 'acct1_v1_bc2']:
                # Validate the last thumbnail is not added.
                self.assertNotEquals(thumb.external_id, '4562077467003')
                self.assertNotEquals(thumb.external_id, '4562076241003')


    @tornado.testing.gen_test
    def test_error_downloading_image(self):
        self.im_download_mock.side_effect = [IOError('Image Download Error')]
        ThumbnailMetadata('acct1_v1_bc1', 'acct1_v1',
                          ['http://bc.com/some_old_thumb.jpg'],
                          ttype=ThumbnailType.DEFAULT,
                          rank=1,
                          refid='my_thumb_ref',
                          external_id='old_thumb_id').save()
        
        with self.assertLogExists(logging.ERROR, 'Could not find valid image'):
            yield self.integration.submit_one_video_object({
                'id' : 'v1',
                'length' : 100,
                'FLVURL' : 'http://video.mp4',
                'videoStillURL' : None,
                'videoStill' : None,
                'thumbnailURL' : 'http://bc.com/new_thumb.jpg?x=8',
                'thumbnail' : {
                    'id' : 'thumb_id',
                    'referenceId' : None,
                    'remoteUrl' : None
                }
                }
                )

        # Make sure there was no change to the database
        video_meta = VideoMetadata.get('acct1_v1')
        self.assertEquals(len(video_meta.thumbnail_ids), 2)

        self.im_download_mock.assert_called_with(
            'http://bc.com/new_thumb.jpg?x=8')
        self.assertEquals(self.cdn_mock.call_count, 0)

class TestSubmitVideo(test_utils.neontest.AsyncTestCase):
    def setUp(self):
        # Mock out the call to services
        self.submit_mocker = patch('integrations.ovp.cmsapiv2.client')
        self.submit_mock = self._future_wrap_mock(
            self.submit_mocker.start().Client().send_request)
        self.submit_mock.side_effect = \
          lambda x, **kwargs: tornado.httpclient.HTTPResponse(
              x, 201, buffer=StringIO('{"job_id": "job1"}'))
        

        # Create the platform object
        self.platform = neondata.BrightcoveIntegration.modify(
            'acct1', lambda x: x, create_missing=True)
        self.integration = integrations.brightcove.BrightcoveIntegration(
            'a1', self.platform)

        self.maxDiff = None

        super(test_utils.neontest.AsyncTestCase, self).setUp()

    def tearDown(self):
        self.submit_mocker.stop()
        self.postgresql.clear_all_tables()
        super(test_utils.neontest.AsyncTestCase, self).tearDown()

    @classmethod
    def setUpClass(cls):
        dump_file = '%s/cmsdb/migrations/cmsdb.sql' % (__base_path__)
        cls.postgresql = test_utils.postgresql.Postgresql(dump_file=dump_file)

    @classmethod
    def tearDownClass(cls): 
        cls.postgresql.stop()

    def _get_video_submission(self):
        '''Returns, the url, parsed json submition'''
        cargs, kwargs = self.submit_mock.call_args

        response = cargs[0]
        return response.url, json.loads(response.body)

    @tornado.testing.gen_test
    def test_unexpected_error(self):
        self.submit_mock.side_effect = [
            Exception('You did something very bad')
            ]

        with self.assertLogExists(logging.ERROR, 'Unexpected error'):
            with self.assertRaises(Exception):
                yield self.integration.submit_one_video_object(
                    { 'id' : 'v1',
                      'referenceId': None,
                      'name' : 'Some video',
                      'length' : 100,
                      'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                      'videoStill' : {
                          'id' : 'still_id',
                          'referenceId' : None,
                          'remoteUrl' : None
                      },
                      'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
                      'thumbnail' : {
                          'id' : 123456,
                          'referenceId' : None,
                          'remoteUrl' : None
                      },
                      'FLVURL' : 'http://video.mp4'
                      })

    @tornado.testing.gen_test
    def test_submission_error(self):
        self.submit_mock.side_effect = \
          lambda x, **kwargs: tornado.httpclient.HTTPResponse(
              x, 500, error=tornado.httpclient.HTTPError(500))

        with self.assertLogExists(logging.ERROR, 'Error submitting video'):
            with self.assertRaises(integrations.ovp.CMSAPIError):
                yield self.integration.submit_one_video_object(
                    { 'id' : 'v1',
                      'referenceId': None,
                      'name' : 'Some video',
                      'length' : 100,
                      'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                      'videoStill' : {
                          'id' : 'still_id',
                          'referenceId' : None,
                          'remoteUrl' : None
                      },
                      'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
                      'thumbnail' : {
                          'id' : 123456,
                          'referenceId' : None,
                          'remoteUrl' : None
                      },
                      'FLVURL' : 'http://video.mp4'
                      })

    @tornado.testing.gen_test
    def test_submit_video_already_submitted(self):
        self.submit_mock.side_effect = \
          lambda x, **kwargs: tornado.httpclient.HTTPResponse(
              x, 409, buffer=StringIO(
                  '{"error":"duplicate job", "job_id": "job2"}'))

        with self.assertLogExists(logging.WARNING, 'Video .* already exists'):
            job_id = yield self.integration.submit_one_video_object(
                { 'id' : 'v1',
                'referenceId': None,
                'name' : 'Some video',
                'length' : 100,
                'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : None,
                  'remoteUrl' : None
                },
                'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
                'thumbnail' : {
                  'id' : 123456,
                  'referenceId' : None,
                  'remoteUrl' : None
                },
                'FLVURL' : 'http://video.mp4'
                })

        self.assertEquals(job_id, 'job2')
        
    @tornado.testing.gen_test
    def test_submit_old_video(self):
        self.platform.oldest_video_allowed = '2015-01-01'
        self.integration.skip_old_videos = True 
        with self.assertLogExists(logging.INFO, 'Skipped video.*old'):
            job_id = yield self.integration.submit_one_video_object(
                { 'id' : 'v1',
                'referenceId': None,
                'name' : 'Some video',
                'length' : 100,
                'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : None,
                  'remoteUrl' : None
                },
                'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
                'thumbnail' : {
                  'id' : 123456,
                  'referenceId' : None,
                  'remoteUrl' : None
                },
                'FLVURL' : 'http://video.mp4',
                'publishedDate' : "1413657557000"
                })

        self.assertIsNone(job_id)

    @tornado.testing.gen_test
    def test_submit_typical_bc_video(self):
        job_id = yield self.integration.submit_one_video_object(
            { 'id' : 123456789,
              'referenceId': None,
              'name' : 'Some video',
              'length' : 100,
              'publishedDate' : "1439768747000",
              'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
              'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : None,
                  'remoteUrl' : None
              },
              'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
              'thumbnail' : {
                  'id' : 123456,
                  'referenceId' : None,
                  'remoteUrl' : None
              },
              'FLVURL' : 'http://video.mp4'
            })

        self.assertIsNotNone(job_id)
        
        url, submission = self._get_video_submission()
        self.assertEquals(
            url, '/api/v2/a1/videos')
        self.assertEquals(
            submission,
            {'integration_id' : self.integration.platform.integration_id, 
             'external_video_ref': '123456789',
             'url': 'http://video.mp4',
             'title': 'Some video',
             'default_thumbnail_url': 'http://bc.com/vid_still.jpg?x=5',
             'thumbnail_ref': 'still_id',
             'custom_data': { '_bc_int_data' :
                              { 'bc_id' : 123456789, 'bc_refid': None }},
             'duration' : 0.1,
             'publish_date' : '2015-08-16T23:45:47'
             })

    @tornado.testing.gen_test
    def test_servingurl_push_callback(self):
        with options._set_bounded(
                'integrations.brightcove.bc_servingurl_push_callback_host',
                '10.1.2.3'):
            job_id = yield self.integration.submit_one_video_object(
                { 'id' : 123456789,
                  'referenceId': None,
                  'name' : 'Some video',
                  'length' : 100,
                  'publishedDate' : "1439768747000",
                  'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                  'videoStill' : {
                      'id' : 'still_id',
                      'referenceId' : None,
                      'remoteUrl' : None
                  },
                  'thumbnailURL' : 'http://bc.com/thumb_still.jpg?x=8',
                  'thumbnail' : {
                      'id' : 123456,
                      'referenceId' : None,
                      'remoteUrl' : None
                  },
                  'FLVURL' : 'http://video.mp4'
                })

            self.assertIsNotNone(job_id)

            url, submission = self._get_video_submission()
            self.assertEquals(submission['callback_url'],
                              'http://10.1.2.3/update_serving_url/%s' %
                              self.platform.integration_id)

    @tornado.testing.gen_test
    def test_submit_video_using_custom_id_field(self):
        def _set_id_field(x):
            x.id_field = 'mediaapiid'
        self.platform = neondata.BrightcoveIntegration.modify(
            self.platform.integration_id, _set_id_field)
        self.integration.platform = self.platform

        # Try a video with a reference id
        job_id = yield self.integration.submit_one_video_object(
            { 'integration_id': self.platform.integration_id, 
              'id' : 'v1',
              'referenceId': 'video_ref',
              'name' : 'Some video',
              'length' : 100,
              'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
              'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : None,
                  'remoteUrl' : None
              },
              'FLVURL' : 'http://video.mp4',
              'customFields' : {
                  'mediaapiid' : 465972,
              }
            })
        self.assertIsNotNone(job_id)

        url, submission = self._get_video_submission()
        self.assertDictEqual(
            submission,
            {'integration_id': self.platform.integration_id, 
             'external_video_ref': '465972',
             'url': 'http://video.mp4',
             'title': 'Some video',
             'default_thumbnail_url': 'http://bc.com/vid_still.jpg?x=5',
             'thumbnail_ref': 'still_id',
             'custom_data': { '_bc_int_data' :
                              { 'bc_id' : 'v1', 'bc_refid': 'video_ref' },
                              'mediaapiid' : 465972
                              },
             'duration' : 0.1
             })

    @tornado.testing.gen_test
    def test_submit_live_video_feeds(self):
        with self.assertLogExists(logging.WARNING,
                                  'Video ID .* for account .* is a '
                                  'live stream'):
            job_id = yield self.integration.submit_one_video_object(
                    { 'id' : 'v1',
                      'length' : -1,
                      'FLVURL' : 'http://video.mp4'
                      })
            self.assertIsNone(job_id)
        logging.getLogger('integrations.ovp').reset_sample_counters()

        with self.assertLogExists(logging.WARNING,
                                  'Video ID .* for account .* is a '
                                  'live stream'):
            job_id = yield self.integration.submit_one_video_object(
                    { 'id' : 'v1',
                      'length' : 0,
                      'FLVURL' : 'http://video.m3u8'
                      })
            self.assertIsNone(job_id)
        logging.getLogger('integrations.ovp').reset_sample_counters()

        with self.assertLogExists(logging.WARNING,
                                  'Video ID .* for account .* is a '
                                  'live stream'):
            job_id = yield self.integration.submit_one_video_object(
                    { 'id' : 'v1',
                      'length' : 0,
                      'FLVURL' : 'http://video.csmil'
                      })
            self.assertIsNone(job_id)
        logging.getLogger('integrations.ovp').reset_sample_counters()

        with self.assertLogExists(logging.WARNING,
                                  'Video ID .* for account .* is a '
                                  'live stream'):
            job_id = yield self.integration.submit_one_video_object(
                    { 'id' : 'v1',
                      'length' : 0,
                      'FLVURL' : 'rtmp://video.mp4'
                      })
            self.assertIsNone(job_id)
        logging.getLogger('integrations.ovp').reset_sample_counters()


class TestChooseDownloadUrl(test_utils.neontest.TestCase):
    def setUp(self):      
        # Create the platform object
        self.platform = neondata.BrightcoveIntegration('acct1')
        self.integration = integrations.brightcove.BrightcoveIntegration(
            'a1', self.platform)

        super(TestChooseDownloadUrl, self).setUp()

    def test_no_renditions(self):
        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4'
            })
        self.assertEquals(url, 'http://video.mp4')

    def test_no_url(self):
        with self.assertLogExists(logging.ERROR, 'missing flvurl .*'):
            self.assertIsNone(self.integration._get_video_url_to_download({}))

    def test_empty_renditions(self):
        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4',
            'renditions' : []
            })
        self.assertEquals(url, 'http://video.mp4')

    def test_rendition_largest_size(self):
        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4',
            'renditions' : [
                { 'frameWidth' : 640,
                  'encodingRate' : 855611,
                  'url' : 'http://video_640.mp4'},
                { 'frameWidth' : 1280,
                  'encodingRate' : 855611,
                  'url' : 'http://video_1280.mp4'},
                { 'frameWidth' : 320,
                  'encodingRate' : 855611,
                  'url' : 'http://video_200.mp4'},
                ]
            })
        self.assertEquals(url, 'http://video_1280.mp4')

    def test_rendition_higher_encoding_rate(self):
        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4',
            'renditions' : [
                { 'frameWidth' : 640,
                  'encodingRate' : 855611,
                  'url' : 'http://video_85.mp4'},
                { 'frameWidth' : 640,
                  'encodingRate' : 1796130,
                  'url' : 'http://video_17.mp4'},
                { 'frameWidth' : 640,
                  'encodingRate' : 1196130,
                  'url' : 'http://video_11.mp4'},
                ]
            })
        self.assertEquals(url, 'http://video_17.mp4')

    def test_specific_platform_width(self):
        self.integration.platform.rendition_frame_width = 640

        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4',
            'renditions' : [
                { 'frameWidth' : 640,
                  'encodingRate' : 855611,
                  'url' : 'http://video_640.mp4'},
                { 'frameWidth' : 1280,
                  'encodingRate' : 855611,
                  'url' : 'http://video_1280.mp4'},
                { 'frameWidth' : 320,
                  'encodingRate' : 855611,
                  'url' : 'http://video_200.mp4'},
                ]
            })
        self.assertEquals(url, 'http://video_640.mp4')

    def test_specific_platform_width_higher_encoding_rate(self):
        self.integration.platform.rendition_frame_width = 640

        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4',
            'renditions' : [
                { 'frameWidth' : 640,
                  'encodingRate' : 855611,
                  'url' : 'http://video_85.mp4'},
                { 'frameWidth' : 1280,
                  'encodingRate' : 855611,
                  'url' : 'http://video_1280.mp4'},
                { 'frameWidth' : 640,
                  'encodingRate' : 1796130,
                  'url' : 'http://video_17.mp4'},
                { 'frameWidth' : 320,
                  'encodingRate' : 855611,
                  'url' : 'http://video_200.mp4'},
                ]
            })
        self.assertEquals(url, 'http://video_17.mp4')

    def test_frame_width_missing_with_rendition_frame(self):
        self.integration.platform.rendition_frame_width = 640

        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4',
            'renditions' : [
                { 'frameWidth' : None,
                  'encodingRate' : 755611,
                  'url' : 'http://video_85.mp4'},
                { 'frameWidth' : None,
                  'encodingRate' : 855611,
                  'url' : 'http://video_1280.mp4'},
                { 'frameWidth' : None,
                  'encodingRate' : 1796130,
                  'url' : 'http://video_17.mp4'},
                { 'frameWidth' : None,
                  'encodingRate' : 655611,
                  'url' : 'http://video_200.mp4'},
                ]
            })
        self.assertEquals(url, 'http://video_17.mp4')

    def test_frame_width_missing(self):

        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4',
            'renditions' : [
                { 'frameWidth' : None,
                  'encodingRate' : 755611,
                  'url' : 'http://video_85.mp4'},
                { 'frameWidth' : None,
                  'encodingRate' : 855611,
                  'url' : 'http://video_1280.mp4'},
                { 'frameWidth' : None,
                  'encodingRate' : 1796130,
                  'url' : 'http://video_17.mp4'},
                { 'frameWidth' : None,
                  'encodingRate' : 655611,
                  'url' : 'http://video_200.mp4'},
                ]
            })
        self.assertEquals(url, 'http://video_17.mp4')

    def test_get_closest_size(self):
        self.integration.platform.rendition_frame_width = 640

        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4',
            'renditions' : [
                { 'frameWidth' : 800,
                  'encodingRate' : 855611,
                  'url' : 'http://video_800.mp4'},
                { 'frameWidth' : 1280,
                  'encodingRate' : 855611,
                  'url' : 'http://video_1280.mp4'},
                { 'frameWidth' : 320,
                  'encodingRate' : 855611,
                  'url' : 'http://video_200.mp4'},
                ]
            })
        self.assertEquals(url, 'http://video_800.mp4')

    def test_get_closest_size_higher_encoding(self):
        self.integration.platform.rendition_frame_width = 640

        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4',
            'renditions' : [
                { 'frameWidth' : 800,
                  'encodingRate' : 1796130,
                  'url' : 'http://video_17.mp4'},
                { 'frameWidth' : 800,
                  'encodingRate' : 855611,
                  'url' : 'http://video_85.mp4'},
                { 'frameWidth' : 1280,
                  'encodingRate' : 855611,
                  'url' : 'http://video_1280.mp4'},
                { 'frameWidth' : 320,
                  'encodingRate' : 855611,
                  'url' : 'http://video_200.mp4'},
                ]
            })
        self.assertEquals(url, 'http://video_17.mp4')

    def test_remote_url(self):
        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4',
            'renditions' : [
                { 'frameWidth' : 640,
                  'encodingRate' : 855611,
                  'remoteUrl' : 'http://video_640.mp4'},
                { 'frameWidth' : 1280,
                  'encodingRate' : 855611,
                  'remoteUrl' : 'http://video_1280.mp4'},
                { 'frameWidth' : 320,
                  'encodingRate' : 855611,
                  'remoteUrl' : 'http://video_200.mp4'},
                ]
            })
        self.assertEquals(url, 'http://video_1280.mp4')

    def test_url_trump_remote(self):
        url = self.integration._get_video_url_to_download({
            'FLVURL' : 'http://video.mp4',
            'renditions' : [
                { 'frameWidth' : 1280,
                  'encodingRate' : 855611,
                  'url' : 'http://video_1280.mp4',
                  'remoteUrl' : 'http://remote_1280.mp4'},
                { 'frameWidth' : 320,
                  'encodingRate' : 855611,
                  'remoteUrl' : 'http://video_200.mp4'},
                ]
            })
        self.assertEquals(url, 'http://video_1280.mp4')

class TestSubmitNewVideos(test_utils.neontest.AsyncTestCase):
    def setUp(self):
        # Mock out the call to services
        #self.submit_mocker = patch('integrations.ovp.utils.http.send_request')
        #self.submit_mock = self._callback_wrap_mock(self.submit_mocker.start())
        self.submit_mocker = patch('integrations.ovp.cmsapiv2.client')
        self.submit_mock = self._future_wrap_mock(
            self.submit_mocker.start().Client().send_request)
        self.submit_mock.side_effect = \
          lambda x, **kwargs: tornado.httpclient.HTTPResponse(
              x, 201, buffer=StringIO('{"job_id": "job1"}'))
        

        # Mock out the find_modified_videos and create the platform object
        self.platform = neondata.BrightcoveIntegration.modify(
            'acct1', lambda x: x, create_missing=True)
        self.integration = integrations.brightcove.BrightcoveIntegration(
            'a1', self.platform)
        find_modified_mock = MagicMock()
        self.integration.bc_api.find_modified_videos = find_modified_mock
        self.mock_find_videos =  self._future_wrap_mock(find_modified_mock)

        super(test_utils.neontest.AsyncTestCase, self).setUp()

    def tearDown(self):
        self.submit_mocker.stop()
        self.postgresql.clear_all_tables()
        super(test_utils.neontest.AsyncTestCase, self).tearDown()

    @classmethod
    def setUpClass(cls):
        dump_file = '%s/cmsdb/migrations/cmsdb.sql' % (__base_path__)
        cls.postgresql = test_utils.postgresql.Postgresql(dump_file=dump_file)

    @classmethod
    def tearDownClass(cls): 
        cls.postgresql.stop()

    def _get_video_submission(self):
        '''Returns, the url, parsed json submition'''
        cargs, kwargs = self.submit_mock.call_args

        response = cargs[0]
        return response.url, json.loads(response.body)
    
    @tornado.testing.gen_test
    def test_bc_submit_video_retry_one(self): 
        def _set_last_processed(x): 
            x.last_process_date = 1410012300
        self.platform = neondata.BrightcoveIntegration.modify(
            self.platform.integration_id, _set_last_processed, create_missing=True)
        self.integration.platform.video_submit_retries = 0
        with patch('integrations.ovp.OVPIntegration.submit_video') as submit_video_mocker:
            submit_video_mock = self._future_wrap_mock(submit_video_mocker)
            submit_video_mock.side_effect = Exception('blah') 

            video_obj = { 'id' : 'v1',
                  'length' : 100,
                  'FLVURL' : 'http://video.mp4',
                  'lastModifiedDate' : 1420080400000,
                  'name' : 'Some Video',
                  'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                  'videoStill' : {
                      'id' : 'still_id',
                      'referenceId' : 'my_still_ref',
                      'remoteUrl' : None
                      },
                }
            self.mock_find_videos.side_effect = [[video_obj],[]]
            yield self.integration.submit_new_videos()
            bp = neondata.BrightcoveIntegration.get(self.platform.integration_id) 
            self.assertEquals(bp.last_process_date, 1410012300)
            self.assertEquals(bp.video_submit_retries, 1)

    @tornado.testing.gen_test
    def test_bc_submit_video_retry_two(self): 
        def _set_last_processed(x): 
            x.last_process_date = 1410012300
            x.video_submit_retries = 1
        self.platform = neondata.BrightcoveIntegration.modify(
            self.platform.integration_id, 
            _set_last_processed, 
            create_missing=True)
        self.integration.platform.video_submit_retries = 1
        with patch('integrations.ovp.OVPIntegration.submit_video') as submit_video_mocker:
            submit_video_mock = self._future_wrap_mock(submit_video_mocker)
            submit_video_mock.side_effect = Exception('blah') 

            video_obj = { 'id' : 'v1',
                  'length' : 100,
                  'FLVURL' : 'http://video.mp4',
                  'lastModifiedDate' : 1420080400000,
                  'name' : 'Some Video',
                  'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                  'videoStill' : {
                      'id' : 'still_id',
                      'referenceId' : 'my_still_ref',
                      'remoteUrl' : None
                      },
                }
            self.mock_find_videos.side_effect = [[video_obj],[]]
            yield self.integration.submit_new_videos()
            bp = neondata.BrightcoveIntegration.get(self.platform.integration_id) 
            self.assertEquals(bp.last_process_date, 1410012300)
            self.assertEquals(bp.video_submit_retries, 2)

    @tornado.testing.gen_test
    def test_bc_submit_video_retry_max(self): 
        def _set_last_processed(x): 
            x.last_process_date = 1410012300
            x.video_submit_retries = 3
        self.platform = neondata.BrightcoveIntegration.modify(
            self.platform.integration_id,  
            _set_last_processed, 
            create_missing=True)
        self.integration.platform.video_submit_retries = 3
        with patch('integrations.ovp.OVPIntegration.submit_video') as submit_video_mocker:
            submit_video_mock = self._future_wrap_mock(submit_video_mocker)
            submit_video_mock.side_effect = Exception('blah') 

            video_obj = { 'id' : 'v1',
                  'length' : 100,
                  'FLVURL' : 'http://video.mp4',
                  'lastModifiedDate' : 1420080400000,
                  'name' : 'Some Video',
                  'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                  'videoStill' : {
                      'id' : 'still_id',
                      'referenceId' : 'my_still_ref',
                      'remoteUrl' : None
                      },
                }
            # should reset in this case
            self.mock_find_videos.side_effect = [[video_obj],[]]
            yield self.integration.submit_new_videos()
            bp = neondata.BrightcoveIntegration.get(self.platform.integration_id) 
            self.assertEquals(bp.last_process_date, 1420080400.000)
            self.assertEquals(bp.video_submit_retries, 0)

    @tornado.testing.gen_test
    def test_bc_submit_video_rate_limit_error(self): 
        def _set_last_processed(x): 
            x.last_process_date = 1410012300
            x.video_submit_retries = 1
        self.platform = neondata.BrightcoveIntegration.modify(
            self.platform.integration_id, 
            _set_last_processed, 
            create_missing=True)
        self.integration.platform.video_submit_retries = 1
        with patch('integrations.ovp.OVPIntegration.submit_video') as submit_video_mocker:
            submit_video_mock = self._future_wrap_mock(submit_video_mocker)
            submit_video_mock.side_effect = integrations.ovp.RateLimitError('blah') 

            video_obj = { 'id' : 'v1',
                  'length' : 100,
                  'FLVURL' : 'http://video.mp4',
                  'lastModifiedDate' : 1420080400000,
                  'name' : 'Some Video',
                  'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                  'videoStill' : {
                      'id' : 'still_id',
                      'referenceId' : 'my_still_ref',
                      'remoteUrl' : None
                      },
                }
            self.mock_find_videos.side_effect = [[video_obj],[]]
            yield self.integration.submit_new_videos()
            bp = neondata.BrightcoveIntegration.get(self.platform.integration_id) 
            self.assertEquals(bp.last_process_date, 1410012300)
            self.assertEquals(bp.video_submit_retries, 1)
        
    @tornado.testing.gen_test
    def test_bc_account_with_custom_last_mod_date_updated(self):
        def _create_platform(x): 
            x.id_field = 'newmediapaid'

        self.integration.platform.last_process_date = 1430012300l
        self.platform.id_field = 'newmediapaid' 
        video_obj = { "referenceId": "1234",
	              "videoStillURL": "http://imageinvalid.jpg",
 	              "publishedDate": "1449508690055",
                      "lastModifiedDate": "1449514583914",
  	              "thumbnailURL": "http://tnimageurl.jpg",
	              "id": 4650024830001,
                      "videoStill": {
		          "displayName": None,
                          "referenceId": "1234-videoStillUrl",
                          "remoteUrl": "http://image.jpg",
                          "id": 4650033445001,
                          "type": "VIDEO_STILL"
                      },
	              "name": "Testa Video",
                      "renditions": [{
		          "referenceId": None,
		          "displayName": None,
		          "url": "http://video.mp4",
		          "encodingRate": 1084000,
		          "frameWidth": 640,
		          "audioOnly": False,
		          "controllerType": "DEFAULT",
		          "videoDuration": 244000,
		          "videoCodec": "H264",
		          "videoContainer": "MP4",
		          "frameHeight": 360,
		          "remoteStreamName": None,
		          "remoteUrl": "http://video.mp4",
		          "uploadTimestampMillis": 1449508687502,
		          "id": 4650027261001,
		          "size": 33044327
		      }],
		      "length": 244000,
		      "FLVURL": "http://flvurl.m3u8",
		      "customFields": {},
		      "thumbnail": {
			      "displayName": None,
			      "referenceId": "1234-thumbnailSmallImage",
			      "remoteUrl": "http://image.jpg",
			      "id": 4650033945001,
			      "type": "THUMBNAIL"
		      }
                    } 
        self.mock_find_videos.side_effect = [[video_obj],[]]

        yield self.integration.submit_new_videos()

        # Make sure that the last processed date was updated
        self.assertEquals(self.integration.platform.last_process_date,
                          1449514583.914)
        self.assertEquals(
            neondata.BrightcoveIntegration.get(
                self.platform.integration_id).last_process_date,
            1449514583.914)

    @tornado.testing.gen_test
    def test_typical_bc_account(self):
        self.integration.platform.last_process_date = \
          1420080300l

        video_obj = { 'id' : 'v1',
              'length' : 100,
              'FLVURL' : 'http://video.mp4',
              'lastModifiedDate' : 1420080400000l,
              'name' : 'Some Video',
              'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
              'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : 'my_still_ref',
                  'remoteUrl' : None
                  },
            }

        self.mock_find_videos.side_effect = [[video_obj],[]]

        yield self.integration.submit_new_videos()

        # Make sure that the last processed date was updated
        self.assertEquals(self.integration.platform.last_process_date,
                          1420080400l)
        self.assertEquals(
            neondata.BrightcoveIntegration.get(
                self.platform.integration_id).last_process_date,
            1420080400l)
        
        # Make sure that a video was submitted
        self.assertEquals(self.submit_mock.call_count, 1)

        # Check the call to brightcove
        self.assertEquals(self.mock_find_videos.call_count, 2)
        calls = self.mock_find_videos.call_args_list
        cargs, kwargs = calls[-1]
        
        self.assertDictContainsSubset({
            'from_date' : datetime.datetime(2015, 1, 1, 2, 45),
            '_filter' : ['UNSCHEDULED', 'INACTIVE', 'PLAYABLE'],
            'sort_by' : 'MODIFIED_DATE',
            'sort_order' : 'ASC',
            'video_fields' : ['id', 'videoStill', 'videoStillURL', 
                              'thumbnail', 'thumbnailURL', 'FLVURL', 
                              'renditions', 'length', 'name', 
                              'publishedDate', 'lastModifiedDate', 
                              'referenceId'],
            'page' : 1,
            'custom_fields' : None},
            kwargs)

        # Submit new videos again and this time, there should be no new ones
        self.submit_mock.reset_mock()
        self.mock_find_videos.side_effect = [[]]

        yield self.integration.submit_new_videos()

        self.assertEquals(self.submit_mock.call_count, 0)
        cargs, kwargs = self.mock_find_videos.call_args
        self.assertDictContainsSubset({
            'from_date' : datetime.datetime(2015, 1, 1, 2, 46, 40),
            '_filter' : ['UNSCHEDULED', 'INACTIVE', 'PLAYABLE'],
            'sort_by' : 'MODIFIED_DATE',
            'sort_order' : 'ASC',
            'video_fields' : ['id', 'videoStill', 'videoStillURL', 
                              'thumbnail', 'thumbnailURL', 'FLVURL', 
                              'renditions', 'length', 'name', 
                              'publishedDate', 'lastModifiedDate', 
                              'referenceId'],
            'custom_fields' : None},
            kwargs)
        

    @tornado.testing.gen_test
    def test_new_account_added(self):
        self.integration.platform.last_process_date = None

        self.mock_find_videos.side_effect = [[
            { 'id' : 'v1',
              'length' : 100,
              'FLVURL' : 'http://video.mp4',
              'lastModifiedDate' : 1420080400000l,
              'name' : 'Some Video',
              'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
              'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : 'my_still_ref',
                  'remoteUrl' : None
                  },
            },
            { 'id' : 'v2',
              'length' : 100,
              'FLVURL' : 'http://video2.mp4',
              'lastModifiedDate' : 1420080300000l,
              'name' : 'Some Video 2',
              'videoStillURL' : 'http://bc.com/vid_still.jpg?x=2',
              'videoStill' : {
                  'id' : 'still_id2',
                  'referenceId' : 'my_still_ref2',
                  'remoteUrl' : None
                  },
            }
            ],
            []
            ]

        with options._set_bounded(
                'integrations.ovp.max_vids_for_new_account', 1):
            yield self.integration.submit_new_videos()

        # Make sure that the last processed date was updated
        self.assertEquals(self.integration.platform.last_process_date,
                          1420080400l)
        self.assertEquals(
            neondata.BrightcoveIntegration.get(
                self.platform.integration_id).last_process_date,
            1420080400l)
        
        # Make sure that only one video was submitted
        self.assertEquals(self.submit_mock.call_count, 1)
        url, submission = self._get_video_submission()
        self.assertEquals(submission['external_video_ref'], 'v1')

        # Check the call to brightcove
        cargs, kwargs = self.mock_find_videos.call_args
        
        self.assertDictContainsSubset({
            'from_date' : datetime.datetime(1980, 1, 1),
            '_filter' : ['UNSCHEDULED', 'INACTIVE', 'PLAYABLE'],
            'sort_by' : 'MODIFIED_DATE',
            'sort_order' : 'ASC',
            'video_fields' : ['id', 'videoStill', 'videoStillURL', 
                              'thumbnail', 'thumbnailURL', 'FLVURL', 
                              'renditions', 'length', 'name', 
                              'publishedDate', 'lastModifiedDate', 
                              'referenceId'],
            'custom_fields' : None},
            kwargs)

    @tornado.testing.gen_test
    def test_video_older_than_process_date(self):
        def _set_proc_date(x):
            x.last_process_date = 1420080300l
        self.integration.platform = neondata.BrightcoveIntegration.modify(
            self.platform.integration_id, _set_proc_date)

        video_obj = { 'id' : 'v1',
              'length' : 100,
              'FLVURL' : 'http://video.mp4',
              'lastModifiedDate' : 1420080200000l,
              'name' : 'Some Video',
              'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
              'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : 'my_still_ref',
                  'remoteUrl' : None
                  },
            }

        self.mock_find_videos.side_effect = [[video_obj],[]]

        yield self.integration.submit_new_videos()

        # Make sure that the last processed date was not updated
        self.assertEquals(self.integration.platform.last_process_date,
                          1420080300l)
        self.assertEquals(
            neondata.BrightcoveIntegration.get(
                self.platform.integration_id).last_process_date,
            1420080300l)

    @tornado.testing.gen_test
    def test_get_custom_platform_id(self):
        def _set_platform(x):
            x.last_process_date = 1420080300l
            x.id_field = 'my_fun_id'
        self.integration.platform = neondata.BrightcoveIntegration.modify(
            self.platform.integration_id, _set_platform)

        video_obj = { 'id' : 'v1',
              'length' : 100,
              'FLVURL' : 'http://video.mp4',
              'lastModifiedDate' : 1420080400000l,
              'name' : 'Some Video',
              'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
              'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : 'my_still_ref',
                  'remoteUrl' : None
                  },
              'customFields' : {
                  'my_fun_id' : 'afunid'
              }
            }

        self.mock_find_videos.side_effect = [[video_obj],[]]

        yield self.integration.submit_new_videos()

        # Make sure that a video was submitted
        self.assertEquals(self.submit_mock.call_count, 1)
        url, submission = self._get_video_submission()
        self.assertEquals(submission['external_video_ref'], 'afunid')

        # Check the call to brightcove
        cargs, kwargs = self.mock_find_videos.call_args
        
        self.assertDictContainsSubset({
            'from_date' : datetime.datetime(2015, 1, 1, 2, 45),
            '_filter' : ['UNSCHEDULED', 'INACTIVE', 'PLAYABLE'],
            'sort_by' : 'MODIFIED_DATE',
            'sort_order' : 'ASC',
            'video_fields' : ['id', 'videoStill', 'videoStillURL', 
                              'thumbnail', 'thumbnailURL', 'FLVURL', 
                              'renditions', 'length', 'name', 
                              'publishedDate', 'lastModifiedDate', 
                              'referenceId'],
            'custom_fields' : ['my_fun_id']},
            kwargs)

    @tornado.testing.gen_test
    def test_brightcove_server_error(self):
        self.mock_find_videos.side_effect = [
            api.brightcove_api.BrightcoveApiServerError('Oops BC went down'),
            api.brightcove_api.BrightcoveApiClientError('Oops you messed up')
            ]

        with self.assertLogExists(
            logging.ERROR, 'Server error getting new videos from Brightcove'):
            with self.assertRaises(integrations.ovp.OVPError):
                yield self.integration.submit_new_videos()

        with self.assertLogExists(
            logging.ERROR, 'Client error getting new videos from Brightcove'):
            with self.assertRaises(integrations.ovp.OVPError):
                yield self.integration.submit_new_videos()

class TestSubmitPlaylist(test_utils.neontest.AsyncTestCase):
    def setUp(self):
        # Mock out the call to services
        self.submit_mocker = patch('integrations.ovp.cmsapiv2.client')
        self.submit_mock = self._future_wrap_mock(
            self.submit_mocker.start().Client().send_request)
        self.submit_mock.side_effect = \
          lambda x, **kwargs: tornado.httpclient.HTTPResponse(
              x, 201, buffer=StringIO('{"job_id": "job1"}'))
        
        # Mock out the find_modified_videos and create the platform object
        self.platform = neondata.BrightcoveIntegration.modify(
            'acct1', lambda x: x, create_missing=True)
        self.integration = integrations.brightcove.BrightcoveIntegration(
            'a1', self.platform)
        find_playlist_mock = MagicMock()
        self.integration.bc_api.find_playlist_by_id = find_playlist_mock
        self.mock_get_playlists =  self._future_wrap_mock(find_playlist_mock)

        super(test_utils.neontest.AsyncTestCase, self).setUp()

    def tearDown(self):
        self.submit_mocker.stop()
        self.postgresql.clear_all_tables()
        super(test_utils.neontest.AsyncTestCase, self).tearDown()

    @classmethod
    def setUpClass(cls):
        dump_file = '%s/cmsdb/migrations/cmsdb.sql' % (__base_path__)
        cls.postgresql = test_utils.postgresql.Postgresql(dump_file=dump_file)

    @classmethod
    def tearDownClass(cls): 
        cls.postgresql.stop()

    def _get_video_submission(self, idx=0):
        '''Returns, the url, parsed json submition'''
        cargs, kwargs = self.submit_mock.call_args_list[idx]

        response = cargs[0]
        return response.url, json.loads(response.body)

    @tornado.testing.gen_test
    def test_typical_playlist(self):
        def _set_platform(x):
            x.playlist_feed_ids = [156]
        self.integration.platform = neondata.BrightcoveIntegration.modify(
            self.platform.integration_id, _set_platform)

        self.mock_get_playlists.side_effect = [
            {  'id': 156,
               'videos' : [
                   { 'id' : 1234567,
                     'length' : 100,
                     'FLVURL' : 'http://video.mp4',
                     'lastModifiedDate' : 1420080400000l,
                     'name' : 'Some Video',
                     'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                     'videoStill' : {
                         'id' : 'still_id',
                         'referenceId' : 'my_still_ref',
                         'remoteUrl' : None
                     }
                   },
                ]
            }]

        yield self.integration.submit_playlist_videos()

        # Make sure two videos were submitted
        self.assertEquals(self.submit_mock.call_count, 1)
        url, submission = self._get_video_submission()
        self.assertEquals(submission['external_video_ref'], '1234567')

        # Check the call to brightcove
        self.assertEquals(self.mock_get_playlists.call_count, 1)
        cargs, kwargs = self.mock_get_playlists.call_args

        self.assertEquals(cargs, (156,))
        self.assertDictContainsSubset({
            'video_fields' : ['id', 'videoStill', 'videoStillURL', 
                              'thumbnail', 'thumbnailURL', 'FLVURL', 
                              'renditions', 'length', 'name', 
                              'publishedDate', 'lastModifiedDate', 
                              'referenceId'],
            'playlist_fields' : ['id', 'videos']},
            kwargs)

    @tornado.testing.gen_test
    def test_playlist_with_custom_id_field(self):
        def _set_platform(x):
            x.playlist_feed_ids = [156]
            x.id_field = 'my_fun_id'
        self.integration.platform = neondata.BrightcoveIntegration.modify(
            self.platform.integration_id, _set_platform)

        self.mock_get_playlists.side_effect = [
            {  'id': 156,
               'videos' : [
                   { 'id' : 'v1',
                     'length' : 100,
                     'FLVURL' : 'http://video.mp4',
                     'lastModifiedDate' : 1420080400000l,
                     'name' : 'Some Video',
                     'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
                     'videoStill' : {
                         'id' : 'still_id',
                         'referenceId' : 'my_still_ref',
                         'remoteUrl' : None
                     },
                     'customFields' : {
                         'my_fun_id' : 'afunid'
                     }
                   },
                   { 'id' : 'v2',
                     'length' : 100,
                     'FLVURL' : 'http://video2.mp4',
                     'lastModifiedDate' : 1420080400000l,
                     'name' : 'Some Video2',
                     'videoStillURL' : 'http://bc.com/vid_still2.jpg',
                     'videoStill' : {
                         'id' : 'still_id2',
                         'referenceId' : 'my_still_ref2',
                         'remoteUrl' : None
                     },
                     'customFields' : {
                         'my_fun_id' : 'afunid2'
                     }
                   }
                ]
            }]

        yield self.integration.submit_playlist_videos()

        # Make sure two videos were submitted
        self.assertEquals(self.submit_mock.call_count, 2)
        url, submission = self._get_video_submission()
        self.assertEquals(submission['external_video_ref'], 'afunid')

        # Check the call to brightcove
        self.assertEquals(self.mock_get_playlists.call_count, 1)
        cargs, kwargs = self.mock_get_playlists.call_args

        self.assertEquals(cargs, (156,))
        self.assertDictContainsSubset({
            'video_fields' : ['id', 'videoStill', 'videoStillURL', 
                              'thumbnail', 'thumbnailURL', 'FLVURL', 
                              'renditions', 'length', 'name', 
                              'publishedDate', 'lastModifiedDate', 
                              'referenceId'],
            'custom_fields' : ['my_fun_id'],
            'playlist_fields' : ['id', 'videos']},
            kwargs)

    @tornado.testing.gen_test
    def test_brightcove_error(self):
        def _set_platform(x):
            x.playlist_feed_ids = [156]
        self.integration.platform = neondata.BrightcoveIntegration.modify(
            self.platform.integration_id, _set_platform)
        
        self.mock_get_playlists.side_effect = [
            api.brightcove_api.BrightcoveApiServerError('Big Fail!'),
            api.brightcove_api.BrightcoveApiClientError('You Fail!'),
            ]

        with self.assertLogExists(
            logging.ERROR, 'Server error getting playlist 156 from Brightcove'):
            with self.assertRaises(integrations.ovp.OVPError):
                yield self.integration.submit_playlist_videos()

        with self.assertLogExists(
            logging.ERROR, 'Client error getting playlist 156 from Brightcove'):
            with self.assertRaises(integrations.ovp.OVPError):
                yield self.integration.submit_playlist_videos()

class TestSubmitSpecificVideos(test_utils.neontest.AsyncTestCase):
    def setUp(self):
        # Mock out the call to services
        self.submit_mocker = patch('integrations.ovp.cmsapiv2.client')
        self.submit_mock = self._future_wrap_mock(
            self.submit_mocker.start().Client().send_request)
        self.submit_mock.side_effect = \
          lambda x, **kwargs: tornado.httpclient.HTTPResponse(
              x, 201, buffer=StringIO('{"job_id": "job1"}'))
        

        # Mock out the find_modified_videos and create the platform object
        self.platform = neondata.BrightcoveIntegration.modify(
            'acct1', lambda x: x, create_missing=True)
        self.integration = integrations.brightcove.BrightcoveIntegration(
            'a1', self.platform)
        find_videos_mock = MagicMock()
        self.integration.bc_api.find_videos_by_ids = find_videos_mock
        self.mock_get_videos =  self._future_wrap_mock(find_videos_mock)

        super(TestSubmitSpecificVideos, self).setUp()

    def tearDown(self):
        self.submit_mocker.stop()
        self.postgresql.clear_all_tables() 
        super(TestSubmitSpecificVideos, self).tearDown()

    @classmethod
    def setUpClass(cls):
        dump_file = '%s/cmsdb/migrations/cmsdb.sql' % (__base_path__)
        cls.postgresql = test_utils.postgresql.Postgresql(dump_file=dump_file)

    @classmethod
    def tearDownClass(cls): 
        cls.postgresql.stop()

    def _get_video_submission(self, idx=0):
        '''Returns, the url, parsed json submition'''
        cargs, kwargs = self.submit_mock.call_args_list[idx]

        response = cargs[0]
        return response.url, json.loads(response.body)

    @tornado.testing.gen_test
    def test_typical_account(self):
        self.mock_get_videos.side_effect = [[
            { 'id' : 1234567,
              'length' : 100,
              'FLVURL' : 'http://video.mp4',
              'lastModifiedDate' : 1420080400000l,
              'name' : 'Some Video',
              'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
              'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : 'my_still_ref',
                  'remoteUrl' : None
              }
            },
            { 'id' : 'v2',
              'length' : 100,
              'FLVURL' : 'http://video2.mp4',
              'lastModifiedDate' : 1420080400000l,
              'name' : 'Some Video2',
              'videoStillURL' : 'http://bc.com/vid_still2.jpg',
              'videoStill' : {
                  'id' : 'still_id2',
                  'referenceId' : 'my_still_ref2',
                  'remoteUrl' : None
              }
              }]]

        yield self.integration.lookup_and_submit_videos([1234567, 'v2'])

        # Make sure two videos were submitted
        self.assertEquals(self.submit_mock.call_count, 2)
        url, submission = self._get_video_submission(0)
        self.assertEquals(submission['external_video_ref'], '1234567')
        url, submission = self._get_video_submission(1)
        self.assertEquals(submission['external_video_ref'], 'v2')

        # Check the call to brightcove
        self.assertEquals(self.mock_get_videos.call_count, 1)
        cargs, kwargs = self.mock_get_videos.call_args

        self.assertEquals(cargs, ([1234567, 'v2'],))
        self.assertDictContainsSubset({
            'video_fields' : ['id', 'videoStill', 'videoStillURL', 
                              'thumbnail', 'thumbnailURL', 'FLVURL', 
                              'renditions', 'length', 'name', 
                              'publishedDate', 'lastModifiedDate', 
                              'referenceId']},
            kwargs)

    @tornado.testing.gen_test
    def test_lookup_video(self):
        self.mock_get_videos.side_effect = [[
            { 'id' : 1234567,
              'length' : 100,
              'FLVURL' : 'http://video.mp4',
              'lastModifiedDate' : 1420080400000l,
              'name' : 'Some Video',
              'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
              'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : 'my_still_ref',
                  'remoteUrl' : None
              }
            }]]

        video_info = yield self.integration.lookup_videos([1234567])

        self.assertEquals(len(video_info), 1)
        self.assertEquals(self.integration.get_video_url(video_info[0]),
                          'http://video.mp4')

    @tornado.testing.gen_test
    def test_continue_on_error(self):
        self.mock_get_videos.side_effect = [[
            { 'id' : 1234567,
              'length' : 100,
              'FLVURL' : 'http://video.mp4',
              'lastModifiedDate' : 1420080400000l,
              'name' : 'Some Video',
              'videoStillURL' : 'http://bc.com/vid_still.jpg?x=5',
              'videoStill' : {
                  'id' : 'still_id',
                  'referenceId' : 'my_still_ref',
                  'remoteUrl' : None
              }
            },
            { 'id' : 'v2',
              'length' : 100,
              'FLVURL' : 'http://video2.mp4',
              'lastModifiedDate' : 1420080400000l,
              'name' : 'Some Video2',
              'videoStillURL' : 'http://bc.com/vid_still2.jpg',
              'videoStill' : {
                  'id' : 'still_id2',
                  'referenceId' : 'my_still_ref2',
                  'remoteUrl' : None
              }
              }]]

        base_request = tornado.httpclient.HTTPRequest('http://some_url')
        self.submit_mock.side_effect = [
            tornado.httpclient.HTTPResponse(
                base_request, 500, error=tornado.httpclient.HTTPError(500)),
            tornado.httpclient.HTTPResponse(
              base_request, 201, buffer=StringIO('{"job_id": "job1"}'))]

        results = yield self.integration.lookup_and_submit_videos(
            [1234567, 'v2'], continue_on_error=True)
        self.assertEquals(results['v2'], 'job1')
        self.assertIsInstance(results[1234567], integrations.ovp.CMSAPIError)

    @tornado.testing.gen_test
    def test_brightcove_error(self):
        self.mock_get_videos.side_effect = [
            api.brightcove_api.BrightcoveApiServerError('Oops BC went down'),
            api.brightcove_api.BrightcoveApiClientError('You messed up')
            ]

        with self.assertLogExists(
            logging.ERROR, 'Server error getting data from Brightcove'):
            with self.assertRaises(integrations.ovp.OVPError):
                yield self.integration.lookup_and_submit_videos(
                    [1234567, 'v2'])

        with self.assertLogExists(
            logging.ERROR, 'Client error getting data from Brightcove'):
            with self.assertRaises(integrations.ovp.OVPError):
                yield self.integration.lookup_and_submit_videos(
                    [1234567, 'v2'])

class TestCMSAPIIntegration(test_utils.neontest.AsyncTestCase):
    def setUp(self):
        # Mock out the call to services
        self.submit_mocker = patch('integrations.ovp.cmsapiv2.client')
        self.submit_mock = self._future_wrap_mock(
            self.submit_mocker.start().Client().send_request)
        self.submit_mock.side_effect = \
          lambda x, **kwargs: tornado.httpclient.HTTPResponse(
              x, 201, buffer=StringIO('{"job_id": "job1"}'))
        

        # Mock out the find_modified_videos and create the platform object
        def _create(x):
            x.application_client_id = 'clientid'
            x.application_client_secret = 'secret'
        self.platform = neondata.BrightcoveIntegration.modify(
            'acct1', _create, create_missing=True)
        self.integration = integrations.create_ovp_integration(
            'a1', self.platform)
        self.integration.bc_api.get_videos = MagicMock()
        self.mock_get_videos =  self._future_wrap_mock(
            self.integration.bc_api.get_videos)
        self.integration.bc_api.get_video_sources = MagicMock()
        self.mock_get_video_sources =  self._future_wrap_mock(
            self.integration.bc_api.get_video_sources)

        super(TestCMSAPIIntegration, self).setUp()

    def tearDown(self):
        self.submit_mocker.stop()
        self.postgresql.clear_all_tables() 
        super(TestCMSAPIIntegration, self).tearDown()

    @classmethod
    def setUpClass(cls):
        dump_file = '%s/cmsdb/migrations/cmsdb.sql' % (__base_path__)
        cls.postgresql = test_utils.postgresql.Postgresql(dump_file=dump_file)

    @classmethod
    def tearDownClass(cls): 
        cls.postgresql.stop()

    @tornado.testing.gen_test
    def test_lookup_video(self):
        self.mock_get_videos.side_effect = [[{
            'id': 'vid1',
            'name' : 'some video'
            }]]
        self.mock_get_video_sources.side_effect = [[{
            'src' : 'some_url.mp4',
            'width' : 1280
            }]]

        vid_info = yield self.integration.lookup_videos(['vid1'])

        self.assertEquals(len(vid_info), 1)
        self.mock_get_videos.assert_called_with(q='id:vid1')
        self.mock_get_video_sources.assert_called_with('vid1')

        self.assertEquals(vid_info[0], {
            'id': 'vid1',
            'name' : 'some video',
            'sources' : [{
                'src' : 'some_url.mp4',
                'width' : 1280
                }]
            })

        self.assertEquals(self.integration.get_video_url(vid_info[0]),
                          'some_url.mp4')

    def test_get_best_image_info_poster(self): 
        video = {
            "account_id": "1752604059001",
            "complete": True,
            "id": "4492075574001",
            "images": {
                "poster": {
                    "asset_id": "4492153571001",
                    "sources": [
                        {
                            "src": "https://testposter.xyz"
                        }
                    ],
                    "src": "https://testposter.xyz"
                },
                "thumbnail": {
                    "asset_id": "4492154714001",
                    "sources": [
                        {
                            "src": "https://test.xyz"
                        }
                    ],
                    "src": "https://test.xyz"
                }
            },
            "link": None,
            "name": "sea_marvels.mp4",
            "state": "ACTIVE",
            "updated_at": "2015-09-17T17:41:20.782Z"
        }
        ii = self.integration._get_best_image_info(video) 
        url = ii[0]
        ref_dict = ii[1] 
        self.assertEquals(url, 'https://testposter.xyz') 
        self.assertEquals(ref_dict['id'], '4492153571001') 
 
    def test_get_best_image_info_thumbnail(self): 
        video = {
            "account_id": "1752604059001",
            "complete": True,
            "id": "4492075574001",
            "images": {
                "thumbnail": {
                    "asset_id": "4492154714001",
                    "sources": [
                        {
                            "src": "https://test.xyz"
                        }
                    ],
                    "src": "https://test.xyz"
                }
            },
            "link": None,
            "name": "sea_marvels.mp4",
            "state": "ACTIVE",
            "updated_at": "2015-09-17T17:41:20.782Z"
        }
        ii = self.integration._get_best_image_info(video) 
        url = ii[0]
        ref_dict = ii[1] 
        self.assertEquals(url, 'https://test.xyz') 
        self.assertEquals(ref_dict['id'], '4492154714001') 
 
    def test_get_best_image_info_dne(self): 
        video = {
            "account_id": "1752604059001",
            "complete": True,
            "id": "4492075574001",
            "images": {
                "dne": {
                    "asset_id": "4492154714001",
                    "sources": [
                        {
                            "src": "https://test.xyz"
                        }
                    ],
                    "src": "https://test.xyz"
                }
            },
            "link": None,
            "name": "sea_marvels.mp4",
            "state": "ACTIVE",
            "updated_at": "2015-09-17T17:41:20.782Z"
        }
        ii = self.integration._get_best_image_info(video)
        url = ii[0]
        ref_dict = ii[1] 
        self.assertEquals(url, None) 
        self.assertEquals(ref_dict['id'], None) 
 
    def test_get_best_image_info_no_images(self): 
        video = {
            "account_id": "1752604059001",
            "complete": True,
            "id": "4492075574001",
            "images": {
            },
            "link": None,
            "name": "sea_marvels.mp4",
            "state": "ACTIVE",
            "updated_at": "2015-09-17T17:41:20.782Z"
        }
        with self.assertLogExists(logging.ERROR, 'Unable to find'):
            ii = self.integration._get_best_image_info(video)
        self.assertEquals(ii[0], None)

    def test_extract_image_field(self): 
        video = {
            "account_id": "1752604059001",
            "complete": True,
            "id": "4492075574001",
            "images": {
                "poster": {
                    "asset_id": "4492153571001",
                    "sources": [
                        {
                            "src": "https://testposter.xyz"
                        }
                    ],
                    "src": "https://testposter.xyz"
                },
                "thumbnail": {
                    "asset_id": "4492154714001",
                    "sources": [
                        {
                            "src": "https://test.xyz"
                        }
                    ],
                    "src": "https://test.xyz"
                }
            },
            "link": None,
            "name": "sea_marvels.mp4",
            "state": "ACTIVE",
            "updated_at": "2015-09-17T17:41:20.782Z"
        }
        ifs = self.integration._extract_image_field(video, 'id')
        self.assertEquals(ifs[0], '4492153571001') 
        self.assertEquals(ifs[1], '4492154714001')
 
        ifs = self.integration._extract_image_field(video, 'dne')
        self.assertEquals(ifs, [])

    def test_extract_image_urls(self): 
        video = {
            "account_id": "1752604059001",
            "complete": True,
            "id": "4492075574001",
            "images": {
                "poster": {
                    "asset_id": "4492153571001",
                    "sources": [
                        {
                            "src": "https://testposter.xyz"
                        }
                    ],
                    "src": "https://testposter.xyz"
                },
                "thumbnail": {
                    "asset_id": "4492154714001",
                    "sources": [
                        {
                            "src": "https://test.xyz"
                        }
                    ],
                    "src": "https://test.xyz"
                }
            },
            "link": None,
            "name": "sea_marvels.mp4",
            "state": "ACTIVE",
            "updated_at": "2015-09-17T17:41:20.782Z"
        }
        iurls = self.integration._extract_image_urls(video)
        self.assertEquals(iurls[0], 'https://testposter.xyz') 
        self.assertEquals(iurls[1], 'https://test.xyz')

    def test_get_best_thumbnail_info_exists(self): 
        video = {
            "account_id": "1752604059001",
            "complete": True,
            "id": "4492075574001",
            "images": {
                "poster": {
                    "asset_id": "4492153571001",
                    "sources": [
                        {
                            "src": "https://testposter.xyz"
                        }
                    ],
                    "src": "https://testposter.xyz"
                },
                "thumbnail": {
                    "asset_id": "4492154714001",
                    "sources": [
                        {
                            "src": "https://test.xyz"
                        }
                    ],
                    "src": "https://test.xyz"
                }
            },
            "link": None,
            "name": "sea_marvels.mp4",
            "state": "ACTIVE",
            "updated_at": "2015-09-17T17:41:20.782Z"
        }
        bii = self.integration.get_video_thumbnail_info(video) 
        self.assertEquals(bii['thumb_url'], 'https://testposter.xyz')
        self.assertEquals(bii['thumb_ref'], '4492153571001')
 
    def test_get_best_thumbnail_info_dne(self): 
        video = {
            "account_id": "1752604059001",
            "complete": True,
            "id": "4492075574001",
            "images": {
            },
            "link": None,
            "name": "sea_marvels.mp4",
            "state": "ACTIVE",
            "updated_at": "2015-09-17T17:41:20.782Z"
        }
        with self.assertLogExists(logging.WARNING, 'Unable to find'):
            bii = self.integration.get_video_thumbnail_info(video)
        self.assertEquals(bii['thumb_url'], None)

    @tornado.testing.gen_test
    def test_set_video_iter(self): 
        self.mock_get_videos.side_effect = [[{
            'id': 'vid1',
            'name' : 'some video'
            }, 
            { 'id': 'vid2', 
              'name' : 'some video 2' }]]
        yield self.integration.set_video_iter()
        vid_list = list(self.integration.video_iter) 
        v1 = vid_list[0]
        v2 = vid_list[1]
        self.assertEquals(v1['id'], 'vid1') 
        self.assertEquals(v1['name'], 'some video') 
        self.assertEquals(v2['id'], 'vid2') 
        self.assertEquals(v2['name'], 'some video 2')
 
    @tornado.testing.gen_test
    def test_set_video_iter_exc(self): 
        self.mock_get_videos.side_effect = [ 
            api.brightcove_api.BrightcoveApiServerError ]
        
        with self.assertLogExists(logging.ERROR, 'Brightcove Error'):
            yield self.integration.set_video_iter()
        vid_list = list(self.integration.video_iter)
        self.assertEquals(len(vid_list), 0) 

    @tornado.testing.gen_test
    def test_lookup_video_errors(self):
        self.mock_get_videos.side_effect = [
            [],
            api.brightcove_api.BrightcoveApiServerError,
            api.brightcove_api.BrightcoveApiClientError]

        with self.assertRaises(integrations.ovp.OVPError):
            yield self.integration.lookup_videos(['vid1'])

        with self.assertLogExists(logging.ERROR, 'Brightcove Error occurred'):
            with self.assertRaises(integrations.ovp.OVPError):
                yield self.integration.lookup_videos(['vid1'])

        with self.assertLogExists(logging.ERROR, 'Brightcove Error occurred'):
            with self.assertRaises(integrations.ovp.OVPError):
                yield self.integration.lookup_videos(['vid1'])

if __name__ == '__main__':
    utils.neon.InitNeon()
    unittest.main()
