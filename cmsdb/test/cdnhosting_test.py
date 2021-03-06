#!/usr/bin/env python
import os
import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..',
                                             '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

from api import akamai_api
import boto.exception
import boto3
from cStringIO import StringIO
import cmsdb.cdnhosting
from cmsdb import neondata
import cv2
from cvutils.imageutils import PILImageUtils
from cvutils import smartcrop
import json
import logging
from mock import MagicMock, patch, ANY
import numpy as np
import PIL
import random
import re
import tempfile
import test_utils.mock_boto_s3 as boto_mock
import test_utils.neontest
import test_utils.opencv
import test_utils.postgresql
import time
import tornado.testing
from tornado.httpclient import HTTPResponse, HTTPRequest, HTTPError
import unittest
from utils.options import options
import urlparse
import utils.neon

_log = logging.getLogger(__name__)

class CDNTestBase(test_utils.neontest.AsyncTestCase):
    @classmethod
    def setUpClass(cls):
        super(CDNTestBase, cls).tearDownClass() 
        cls.max_io_loop_size = options.get(
            'cmsdb.neondata.max_io_loop_dict_size')
        options._set('cmsdb.neondata.max_io_loop_dict_size', 10)
        dump_file = '%s/cmsdb/migrations/cmsdb.sql' % (__base_path__)
        cls.postgresql = test_utils.postgresql.Postgresql(dump_file=dump_file)

    @classmethod
    def tearDownClass(cls): 
        cls.postgresql.stop()
        options._set('cmsdb.neondata.max_io_loop_dict_size', 
            cls.max_io_loop_size)
        super(CDNTestBase, cls).tearDownClass()
 
    def tearDown(self):
        self.postgresql.clear_all_tables()
        super(CDNTestBase, self).tearDown()

