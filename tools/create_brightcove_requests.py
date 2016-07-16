#!/usr/bin/python
'''
BRIGHTCOVE CRON 

Parse Brightcove Feed for all customers and 
Create api requests for the brightcove customers 

'''
import os.path
import sys
__base_path__ = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if sys.path[0] != __base_path__:
    sys.path.insert(0, __base_path__)

import redis as blockingRedis
import os
from cmsdb.neondata import *
import json
import urllib2
import utils.neon
import utils.monitor
from utils.options import define, options
from utils import statemon

import logging
_log = logging.getLogger(__name__)

statemon.define('cron_finished', int)
statemon.define('cron_error', int)
statemon.define('accnt_delay', int)

def check_single_brightcove_account_delay(self, api_key='6d3d519b15600c372a1f6735711d956e', i_id='52'):
    '''
    Maintains a counter to help understand the delay between api call and request creation
    '''
    ba = BrighcovePlatform.get(api_key, i_id) 
    req = 'http://api.brightcove.com/services/library?command=find_all_videos&token=%s&media_delivery=http&output=json&sort_by=publish_date' %ba.read_token
    response = urllib2.urlopen(req)
    resp = json.loads(resp.read())
    for item in vitems['items']:
        vid = str(item['id'])
        if not ba.video.has_key(vid):
            statemon.state.increment('accnt_delay')
            return #return on a single delay detection

if __name__ == "__main__":
    utils.neon.InitNeon()
    pid = str(os.getpid())
    pidfile = "/tmp/brightcovecron.pid"
    if os.path.isfile(pidfile):
        with open(pidfile, 'r') as f:
            pid = f.readline().rstrip('\n')
            if os.path.exists('/proc/%s' %pid):
                print "%s already exists, exiting" % pidfile
                sys.exit()
            else:
                os.unlink(pidfile)

    else:
        file(pidfile, 'w').write(pid)

        try:
            # Get all Brightcove accounts
            dbconn = DBConnection(BrightcovePlatform)
            keys = dbconn.blocking_conn.keys('brightcoveplatform*')
            accounts = []
            for k in keys:
                parts = k.split('_')
                bp = BrightcovePlatform.get(parts[-2], parts[-1])
                accounts.append(bp)
            for accnt in accounts:
                # If not enabled for processing, skip
                if accnt.enabled == False:
                    continue
                api_key = accnt.neon_api_key
                i_id = accnt.integration_id 
                _log.info("key=brightcove_request msg= internal account %s "
                          "i_id %s" %(api_key, i_id))
                accnt.check_feed_and_create_api_requests()
                accnt.check_playlist_feed_and_create_requests()

        except Exception as e:
            _log.exception('key=create_brightcove_requests '
                           'msg=Unhandled exception %s'
                           % e)
            statemon.state.increment('cron_error')

        os.unlink(pidfile)
    statemon.state.increment('cron_finished')
    utils.monitor.send_statemon_data()


'''

'''