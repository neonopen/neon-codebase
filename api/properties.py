

#====== Properties file - DEV ===============#

#======== API Spec ====================#
THUMBNAIL_RATE = "rate"
TOP_THUMBNAILS = "topn"
THUMBNAIL_SIZE = "size"
ABTEST_THUMBNAILS = "abtest"
THUMBNAIL_INTERVAL = "interval"
CALLBACK_URL = "callback_url"
VIDEO_ID = "video_id"
VIDEO_DOWNLOAD_URL = "video_url"
VIDEO_TITLE = "video_title"
BCOVE_READ_TOKEN = "read_token"
BCOVE_WRITE_TOKEN = "write_token"
LOG_FILE = "/tmp/neon-server.log"
REQUEST_UUID_KEY = "job_id"
API_KEY = "api_key"
JOB_SUBMIT_TIME = "submit_time"
JOB_END_TIME = "end_time"
VIDEO_PROCESS_TIME = "process_time"
YOUTUBE_VIDEO_URL = 'youtube_url'
LOCALHOST_URL = "http://localhost:8081"
BASE_SERVER_URL = "http://50.19.216.114:8081" #EIP
IMAGE_SIZE = 256,256
THUMBNAIL_IMAGE_SIZE = 256,144
MAX_THUMBNAILS  = 25
MAX_SAMPLING_RATE = 0.25
SAVE_DATA_TO_S3 = False
DELETE_TEMP_TAR = False 
YOUTUBE = False
API_KEY_FILE = 'apikeys.json'
BRIGHTCOVE_THUMBNAILS = "brightcove"
PUBLISHER_ID = "publisher_id"
PREV_THUMBNAIL = "previous_thumbnail"
INTEGRATION_ID = "integration_id"
NEON_AUTH = "secret_token"
#=========== S3 Config ===============#
S3_KEY_PREFIX = 'internal_test_'

#Prod
#S3_ACCESS_KEY = 'AKIAI5CLWOBKJDWTWZDA'
#S3_SECRET_KEY = '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'
S3_ACCESS_KEY = 'AKIAJ5G2RZ6BDNBZ2VBA'
S3_SECRET_KEY = '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'
S3_BUCKET_NAME = 'neon-beta-test' 
S3_IMAGE_HOST_BUCKET_NAME = 'host-thumbnails' 
S3_CUSTOMER_ACCOUNT_BUCKET_NAME = 'neon-customer-accounts'

#Frontend config
NOTIFICATION_API_KEY = 'icAxBCbwo--owZaFED8hWA'
NOTIFICATION_API_KEY_STAGING = 'kR0ks7NpdSD0w6xXAAvWfw'

#IMAGE CDN
CDN_IMAGE_SIZES = [(120, 67), (160, 90), (320, 180), (480, 270), 
        (640, 360), (120, 90), (160, 120), (320, 240), (480, 360),
        (640, 480), (1280, 720)]
S3_IMAGE_CDN_BUCKET_NAME = "neon-image-cdn"
CDN_URL_PREFIX = "imagecdn.neon-lab.com"

## Cloudinary config

CLOUDINARY_API_KEY = '433154993476843'
CLOUDINARY_API_SECRET = 'n0E7427lrS1Fe_9HLbtykf9CdtA' 
CLOUDINARY_NAME = "neon-labs" 