class TestAWSHosting(test_utils.neontest.AsyncTestCase):
    ''' 
    Test the ability to host images on an aws cdn (aka S3)
    '''
    def setUp(self):
        self.s3conn = boto_mock.MockConnection()
        self.s3_patcher = patch('cmsdb.cdnhosting.S3Connection')
        self.mock_conn = self.s3_patcher.start()
        self.mock_conn.return_value = self.s3conn
        self.s3conn.create_bucket('hosting-bucket')
        self.bucket = self.s3conn.get_bucket('hosting-bucket')

        # Mock neondata
        self.neondata_patcher = patch('cmsdb.cdnhosting.cmsdb.neondata')
        self.datamock = self.neondata_patcher.start()
        self.datamock.S3CDNHostingMetadata = neondata.S3CDNHostingMetadata
        self.datamock.CloudinaryCDNHostingMetadata = \
          neondata.CloudinaryCDNHostingMetadata
        self.datamock.NeonCDNHostingMetadata = neondata.NeonCDNHostingMetadata
        self.datamock.PrimaryNeonHostingMetadata = \
          neondata.PrimaryNeonHostingMetadata
        self.datamock.ThumbnailServingURLs.create_filename = \
          neondata.ThumbnailServingURLs.create_filename

        # Mock out the cdn url check
        self.cdn_check_patcher = patch('cmsdb.cdnhosting.utils.http')
        self.mock_cdn_url = self._future_wrap_mock(
            self.cdn_check_patcher.start().send_request)
        self.mock_cdn_url.side_effect = lambda x, **kw: HTTPResponse(x, 200)

        random.seed(1654984)

        self.image = PILImageUtils.create_random_image(480, 640)
        super(TestAWSHosting, self).setUp()

    def tearDown(self):
        self.neondata_patcher.stop()
        self.s3_patcher.stop()
        self.cdn_check_patcher.stop()
        super(TestAWSHosting, self).tearDown()

    @patch('cmsdb.cdnhosting.smartcrop.SmartCrop.crop_and_resize', 
        side_effect=lambda x, y: np.zeros((x, y, 3), dtype=np.uint8))
    @tornado.testing.gen_test
    def test_source_and_smart_crop(self, mock_smartcrop):
        '''
        Tests that the source cropping and smart cropping is only
        performed when we're dealing with a NEON image.
        '''
        # # set the return value for resize_and_crop
        # mock_smartcrop.crop_and_resize.side_effect = \
        #                             lambda s, x, y: np.array(x, y, 3)
        # make the first thumb -- with smart cropping
        thumb_with_smart_crop = neondata.ThumbnailMetadata(
            'test_thumb_from_neon', ttype=neondata.ThumbnailType.NEON)
        self.assertTrue(thumb_with_smart_crop.do_smart_crop)
        self.assertTrue(thumb_with_smart_crop.do_source_crop)

        # make the second thumb, without smart cropping
        thumb_without_smart_crop = neondata.ThumbnailMetadata(
            'test_thumb_from_default', ttype=neondata.ThumbnailType.DEFAULT)
        self.assertFalse(thumb_without_smart_crop.do_smart_crop)
        self.assertFalse(thumb_without_smart_crop.do_source_crop)
        # make the CDN Hosting metdata
        cdn_metadata = neondata.S3CDNHostingMetadata(None,
                'access_key', 'secret_key',
                'hosting-bucket', ['cdn1.cdn.com', 'cdn2.cdn.com'],
                'folder1', source_crop=[0, .33, 0, 0],
                resize=True, rendition_sizes=[(300, 300), (690, 450)])
        # make the thumbnail metadata
        # add the side effect from the ThumbnailMetadata.get
        # create the CDNHosting object 1
        hoster = cmsdb.cdnhosting.CDNHosting.create(cdn_metadata)
        yield hoster.upload(self.image, 'test_thumb_from_neon', async=True,
                        do_smart_crop=thumb_with_smart_crop.do_smart_crop,
                        do_source_crop=thumb_with_smart_crop.do_source_crop)
        # ensure that smartcrop was called
        self.assertGreater(mock_smartcrop.call_count, 0)

        cur_call_count = mock_smartcrop.call_count

        yield hoster.upload(self.image, 'test_thumb_from_default', 
                    async=True,
                    do_smart_crop=thumb_without_smart_crop.do_smart_crop,
                    do_source_crop=thumb_without_smart_crop.do_source_crop)
        # ensure that smartcrop was not called again
        self.assertEquals(mock_smartcrop.call_count, cur_call_count)

    @tornado.testing.gen_test
    def test_host_single_image(self):
        '''
        Test hosting a single image with CDN prefixes into a S3 bucket
        '''
        metadata = neondata.S3CDNHostingMetadata(None,
            'access_key', 'secret_key',
            'hosting-bucket', ['cdn1.cdn.com', 'cdn2.cdn.com'],
            'folder1', False, False, False)

        hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)
        yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)

        self.mock_conn.assert_called_with('access_key', 'secret_key')

        s3_key = self.bucket.get_key(
            'folder1/neontnacct1_vid1_tid1_w640_h480.jpg')
        self.assertIsNotNone(s3_key)
        self.assertEqual(s3_key.content_type, 'image/jpeg')
        self.assertNotEqual(s3_key.policy, 'public-read')

        # Make sure that the serving urls weren't added
        self.assertEquals(self.datamock.ThumbnailServingURLs.modify.call_count,
                          0)
    
    @tornado.testing.gen_test
    def test_primary_hosting_single_image(self):
        '''
        Test hosting the Primary copy for a image in Neon's primary 
        hosting bucket
        '''
        metadata = neondata.PrimaryNeonHostingMetadata(
            'acct1', 'hosting-bucket')

        hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)
        urls = yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)
        self.assertEqual(
            urls[0][0],
            "http://s3.amazonaws.com/hosting-bucket/acct1/vid1/tid1/w640_h480.jpg")
        self.bucket = self.s3conn.get_bucket('hosting-bucket')
        s3_key = self.bucket.get_key('acct1/vid1/tid1/w640_h480.jpg')
        self.assertIsNotNone(s3_key)
        self.assertEqual(s3_key.content_type, 'image/jpeg')
        self.assertEqual(s3_key.policy, 'public-read')

    @tornado.testing.gen_test
    def test_primary_hosting_with_folder(self):
        '''
        Test hosting the Primary copy for a image in Neon's primary 
        hosting bucket
        '''
        metadata = neondata.PrimaryNeonHostingMetadata(
            'acct1',
            'hosting-bucket',
            folder_prefix='my/folder/path')

        hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)
        urls = yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)
        self.assertEqual(
            urls[0][0],
            "http://s3.amazonaws.com/hosting-bucket/my/folder/path/acct1/vid1/tid1/w640_h480.jpg")
        self.bucket = self.s3conn.get_bucket('hosting-bucket')
        s3_key = self.bucket.get_key('my/folder/path/acct1/vid1/tid1/w640_h480.jpg')
        self.assertIsNotNone(s3_key)
        self.assertEqual(s3_key.content_type, 'image/jpeg')
        self.assertEqual(s3_key.policy, 'public-read')

    @tornado.testing.gen_test
    def test_overwrite_image(self):
        metadata = neondata.PrimaryNeonHostingMetadata(
            'acct1', 'hosting-bucket')
        hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)

        # Do initial upload
        url = yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)
        s3key = self.bucket.get_key('acct1/vid1/tid1/w640_h480.jpg')
        orig_etag = s3key.etag

        # Now upload, but don't overwrite
        new_image = PILImageUtils.create_random_image(480, 640)
        yield hoster.upload(new_image, 'acct1_vid1_tid1', overwrite=False,
                            async=True)

        # Check the file contents
        s3key = self.bucket.get_key('acct1/vid1/tid1/w640_h480.jpg')
        self.assertIsNotNone(s3key)
        self.assertEquals(s3key.etag, orig_etag)

        # Now overwrite
        yield hoster.upload(new_image, 'acct1_vid1_tid1', async=True)
            
        buf = StringIO()
        s3key = self.bucket.get_key('acct1/vid1/tid1/w640_h480.jpg')
        self.assertNotEquals(s3key.etag, orig_etag)
            

    @tornado.testing.gen_test
    def test_permissions_error_uploading_image(self):
        self.s3conn.get_bucket = MagicMock()
        self.s3conn.get_bucket().get_key.side_effect = [None]
        self.s3conn.get_bucket().new_key().set_contents_from_file.side_effect = [boto.exception.S3PermissionsError('Permission error')]
        
        metadata = neondata.S3CDNHostingMetadata(None,
            'access_key', 'secret_key',
            'hosting-bucket', ['cdn1.cdn.com', 'cdn2.cdn.com'],
            'folder1', False, False)
        hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)

        with self.assertLogExists(logging.ERROR, 'AWS client error'):
            with self.assertRaises(IOError):
                yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)

    @tornado.testing.gen_test
    def test_create_error_uploading_image(self):
        self.s3conn.get_bucket = MagicMock()
        self.s3conn.get_bucket().get_key.side_effect = [None]
        self.s3conn.get_bucket().new_key().set_contents_from_file.side_effect = [boto.exception.S3CreateError('oops', 'seriously, oops')]
        
        metadata = neondata.S3CDNHostingMetadata(None,
            'access_key', 'secret_key',
            'hosting-bucket', ['cdn1.cdn.com', 'cdn2.cdn.com'],
            'folder1', False, False)
        hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)

        with self.assertLogExists(logging.ERROR, 'AWS Server error'):
            with self.assertRaises(IOError):
                yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)

    @tornado.testing.gen_test
    def test_s3_redirect(self):
        self.s3conn.create_bucket('my-bucket')
        self.s3conn.create_bucket('host-bucket')
        self.s3conn.create_bucket('obucket')
        self.s3conn.create_bucket('mine')

        with patch('cmsdb.cdnhosting.get_s3_hosting_bucket') as location_mock:
            location_mock.return_value = 'host-bucket'
            yield [
                cmsdb.cdnhosting.create_s3_redirect(
                    'dest/image.jpg', 'src/samebuc.jpg', 'my-bucket',
                    'my-bucket', async=True),
                cmsdb.cdnhosting.create_s3_redirect(
                    'dest/image.jpg', 'src/diffbuc.jpg', 'my-bucket',
                    'obucket', async=True),
                cmsdb.cdnhosting.create_s3_redirect(
                        'dest/image.jpg', 'src/bothdefault.jpg', async=True),
                cmsdb.cdnhosting.create_s3_redirect(
                        'dest/image.jpg', 'src/destdefault.jpg',
                        src_bucket='mine', async=True),
                cmsdb.cdnhosting.create_s3_redirect(
                        'dest/image.jpg', 'src/srcdefault.jpg',
                        dest_bucket='mine', async=True), 
                ]

        self.assertEqual(self.s3conn.get_bucket('my-bucket').get_key(
            'src/samebuc.jpg').redirect_destination,
            '/dest/image.jpg')
        self.assertEqual(self.s3conn.get_bucket('obucket').get_key(
            'src/diffbuc.jpg').redirect_destination,
            'https://s3.amazonaws.com/my-bucket/dest/image.jpg')
        self.assertEqual(self.s3conn.get_bucket('host-bucket').get_key(
            'src/bothdefault.jpg').redirect_destination,
            '/dest/image.jpg')
        self.assertEqual(self.s3conn.get_bucket('mine').get_key(
            'src/destdefault.jpg').redirect_destination,
            'https://s3.amazonaws.com/host-bucket/dest/image.jpg')
        self.assertEqual(self.s3conn.get_bucket('host-bucket').get_key(
            'src/srcdefault.jpg').redirect_destination,
            'https://s3.amazonaws.com/mine/dest/image.jpg')

    @tornado.testing.gen_test
    def test_permissions_error_s3_redirect(self):
        self.s3conn.get_bucket = MagicMock()
        self.s3conn.get_bucket().new_key().set_contents_from_string.side_effect = [boto.exception.S3PermissionsError('Permission Error')]
        self.s3conn.create_bucket('host-bucket')

        with self.assertLogExists(logging.ERROR, 'AWS client error'):
            with self.assertRaises(IOError):
                yield cmsdb.cdnhosting.create_s3_redirect('dest.jpg', 'src.jpg',
                                                        async=True)

    @tornado.testing.gen_test
    def test_create_error_s3_redirect(self):
        self.s3conn.get_bucket = MagicMock()
        self.s3conn.get_bucket().new_key().set_contents_from_string.side_effect = [boto.exception.S3CreateError('oops', 'seriously, oops')]
        self.s3conn.create_bucket('host-bucket')

        with self.assertLogExists(logging.ERROR, 'AWS Server error'):
            with self.assertRaises(IOError):
                yield cmsdb.cdnhosting.create_s3_redirect('dest.jpg', 'src.jpg',
                                                        async=True)

    @patch('cmsdb.cdnhosting.boto3.client')
    @patch('cmsdb.cdnhosting.boto3.resource') 
    @tornado.testing.gen_test
    def test_host_single_image_iam_role(self, assr_mocker, res_mocker):
        assr_mocker.return_value.assume_role.side_effect = [{ 'Credentials' : { 
            'AccessKeyId' : '123', 
            'SecretAccessKey' : '12421', 
            'SessionToken' : '342adsf'
        }}] 
        metadata = neondata.S3CDNHostingMetadata(None,
            'access_key', 'secret_key',
            'hosting-bucket', ['cdn1.cdn.com', 'cdn2.cdn.com'],
            'folder1', False, False, False, 
            use_iam_role=True, 
            iam_role_account='12345/adsf', 
            iam_role_name='Neonaa', 
            iam_role_external_id='12324234')

        res_mocker.return_value.Bucket.return_value = MagicMock() 

        hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)
        yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)

        hoster.s3bucket.put_object.assert_called_with(
            Body=ANY, 
            ContentType='image/jpeg', 
            Key='folder1/neontnacct1_vid1_tid1_w640_h480.jpg', 
            ACL='') 

        # Make sure that the serving urls weren't added
        self.assertEquals(self.datamock.ThumbnailServingURLs.modify.call_count,
                          0)

class TestCloudinaryHosting(test_utils.neontest.AsyncTestCase):

    def setUp(self):
        self.image = PILImageUtils.create_random_image(480, 640)
        super(TestCloudinaryHosting, self).setUp()

    @patch('cmsdb.cdnhosting.utils.http.send_request')
    def test_cloudinary_hosting(self, mock_http):
        mock_http = self._future_wrap_mock(mock_http)
        
        mock_response = '{"public_id":"bfea94933dc752a2def8a6d28f9ac4c2","version":1406671711,"signature":"26bd2ffa2b301b9a14507d152325d7692c0d4957","width":480,"height":268,"format":"jpg","resource_type":"image","created_at":"2014-07-29T22:08:19Z","bytes":74827,"type":"upload","etag":"99fd609b49a802fdef7e2952a5e75dc3","url":"http://res.cloudinary.com/neon-labs/image/upload/v1406671711/bfea94933dc752a2def8a6d28f9ac4c2.jpg","secure_url":"https://res.cloudinary.com/neon-labs/image/upload/v1406671711/bfea94933dc752a2def8a6d28f9ac4c2.jpg"}'

        metadata = neondata.CloudinaryCDNHostingMetadata()
        cd = cmsdb.cdnhosting.CDNHosting.create(metadata)
        url = 'https://s3.amazonaws.com/host-thumbnails/image.jpg'
        tid = 'bfea94933dc752a2def8a6d28f9ac4c2'
        mresponse = tornado.httpclient.HTTPResponse(
            tornado.httpclient.HTTPRequest('http://cloudinary.com'), 
            200, buffer=StringIO(mock_response))
        mock_http.side_effect = \
          lambda x, **kw: tornado.httpclient.HTTPResponse(
              x, 200,buffer=StringIO(mock_response))
        url = cd.upload(self.image, tid, url)
        self.assertEquals(mock_http.call_count, 2)
        self.assertIsNotNone(mock_http._mock_call_args_list[0][0][0]._body)
        self.assertEqual(mock_http._mock_call_args_list[0][0][0].url,
                "https://api.cloudinary.com/v1_1/neon-labs/image/upload")

    @patch('cmsdb.cdnhosting.utils.http.send_request')
    def test_cloudinary_error(self, mock_http):
        mock_http = self._future_wrap_mock(mock_http)

        metadata = neondata.CloudinaryCDNHostingMetadata()
        cd = cmsdb.cdnhosting.CDNHosting.create(metadata)
        url = 'https://s3.amazonaws.com/host-thumbnails/image.jpg'
        tid = 'bfea94933dc752a2def8a6d28f9ac4c2'
        mresponse = tornado.httpclient.HTTPResponse(
            tornado.httpclient.HTTPRequest('http://cloudinary.com'), 
            502, buffer=StringIO("gateway error"))
        mock_http.side_effect = \
          lambda x, **kw: tornado.httpclient.HTTPResponse(
              x, 502, buffer=StringIO("gateway error"))
        with self.assertLogExists(logging.ERROR,
                'Failed to upload file to cloudinary .*%s' % tid):
            with self.assertRaises(IOError):
                url = cd.upload(self.image, tid, url)
        self.assertEquals(mock_http.call_count, 1)


class TestAWSHostingWithServingUrls(CDNTestBase):
    ''' 
    Test the ability to host images on an aws cdn (aka S3)
    '''
    def setUp(self):
        self.s3conn = boto_mock.MockConnection()
        self.s3_patcher = patch('cmsdb.cdnhosting.S3Connection')
        self.mock_conn = self.s3_patcher.start()
        self.mock_conn.return_value = self.s3conn
        self.s3conn.create_bucket('hosting-bucket')
        self.bucket = self.s3conn.get_bucket('hosting-bucket')

        # Mock out the cdn url check
        self.cdn_check_patcher = patch('cmsdb.cdnhosting.utils.http')
        self.mock_cdn_url = self._future_wrap_mock(
            self.cdn_check_patcher.start().send_request)
        self.mock_cdn_url.side_effect = lambda x, **kw: HTTPResponse(x, 200)

        random.seed(1654984)

        sizes = [(640, 480), (160, 90)]
        self.metadata = neondata.NeonCDNHostingMetadata(None,
            'hosting-bucket', ['cdn1.cdn.com', 'cdn2.cdn.com'],
            'folder1', True, True, False, False, sizes)
        self.metadata.crop_with_saliency = False
        self.metadata.crop_with_face_detection = False
        self.metadata.crop_with_text_detection = False

        self.image = PILImageUtils.create_random_image(480, 640)
        super(TestAWSHostingWithServingUrls, self).setUp()

    def tearDown(self):
        self.s3_patcher.stop()
        self.cdn_check_patcher.stop()
        super(TestAWSHostingWithServingUrls, self).tearDown()

    @tornado.testing.gen_test
    def test_host_resized_images(self):
        hoster = cmsdb.cdnhosting.CDNHosting.create(self.metadata)
        yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)

        serving_urls = neondata.ThumbnailServingURLs.get('acct1_vid1_tid1')
        self.assertIsNotNone(serving_urls)

        for w, h in self.metadata.rendition_sizes:

            # check that the image is in s3
            key_name = 'folder1/neontnacct1_vid1_tid1_w%i_h%i.jpg' % (w, h)
            s3key = self.bucket.get_key(key_name)
            self.assertIsNotNone(s3key)
            buf = StringIO()
            s3key.get_contents_to_file(buf)
            buf.seek(0)
            im = PIL.Image.open(buf)
            self.assertEqual(im.size, (w, h))
            self.assertEqual(im.mode, 'RGB')
            self.assertEqual(s3key.policy, 'public-read')

            # Check that the serving url is included
            url = serving_urls.get_serving_url(w, h)
            self.assertRegexpMatches(
                url, 'http://cdn[1-2].cdn.com/%s' % key_name)

    @tornado.testing.gen_test
    def test_https_cdn_prefix(self):
        self.metadata.cdn_prefixes = ['https://cdn1.cdn.com/neon',
                                      'https://cdn2.cdn.com/neon']

        hoster = cmsdb.cdnhosting.CDNHosting.create(self.metadata)
        yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)

        serving_urls = neondata.ThumbnailServingURLs.get('acct1_vid1_tid1')
        self.assertIsNotNone(serving_urls)

        for w, h in self.metadata.rendition_sizes:
            key_name = 'folder1/neontnacct1_vid1_tid1_w%i_h%i.jpg' % (w, h)
            s3key = self.bucket.get_key(key_name)
            self.assertIsNotNone(s3key)

            url = serving_urls.get_serving_url(w, h)
            self.assertRegexpMatches(
                url, 'https://cdn[1-2].cdn.com/neon/%s' % key_name)
                                                   

    @tornado.testing.gen_test
    def test_salted_path(self):
        self.metadata.do_salt = True

        hoster = cmsdb.cdnhosting.CDNHosting.create(self.metadata)
        yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)

        serving_urls = neondata.ThumbnailServingURLs.get('acct1_vid1_tid1')
        self.assertIsNotNone(serving_urls)

        keyRe = re.compile('(folder1/[0-9a-zA-Z]{3})/neontnacct1_vid1_tid1_'
                           'w([0-9]+)_h([0-9]+).jpg')
        sizes_found = []
        folders_found = []
        base_urls = []
        for s3key in self.bucket.list():
            # Make sure that the key is the expected format with salt
            match = keyRe.match(s3key.name)
            self.assertIsNotNone(match)

            width = int(match.group(2))
            height = int(match.group(3))
            sizes_found.append((width, height))
            folders_found.append(match.group(1))

            # Check that the serving url is included
            url = serving_urls.get_serving_url(width, height)
            self.assertRegexpMatches(
                url, 'http://cdn[1-2].cdn.com/%s' % s3key.name)
            base_urls.append(url.rpartition('/')[0])

            # Check that the image is as expected
            buf = StringIO()
            s3key.get_contents_to_file(buf)
            buf.seek(0)
            im = PIL.Image.open(buf)
            self.assertEqual(im.size, (width, height))
            self.assertEqual(im.mode, 'RGB')
            self.assertEqual(s3key.policy, 'public-read')

        # Make sure that all the expected files were found
        self.assertItemsEqual(sizes_found, self.metadata.rendition_sizes)

        # Make sure that the folders were the same
        self.assertEquals(len(folders_found),
                          len(self.metadata.rendition_sizes))
        self.assertEquals(len(set(folders_found)), 1)

        # Make sure the base urls were the same
        self.assertEquals(len(base_urls), len(self.metadata.rendition_sizes))
        self.assertEquals(len(set(base_urls)), 1)

    @tornado.testing.gen_test
    def test_delete_salted_image(self):
        self.metadata.do_salt = True

        hoster = cmsdb.cdnhosting.CDNHosting.create(self.metadata)
        yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)

        self.assertEquals(len(list(self.bucket.list())), 2)

        serving_urls = neondata.ThumbnailServingURLs.get('acct1_vid1_tid1')
        self.assertIsNotNone(serving_urls)

        yield hoster.delete(serving_urls.get_serving_url(640,480), async=True)

        self.assertEquals(len(list(self.bucket.list())), 1)

    @tornado.testing.gen_test
    def test_change_rendition_sizes(self):
        # Upload 640x480 & 160x90
        hoster = cmsdb.cdnhosting.CDNHosting.create(self.metadata)
        yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)
        
        serving_urls = neondata.ThumbnailServingURLs.get('acct1_vid1_tid1')
        self.assertIsNotNone(serving_urls)

        for w, h in self.metadata.rendition_sizes:
            key_name = 'folder1/neontnacct1_vid1_tid1_w%i_h%i.jpg' % (w, h)
            s3key = self.bucket.get_key(key_name)
            self.assertIsNotNone(s3key)

        # Change the rendition sizes to be 320x240 & 160x90 but keep
        # the old rendition around
        self.metadata.rendition_sizes = [(320, 240), (160, 90)]
        hoster = cmsdb.cdnhosting.CDNHosting.create(self.metadata)
        yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)

        # Check that the serving urls include the intersection
        serving_urls = neondata.ThumbnailServingURLs.get('acct1_vid1_tid1')
        for w, h in [(320, 240), (160, 90), (640, 480)]:
            self.assertRegexpMatches(serving_urls.get_serving_url(w, h),
                                     'http://cdn[1-2].cdn.com/.*\.jpg')

        # Now overwrite the serving urls and check that the 640x480 isn't there
        yield hoster.upload(self.image, 'acct1_vid1_tid1',
                            servingurl_overwrite=True, async=True)
        serving_urls = neondata.ThumbnailServingURLs.get('acct1_vid1_tid1')
        for w, h in [(320, 240), (160, 90)]:
            self.assertRegexpMatches(serving_urls.get_serving_url(w, h),
                                     'http://cdn[1-2].cdn.com/.*\.jpg')
        with self.assertRaises(KeyError):
            serving_urls.get_serving_url(640, 480)

        # Make sure the keys are consistent in S3
        for w, h in self.metadata.rendition_sizes:
            key_name = 'folder1/neontnacct1_vid1_tid1_w%i_h%i.jpg' % (w, h)
            s3key = self.bucket.get_key(key_name)
            self.assertIsNotNone(s3key)

        # Don't delete the 640x480 image because it will take a while
        # until we stop serving it.
        s3key = self.bucket.get_key(
            'folder1/neontnacct1_vid1_tid1_w640_h480.jpg')
        self.assertIsNotNone(s3key)

    @tornado.testing.gen_test
    def test_bad_url_generated(self):
        self.mock_cdn_url.side_effect = lambda x, **kw: HTTPResponse(
            x, 404, error=HTTPError(404))
        hoster = cmsdb.cdnhosting.CDNHosting.create(self.metadata)

        with self.assertRaisesRegexp(IOError, 'CDN url .* is invalid'):
            yield hoster.upload(self.image, 'acct1_vid1_tid1', async=True)
            
        # Make sure there are no serving urls
        self.assertIsNone(neondata.ThumbnailServingURLs.get('acct1_vid1_tid1'))
        

class TestAkamaiHosting(CDNTestBase):
    '''
    Test uploading images to Akamai
    '''
    def setUp(self):
        super(TestAkamaiHosting, self).setUp()

        # Mock out the http requests, one mock for each type
        self.akamai_mock = MagicMock()
        self.akamai_mock.side_effect = lambda x, **kw: HTTPResponse(x, 200)
        self.cdn_mock = MagicMock()
        self.cdn_mock.side_effect = lambda x, **kw: HTTPResponse(x, 200)
        self.http_patcher = patch('cmsdb.cdnhosting.utils.http')
        self.http_mock = self._future_wrap_mock(
            self.http_patcher.start().send_request)
        def _handle_http_request(request, *args, **kwargs):
            if 'cdn' in request.url:
                return self.cdn_mock(request, *args, **kwargs)
            else:
                return self.akamai_mock(request, *args, **kwargs)
        self.http_mock.side_effect = _handle_http_request
        
        random.seed(1654984)
        
        self.image = PILImageUtils.create_random_image(480, 640)

    def tearDown(self):
        self.http_patcher.stop()
        super(TestAkamaiHosting, self).tearDown()

    @tornado.testing.gen_test
    def test_upload_image(self):
        metadata = neondata.AkamaiCDNHostingMetadata(
            key=None,
            host='akamai',
            akamai_key='akey',
            akamai_name='aname',
            cpcode='168974',
            folder_prefix=None,
            cdn_prefixes=['cdn1.akamai.com', 'cdn2.akamai.com']
            )

        self.hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)
        
        tid = 'customeraccountnamelabel_vid1_tid1'

        # the expected root of the url is the first 24 characters of the tid
        url_root_folder = tid[:24]

        yield self.hoster.upload(self.image, tid, async=True)
       
        # Check http mock and Akamai request
        # make sure the http mock was called
        self.assertGreater(self.akamai_mock._mock_call_count, 0)
        upload_requests = [x[0][0] for x in
                           self.akamai_mock._mock_call_args_list]
        for request in upload_requests:
            self.assertItemsEqual(request.headers.keys(),
                                  ['Content-Length',
                                   'X-Akamai-ACS-Auth-Data',
                                   'X-Akamai-ACS-Auth-Sign',
                                   'X-Akamai-ACS-Action'])
            actions = urlparse.parse_qs(request.headers['X-Akamai-ACS-Action'])
            self.assertDictContainsSubset(
                { 'version': ['1'],
                  'action' : ['upload'],
                  'format' : ['xml']
                  },
                  actions)
            self.assertIn('md5', actions)
                  
            self.assertRegexpMatches(
                request.url, 
                ('http://akamai/168974/%s/[a-zA-Z]/[a-zA-Z]/[a-zA-Z]/'
                 'neontn%s_w[0-9]+_h[0-9]+.jpg' % (url_root_folder, tid)))
            self.assertEquals(request.method, "POST")

        # Check serving URLs
        ts = neondata.ThumbnailServingURLs.get(tid)
        self.assertGreater(ts.get_serving_url_count(), 0)

        base_urls = []

        # Verify the final image URLs. This should be the account id 
        # followed by 3 sub folders whose name should be a single letter
        # (lower or uppercase) choosen randomly, then the thumbnail file
        for (w, h), url in ts:
            url_re = ('(http://cdn[12].akamai.com/%s/[a-zA-Z]/[a-zA-Z]/'
                      '[a-zA-Z])/neontn%s_w%s_h%s.jpg' % 
                      (url_root_folder, tid, w, h))
                
            self.assertRegexpMatches(url, url_re)

            # Grab the base url
            base_urls.append(re.compile(url_re).match(url).group(1))

        # Make sure all the base urls are the same for a given thumb
        self.assertGreater(len(base_urls), 1)
        self.assertEquals(len(set(base_urls)), 1)

        # Make sure that the url is exactly what we expect. If this
        # check fails, then the python random module had changed
        self.assertEquals(
            base_urls[0],
            'http://cdn1.akamai.com/customeraccountnamelabel/i/E/O')

    @tornado.testing.gen_test
    def test_with_folder_prefix(self):
        metadata = neondata.AkamaiCDNHostingMetadata(
            key=None,
            host='http://akamai.com/',
            akamai_key='akey',
            akamai_name='aname',
            cpcode='168974',
            folder_prefix='neon/prod',
            cdn_prefixes=['https://cdn1.akamai.com', 'https://cdn2.akamai.com']
            )

        self.hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)
        
        tid = 'customeraccountnamelabel_vid1_tid1'

        # the expected root of the url is the first 24 characters of the tid
        url_root_folder = tid[:24]

        yield self.hoster.upload(self.image, tid, async=True)

        # Check http mock and Akamai request
        # make sure the http mock was called
        self.assertGreater(self.akamai_mock._mock_call_count, 0)
        upload_requests = [x[0][0] for x in
                           self.akamai_mock._mock_call_args_list]
        for request in upload_requests:
            self.assertItemsEqual(request.headers.keys(),
                                  ['Content-Length',
                                   'X-Akamai-ACS-Auth-Data',
                                   'X-Akamai-ACS-Auth-Sign',
                                   'X-Akamai-ACS-Action'])
            actions = urlparse.parse_qs(request.headers['X-Akamai-ACS-Action'])
            self.assertDictContainsSubset(
                { 'version': ['1'],
                  'action' : ['upload'],
                  'format' : ['xml']
                  },
                  actions)
            self.assertIn('md5', actions)
                  
            self.assertRegexpMatches(
                request.url, 
                ('http://akamai.com/168974/neon/prod/%s/[a-zA-Z]/[a-zA-Z]/'
                 '[a-zA-Z]/neontn%s_w[0-9]+_h[0-9]+.jpg' % 
                 (url_root_folder, tid)))
            self.assertEquals(request.method, "POST")

        # Check serving URLs
        ts = neondata.ThumbnailServingURLs.get(tid)
        self.assertGreater(len(ts), 0)

        # Verify the final image URLs. This should be the account id 
        # followed by 3 sub folders whose name should be a single letter
        # (lower or uppercase) choosen randomly, then the thumbnail file
        for (w, h), url in ts.size_map.iteritems():
            url = ts.get_serving_url(w, h)
            url_re = ('(https://cdn[12].akamai.com/neon/prod/%s/[a-zA-Z]/'
                      '[a-zA-Z]/[a-zA-Z])/neontn%s_w%s_h%s.jpg' % 
                      (url_root_folder,tid, w, h))
                
            self.assertRegexpMatches(url, url_re)

    @tornado.testing.gen_test
    def test_no_overwrite(self):
        metadata = neondata.AkamaiCDNHostingMetadata(
            key=None,
            host='akamai',
            akamai_key='akey',
            akamai_name='aname',
            cpcode='168974',
            folder_prefix=None,
            cdn_prefixes=['cdn1.akamai.com', 'cdn2.akamai.com'],
            rendition_sizes=[(640, 480)]
            )

        self.hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)

        yield self.hoster.upload(self.image, 'some_vid_tid', overwrite=False,
                                 async=True)

        # Only one call should have been sent to akamai and it should be a stat
        self.assertEquals(self.akamai_mock.call_count, 1)
        cargs, kwargs = self.akamai_mock.call_args
        request = cargs[0]
        self.assertIn('action=stat', request.headers['X-Akamai-ACS-Action'])
        
    
    @tornado.testing.gen_test
    def test_upload_image_error(self):
        self.akamai_mock.side_effect = lambda x, **kw: HTTPResponse(x, 500)
        metadata = neondata.AkamaiCDNHostingMetadata(
            key=None,
            host='http://akamai',
            akamai_key='akey',
            akamai_name='aname',
            cpcode='168974',
            cdn_prefixes=['cdn1.akamai.com', 'cdn2.akamai.com']
            )

        self.hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)
        self.hoster.ntries = 2
        tid = 'akamai_vid1_tid2'
        
        with self.assertLogExists(logging.ERROR, 
                'Error uploading file to akamai.*%s' % tid):
            with self.assertRaises(IOError):
                yield self.hoster.upload(self.image, tid, async=True)
        
        self.assertGreater(self.akamai_mock._mock_call_count, 0)
        
        ts = neondata.ThumbnailServingURLs.get(tid)
        self.assertIsNone(ts)

    @tornado.testing.gen_test
    def test_bad_url_generated(self):
        self.cdn_mock.side_effect = lambda x, **kw: HTTPResponse(
            x, 404, error=HTTPError(404))
        
        metadata = neondata.AkamaiCDNHostingMetadata(
            key=None,
            host='akamai',
            akamai_key='akey',
            akamai_name='aname',
            cpcode='168974',
            folder_prefix=None,
            cdn_prefixes=['cdn1.akamai.com', 'cdn2.akamai.com']
            )

        self.hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)
        
        tid = 'customeraccountnamelabel_vid1_tid1'

        with self.assertRaisesRegexp(IOError, 'CDN url .* is invalid'):
            yield self.hoster.upload(self.image, tid, async=True)
            
        # Make sure there are no serving urls
        self.assertIsNone(neondata.ThumbnailServingURLs.get(tid))

class TestVideoUploading(test_utils.neontest.AsyncTestCase):
    ''' 
    Test the ability to host images on an aws cdn (aka S3)
    '''
    def setUp(self):
        self.s3conn = boto_mock.MockConnection()
        self.s3_patcher = patch('cmsdb.cdnhosting.S3Connection')
        self.mock_conn = self.s3_patcher.start()
        self.mock_conn.return_value = self.s3conn
        self.s3conn.create_bucket('hosting-bucket')
        self.bucket = self.s3conn.get_bucket('hosting-bucket')

        # Mock out the cdn url check
        self.cdn_check_patcher = patch('cmsdb.cdnhosting.utils.http')
        self.mock_cdn_url = self._future_wrap_mock(
            self.cdn_check_patcher.start().send_request)
        self.mock_cdn_url.side_effect = lambda x, **kw: HTTPResponse(x, 200)

        self.mov = test_utils.opencv.VideoCaptureMock(frame_count=60, fps=30.0)
        self.account_id = 'aid0'
        self.video_id = 'aid0_extvid0'
        self.clip = neondata.Clip(
            'cid0',
            video_id=self.video_id,
            start_frame=15,
            end_frame=20,)
        self.key_name_format = 'folder1/neonvr{}_{}_w%i_h%i.mp4'.format(
            self.video_id,
            self.clip.get_id())
        
        random.seed(1654984)

        self.image = PILImageUtils.create_random_image(480, 640)
        random.seed(time.time())
        super(TestVideoUploading, self).setUp()

    def tearDown(self):
        self.s3_patcher.stop()
        self.cdn_check_patcher.stop()
        super(TestVideoUploading, self).tearDown()

    def _get_mp4_hoster(self, **kwargs):

        # Merge kwargs over a default set of args.
        init_args = {
            'key':None,
            'access_key':'access_key',
            'secret_key':'secret_key',
            'bucket_name':'hosting-bucket',
            'cdn_prefixes':['cdn1.cdn.com', 'cdn2.cdn.com'],
            'folder_prefix':'folder1',
            'resize':False,
            'update_serving_urls':False,
            'do_salt':False,
            'make_tid_folders':False,
            'video_rendition_formats':[(None, None, 'mp4', None),
                                     (400, 304, 'mp4', 'libx264')]}
        init_args.update(kwargs)
        metadata = neondata.S3CDNHostingMetadata(**init_args)

        # Return a hosting created from the merged metadata.
        return cmsdb.cdnhosting.CDNHosting.create(metadata)

    @tornado.testing.gen_test(timeout=20.0)
    def test_upload_mp4(self):

        hoster = self._get_mp4_hoster()

        upload_results = yield hoster.upload_video(
            self.mov, self.clip, async=True)

        self.assertItemsEqual([x[1:] for x in upload_results], 
                               [(640, 480, 'mp4', 'libx264'),
                                (400, 304, 'mp4', 'libx264')])
        # Check the rendition files
        for url, w, h, container, codec in upload_results:

            s3key = self.bucket.get_key(self.key_name_format % (w, h))
            self.assertIsNotNone(s3key)

            with tempfile.NamedTemporaryFile(suffix='.mp4') as target:
                s3key.get_contents_to_file(target)
                target.flush()
                found_mov = cv2.VideoCapture(target.name)

                self.assertEquals(found_mov.get(cv2.CAP_PROP_FRAME_WIDTH), w)
                self.assertEquals(found_mov.get(cv2.CAP_PROP_FRAME_HEIGHT), h)
                self.assertEquals(found_mov.get(cv2.CAP_PROP_FRAME_COUNT), 5)
                self.assertEquals(found_mov.get(cv2.CAP_PROP_FPS), 30.0)
            
            re = 'http://cdn[1-2].cdn.com/%s' % self.key_name_format % (w, h)
            self.assertRegexpMatches(url, re)

    @tornado.testing.gen_test(timeout=20.0)
    def test_mp4_key_with_make_tid_folders(self):

        hoster = self._get_mp4_hoster(make_tid_folders=True)
        # Ensure the path is constructed correctly with tid folders and
        # filename has fewer parts.
        upload_results = yield hoster.upload_video(
            self.mov, self.clip, async=True)

        # Check the rendition urls 
        for url, w, h, _, _ in upload_results:

            external_ref = neondata.InternalVideoID.to_external(self.video_id)
            key_name = 'folder1/%s/%s/%s/w%i_h%i.mp4' % (
                self.account_id,
                external_ref,
                self.clip.get_id(),
                w,
                h)
            s3key = self.bucket.get_key(key_name)
            self.assertIsNotNone(s3key)

            re = 'http://cdn[1-2].cdn.com/' + key_name
            self.assertRegexpMatches(url, re)

    @tornado.testing.gen_test(timeout=20.0)
    def test_mp4_width_or_height_is_none(self):

        hoster = self._get_mp4_hoster(
            video_rendition_formats=[
                (None, 100, 'mp4', None),
                (200, None, 'mp4', None)])

        # Ensure the basename has the implicitly scaled dimension.
        upload_results = yield hoster.upload_video(
            self.mov, self.clip, async=True)
        for url, w, h, _, _ in upload_results:
            re = 'http://cdn[1-2].cdn.com/%s' % self.key_name_format % (w, h)
            self.assertRegexpMatches(url, re)


    @tornado.testing.gen_test
    def test_upload_gif(self):
        metadata = neondata.S3CDNHostingMetadata(None,
            'access_key', 'secret_key',
            'hosting-bucket', ['cdn1.cdn.com', 'cdn2.cdn.com'],
            'folder1', False, False, False,
            video_rendition_formats=[(320, 240, 'gif', None)])

        self.hoster = cmsdb.cdnhosting.CDNHosting.create(metadata)

        upload_results = yield self.hoster.upload_video(
            self.mov, self.clip, async=True)
        self.assertEquals(len(upload_results), 1)

        url, w, h, container, codec = upload_results[0]
        self.assertEquals(container, 'gif')
        self.assertIsNone(codec)
        self.assertEquals(w, 320)
        self.assertEquals(h, 240)

        
        key_name = 'folder1/neonvraid0_extvid0_cid0_w320_h240.gif'
        s3key = self.bucket.get_key(key_name)
        self.assertIsNotNone(s3key)
        self.assertRegexpMatches(
                url, 'http://cdn[1-2].cdn.com/%s' % key_name)

        buf = StringIO()
        s3key.get_contents_to_file(buf)
        buf.seek(0)
        gif_image = PIL.Image.open(buf)

        self.assertEquals(gif_image.size, (320, 240))
        self.assertAlmostEqual(np.round(1000./gif_image.info['duration']), 6)
        
        
if __name__ == '__main__':
    utils.neon.InitNeon()
    unittest.main()
